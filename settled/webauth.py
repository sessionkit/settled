"""webauth.py: a passkey (WebAuthn) lock for Settings on the web.

WebAuthn, so a user must authenticate to use Settings and make changes;
`tailscale serve` gives the HTTPS URL WebAuthn needs. Touch ID on the Mac,
Face ID on the phone: one passkey, synced by iCloud Keychain.

Pure functions, no network: web.py calls them over its websocket. The browser
hands over the credential's public key itself (getPublicKey(), SubjectPublicKeyInfo),
so no CBOR is parsed here. Attestation is "none": we trust the browser to say
which key it made, and the Enclave or the YubiKey to guard it.

A passkey belongs to one relying party (RP), a host name: the tailnet name
(https://<host>.<tailnet>.ts.net:PORT) or localhost (http://localhost:PORT).
WebAuthn refuses an IP address, so 127.0.0.1 cannot have one.

No trust on first use: even with NO passkey stored at all, a
locked page is never offered a bare registration. The Origin header is set by
the client, and behind `tailscale serve` a tailnet device can claim any origin,
so the first visitor could be anyone on the tailnet. The first passkey is
enrolled like any other, below. Adding another needs an unlocked page.

Enrol: a passkey is bound to its RP, so an empty store, or a
host whose store holds only ANOTHER host's passkeys (a laptop carrying a
desktop's passkeys.json), has none of its own and could not unlock at all. It is
offered "enrol": registration for its own RP id, gated by a one-time code shown
on the machine itself
(`settled enrol`, or `make enrol`). The page PRE-CHECKS the code before
it makes the passkey, so a mistyped code leaves none on the device; the check at
registration is the one that spends it. The code is single use, lives
ENROL_TTL seconds, burns after ENROL_TRIES wrong tries, and only a salted hash
of it is kept (ENROL, 0600, beside passkeys.json). Someone who reaches the page
without being at the machine has no code, so cannot enrol. Once a host has a
passkey of its own, enrol is closed to it: more keys are added from an unlocked
page, as before.
"""
import base64, hashlib, hmac, json, os, secrets, time
from pathlib import Path
from urllib.parse import urlparse

from settled import paths  # settled's own folder, and the one-time carry of the old store
from settled.sessions import OWNER  # the desk's one user, never a person's name

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

STORE = Path(os.environ.get("SETTINGS_PASSKEYS", paths.settled_home() / "passkeys.json"))
UP, UV = 0x01, 0x04          # authenticatorData flags: user present, user verified
ES256, RS256 = -7, -257
ENROL = Path(os.environ.get("SETTINGS_ENROL", STORE.parent / "enrol.json"))   # the pending enrol code's hash
ENROL_TTL = 600              # seconds an enrol code is good for
ENROL_TRIES = 5              # wrong tries that burn it
ENROL_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"   # no 0/O, 1/I/L: read off a screen, typed on a phone
ENROL_LEN = 10


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def rp_of(origin: str) -> str:
    return urlparse(origin).hostname or ""


def load(path: Path = None) -> list:
    p = path or STORE
    if path is None and "SETTINGS_PASSKEYS" not in os.environ:
        # The first read at the new path copies the pre-package store once, so a
        # passkey enrolled before the move still signs in (paths.py).
        old = paths.legacy_state()
        if old is not None:
            paths.carry(p, old / "passkeys.json")
    try:
        return json.loads(p.read_text()).get("credentials", [])
    except FileNotFoundError:
        return []


def save(creds: list, path: Path = None) -> None:
    p = path or STORE
    p.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(p.parent, 0o700)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"credentials": creds}, indent=2) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(p)


def _write_private(p: Path, text: str) -> None:
    """Write `p` atomically, 0600 from the first byte, its directory 0700."""
    p.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(p.parent, 0o700)
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    tmp.replace(p)


def _enrol_hash(code: str, salt: str) -> str:
    return hashlib.sha256(bytes.fromhex(salt) + code.encode()).hexdigest()


def _enrol_norm(code) -> str:
    """What was typed, as the code is printed: upper case, no spaces or dashes."""
    return "".join(str(code or "").split()).replace("-", "").upper()


def _enrol_drop() -> None:
    try:
        ENROL.unlink()
    except FileNotFoundError:
        pass


def new_enrol_code(now: float = None) -> str:
    """A fresh one-time enrol code, good for ENROL_TTL seconds; it replaces any
    earlier one. Only its salted hash, expiry and wrong-try count go to disk."""
    now = time.time() if now is None else now
    code = "".join(secrets.choice(ENROL_ALPHABET) for _ in range(ENROL_LEN))
    salt = secrets.token_hex(16)
    _write_private(ENROL, json.dumps({"salt": salt, "hash": _enrol_hash(code, salt),
                                      "expires": now + ENROL_TTL, "wrong": 0}) + "\n")
    return code


def check_enrol_code(code, now: float = None, consume: bool = True) -> str:
    """'' when `code` is the pending enrol code, which is then CONSUMED; else why
    not. A wrong try counts: the ENROL_TRIES-th burns the code.

    consume=False is the PRE-CHECK, made before the passkey
    ceremony so a mistyped code never leaves a passkey on the device: a right code
    says '' and stays pending, its try count unchanged; a wrong one counts exactly
    as above, and an expired, burnt or missing code fails as above. The finish
    checks again with consume=True, so a pre-check alone never enrols."""
    now = time.time() if now is None else now
    try:
        rec = json.loads(ENROL.read_text())
        salt, want, expires, wrong = rec["salt"], rec["hash"], float(rec["expires"]), int(rec.get("wrong", 0))
    except FileNotFoundError:
        return "no enrol code is pending: run `settled enrol` on this machine"
    except (KeyError, TypeError, ValueError):
        _enrol_drop()                    # unreadable: fail closed, and clear it
        return "no enrol code is pending: run `settled enrol` on this machine"
    if now >= expires or wrong >= ENROL_TRIES:
        _enrol_drop()
        return "the enrol code has expired: run `settled enrol` for a new one" if now >= expires \
            else "the enrol code is burnt: run `settled enrol` for a new one"
    got = _enrol_norm(code)
    if got and hmac.compare_digest(_enrol_hash(got, salt), want):
        if consume:
            _enrol_drop()                # single use
        return ""
    wrong += 1
    if wrong >= ENROL_TRIES:
        _enrol_drop()
        return f"wrong enrol code, {wrong} tries: it is burnt, run `settled enrol` for a new one"
    _write_private(ENROL, json.dumps({**rec, "wrong": wrong}) + "\n")
    return f"wrong enrol code ({ENROL_TRIES - wrong} tries left)"


def offer(creds: list, rp_id: str, may_register: bool) -> tuple:
    """(what the page needs before the tap, the challenge bytes). Login when this
    RP has a passkey; register when it has none and registering is allowed (only
    the add offer an unlocked, stepped-up page gets: a locked page never passes
    may_register: no trust on first use); else enrol, a registration for this RP that the
    page must send with the one-time enrol code."""
    c = secrets.token_bytes(32)
    mine = [x["id"] for x in creds if x.get("rp_id") == rp_id]
    out = {"type": "auth", "challenge": b64u(c), "rpId": rp_id, "allow": mine,
           "mode": "login" if mine else "register" if may_register else "enrol"}
    if out["mode"] in ("register", "enrol"):
        out["user"] = {"id": b64u(secrets.token_bytes(16)), "name": OWNER, "displayName": OWNER}
    return out, c


def _client_data(cdj: bytes, kind: str, chal: bytes, origin: str) -> str:
    """'' if clientDataJSON is right for this ceremony, else why not."""
    try:
        cd = json.loads(cdj)
    except ValueError:
        return "clientDataJSON is not JSON"
    if cd.get("type") != kind:
        return f"type {cd.get('type')!r}, wanted {kind!r}"
    if not chal or unb64u(cd.get("challenge", "")) != chal:
        return "challenge does not match"
    if cd.get("origin") != origin:
        return f"origin {cd.get('origin')!r}, wanted {origin!r}"
    return ""


def _auth_data(ad: bytes, rp_id: str) -> str:
    if len(ad) < 37:
        return "authenticatorData too short"
    if ad[:32] != hashlib.sha256(rp_id.encode()).digest():
        return f"rpIdHash is not {rp_id}'s"
    if not (ad[32] & UP and ad[32] & UV):
        return "no user presence and verification (Touch ID, Face ID or PIN)"
    return ""


RSA_MIN_BITS = 2048         # the smallest RSA key an RS256 passkey may have
NAME_LEN = 80               # the longest label a passkey keeps


def clean_label(text) -> str:
    """A browser-supplied label kept printable (no control characters), at most NAME_LEN."""
    return "".join(c for c in text if c.isprintable())[:NAME_LEN] if isinstance(text, str) else ""


def _key_fits(pub, alg: int) -> str:
    """'' when PUB is a key ALG may use: ES256 a P-256 key, RS256 an RSA key of at least
    RSA_MIN_BITS; else why not."""
    if alg == ES256:
        if not isinstance(pub, ec.EllipticCurvePublicKey) or not isinstance(pub.curve, ec.SECP256R1):
            return "public key is not a P-256 key"
        return ""
    if alg == RS256:
        if not isinstance(pub, rsa.RSAPublicKey):
            return "public key is not an RSA key"
        if pub.key_size < RSA_MIN_BITS:
            return f"RSA key of {pub.key_size} bits, under {RSA_MIN_BITS}"
        return ""
    return f"algorithm {alg} not supported"


def register(msg: dict, chal: bytes, origin: str) -> tuple:
    """(credential, '') when the new passkey checks out, else (None, why)."""
    rp_id = rp_of(origin)
    try:
        cdj, ad = unb64u(msg["clientDataJSON"]), unb64u(msg["authenticatorData"])
        spki, alg, cid = unb64u(msg["publicKey"]), int(msg["alg"]), msg["id"]
    except (KeyError, TypeError, ValueError) as x:
        return None, f"malformed: {x}"
    why = _client_data(cdj, "webauthn.create", chal, origin) or _auth_data(ad, rp_id)
    if why:
        return None, why
    if alg not in (ES256, RS256):
        return None, f"algorithm {alg} not supported"
    try:
        pub = serialization.load_der_public_key(spki)
    except (ValueError, TypeError):
        return None, "public key does not load"
    why = _key_fits(pub, alg)
    if why:
        return None, why
    return {"id": cid, "rp_id": rp_id, "spki": b64u(spki), "alg": alg,
            "added": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "name": clean_label(msg.get("name"))}, ""


def login(msg: dict, chal: bytes, origin: str, creds: list) -> str:
    """'' when the assertion is good, else why not."""
    rp_id = rp_of(origin)
    try:
        cdj, ad, sig = unb64u(msg["clientDataJSON"]), unb64u(msg["authenticatorData"]), unb64u(msg["signature"])
        cid = msg["id"]
    except (KeyError, TypeError, ValueError) as x:
        return f"malformed: {x}"
    cred = next((c for c in creds if c["id"] == cid and c.get("rp_id") == rp_id), None)
    if cred is None:
        return "unknown passkey"
    why = _client_data(cdj, "webauthn.get", chal, origin) or _auth_data(ad, rp_id)
    if why:
        return why
    try:
        pub = serialization.load_der_public_key(unb64u(cred["spki"]))
    except (KeyError, TypeError, ValueError):
        return "the kept public key does not load"
    if _key_fits(pub, cred.get("alg")):
        return "the kept key does not match its algorithm"
    signed = ad + hashlib.sha256(cdj).digest()
    try:
        if cred["alg"] == ES256:
            pub.verify(sig, signed, ec.ECDSA(hashes.SHA256()))
        else:
            pub.verify(sig, signed, padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature:
        return "signature does not verify"
    return ""
