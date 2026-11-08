"""Offline tests for webauth.py: a software P-256 key plays the passkey.

Every check the lock relies on is made to FAIL once here (wrong signature,
challenge, origin, RP, missing Touch ID), so none of them is a decoration.
Run: python3 tests/test_webauth.py (or pytest). Needs `cryptography`.
"""
import hashlib, json, os, sys, tempfile
from pathlib import Path

os.environ["SETTINGS_PASSKEYS"] = str(Path(tempfile.mkdtemp(prefix="settings-pk-")) / "passkeys.json")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from settled import webauth as w  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402

ORIGIN = "https://studio.example.ts.net"
RP = "studio.example.ts.net"
KEY = ec.generate_private_key(ec.SECP256R1())
SPKI = KEY.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)


def auth_data(rp=RP, flags=0x05):
    return hashlib.sha256(rp.encode()).digest() + bytes([flags]) + b"\0\0\0\0"


def cdj(kind, chal, origin=ORIGIN):
    return json.dumps({"type": kind, "challenge": w.b64u(chal), "origin": origin}).encode()


def reg_msg(chal, **kw):
    return {"id": "cred1", "clientDataJSON": w.b64u(kw.get("cd") or cdj("webauthn.create", chal)),
            "authenticatorData": w.b64u(kw.get("ad") or auth_data()), "publicKey": w.b64u(SPKI), "alg": -7}


def login_msg(chal, cd=None, ad=None, sig=None):
    cd = cd or cdj("webauthn.get", chal)
    ad = ad or auth_data()
    sig = sig or KEY.sign(ad + hashlib.sha256(cd).digest(), ec.ECDSA(hashes.SHA256()))
    return {"id": "cred1", "clientDataJSON": w.b64u(cd), "authenticatorData": w.b64u(ad), "signature": w.b64u(sig)}


def registered():
    o, chal = w.offer([], RP, may_register=True)
    assert o["mode"] == "register" and o["rpId"] == RP
    cred, why = w.register(reg_msg(chal), chal, ORIGIN)
    assert why == "" and cred["rp_id"] == RP, why
    return [cred]


def test_register_then_login():
    creds = registered()
    o, chal = w.offer(creds, RP, may_register=False)
    assert o["mode"] == "login" and o["allow"] == ["cred1"]
    assert w.login(login_msg(chal), chal, ORIGIN, creds) == ""


def test_offer_enrol_when_other_rp_has_the_key():
    """Another RP's passkey is no use here: offer "enrol", which used to be
    "closed". The registration it offers needs the one-time enrol code."""
    o, _ = w.offer(registered(), "localhost", may_register=False)
    assert o["mode"] == "enrol" and o["allow"] == [] and "user" in o


def test_each_check_fails():
    creds = registered()
    _, chal = w.offer(creds, RP, may_register=False)
    bad = {
        "signature": login_msg(chal, sig=KEY.sign(b"something else", ec.ECDSA(hashes.SHA256()))),
        "challenge": login_msg(chal, cd=cdj("webauthn.get", b"x" * 32)),
        "origin": login_msg(chal, cd=cdj("webauthn.get", chal, "https://evil.example")),
        "rpIdHash": login_msg(chal, ad=auth_data(rp="evil.example")),
        "verification": login_msg(chal, ad=auth_data(flags=0x01)),   # present, not verified
        "type": login_msg(chal, cd=cdj("webauthn.create", chal)),
    }
    for want, msg in bad.items():
        why = w.login(msg, chal, ORIGIN, creds)
        assert want in why, (want, why)
    assert w.login(login_msg(chal), chal, "https://evil.example", creds) == "unknown passkey"
    assert w.login(login_msg(chal), None, ORIGIN, creds) == "challenge does not match"


def test_register_refuses_unverified():
    _, chal = w.offer([], RP, may_register=True)
    cred, why = w.register(reg_msg(chal, ad=auth_data(flags=0x01)), chal, ORIGIN)
    assert cred is None and "verification" in why


def test_store_is_private():
    w.save(registered())
    assert oct(os.stat(w.STORE).st_mode & 0o777) == "0o600"
    assert w.load()[0]["id"] == "cred1"


def _reg_with(key, alg):
    """A registration whose public key is KEY's, under ALG."""
    o, chal = w.offer([], RP, may_register=True)
    spki = key.public_key().public_bytes(serialization.Encoding.DER,
                                         serialization.PublicFormat.SubjectPublicKeyInfo)
    return w.register({**reg_msg(chal), "publicKey": w.b64u(spki), "alg": alg}, chal, ORIGIN)


def test_es256_takes_a_p256_key_only():
    cred, why = _reg_with(ec.generate_private_key(ec.SECP384R1()), -7)
    assert cred is None and why == "public key is not a P-256 key"


def test_rs256_takes_an_rsa_key_of_2048_bits_or_more():
    from cryptography.hazmat.primitives.asymmetric import rsa
    cred, why = _reg_with(rsa.generate_private_key(public_exponent=65537, key_size=1024), -257)
    assert cred is None and "under 2048" in why
    cred, why = _reg_with(rsa.generate_private_key(public_exponent=65537, key_size=2048), -257)
    assert why == "" and cred["alg"] == -257
    cred, why = _reg_with(KEY, -257)  # an EC key under RS256
    assert cred is None and why == "public key is not an RSA key"


def test_a_kept_key_that_does_not_fit_its_algorithm_signs_in_nothing():
    creds = registered()
    weak = ec.generate_private_key(ec.SECP384R1())
    creds[0]["spki"] = w.b64u(weak.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo))
    o, chal = w.offer(creds, RP, may_register=False)
    cd, ad = cdj("webauthn.get", chal), auth_data()
    sig = weak.sign(ad + hashlib.sha256(cd).digest(), ec.ECDSA(hashes.SHA256()))
    assert w.login(login_msg(chal, cd=cd, ad=ad, sig=sig), chal, ORIGIN, creds) == \
        "the kept key does not match its algorithm"


def test_a_label_is_kept_printable():
    o, chal = w.offer([], RP, may_register=True)
    cred, why = w.register({**reg_msg(chal), "name": "iPhone\x1b]0;x\x07\n" + "y" * 200}, chal, ORIGIN)
    assert why == "" and cred["name"].startswith("iPhone]0;x") and cred["name"].isprintable()
    assert len(cred["name"]) == w.NAME_LEN
    assert w.clean_label(None) == "" and w.clean_label("ok\tlabel") == "oklabel"


if __name__ == "__main__":
    for name, f in list(globals().items()):
        if name.startswith("test_"):
            f()
            print("ok", name)
