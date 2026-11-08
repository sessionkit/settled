"""RED tests for enrolling a passkey on a second host.

A passkey belongs to one RP id (a host name), so every host that serves Settings
needs its own. Before per-host enrol, webauth.offer() said "closed" to a host whose store
held only another host's passkeys (a laptop carrying a desktop's passkeys.json), and
registration was allowed only on an EMPTY store: the laptop was locked out.

Option A (the pick, designs/enrol-options.md section A): on the machine
itself, `python3 web.py enrol` (or `make enrol`) prints a one-time enrol code.
A host with no passkey for its own RP is offered mode "enrol"; the page sends
the code with the registration, and web.unlock() checks it before registering.
The code is single use, lives ENROL_TTL = 600 s, burns after ENROL_TRIES = 5
wrong tries, and only a hash of it is on disk (webauth.ENROL, 0600, beside
passkeys.json). A stranger who reaches the page without the code cannot enrol.

web.unlock() is driven directly over a FakeWS that records what it sends; a
software ES256 key plays the passkey and the unmodified webauth.register() checks it.
webauth.STORE and webauth.ENROL point at tmp paths; conftest.py sandboxes HOME.
`make` is only ever run with -n here: `make enrol` itself would write a code
under the real HOME.
Run: HOME=$(mktemp -d) uvx --with pytest --with cryptography --with websockets pytest -q tests/test_enrol.py
"""
import asyncio, base64, hashlib, json, os, re, stat, subprocess, sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from settled import web  # noqa: E402
from settled import webauth  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402

STUDIO, LAPTOP = "studio.enrol-test.ts.net", "laptop.enrol-test.ts.net"
STUDIO_ORIGIN, LAPTOP_ORIGIN = f"https://{STUDIO}", f"https://{LAPTOP}"
T0 = 1_800_000_000.0          # a fixed "now" for the clocked checks


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def run(args, **kw) -> subprocess.CompletedProcess:
    """subprocess.run, by way of Popen: tests/test_voice_browser.py swaps the shared
    subprocess.run for a fake at import (desk.subprocess IS subprocess), which would
    hand these tests an empty stdout for the rest of a full-suite run."""
    timeout = kw.pop("timeout", 30)
    with subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **kw) as p:
        try:
            out, err = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            raise
    return subprocess.CompletedProcess(args, p.returncode, out, err)


class Key:
    """A software passkey for one RP: the bytes a browser + Touch ID hand over."""

    def __init__(self, rp: str):
        self.rp = rp
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.id = b64u(os.urandom(16))

    def spki(self) -> bytes:
        return self.key.public_key().public_bytes(serialization.Encoding.DER,
                                                  serialization.PublicFormat.SubjectPublicKeyInfo)

    def cred(self, name: str) -> dict:
        """The record webauth.register() would have stored for this key."""
        return {"id": self.id, "rp_id": self.rp, "spki": b64u(self.spki()), "alg": -7,
                "added": "2030-06-09T09:00:00+1000", "name": name}

    def create(self, offer: dict, origin: str, mode: str, code=None) -> dict:
        """navigator.credentials.create over `offer`, sent as auth-finish in `mode`."""
        cd = json.dumps({"type": "webauthn.create", "challenge": offer["challenge"], "origin": origin}).encode()
        ad = hashlib.sha256(self.rp.encode()).digest() + bytes([0x05]) + (0).to_bytes(4, "big")
        m = {"type": "auth-finish", "mode": mode, "id": self.id, "alg": -7, "name": "laptop Touch ID",
             "clientDataJSON": b64u(cd), "authenticatorData": b64u(ad), "publicKey": b64u(self.spki())}
        if code is not None:
            m["code"] = code
        return m


class FakeWS:
    """Just enough websocket for web.offer()/unlock(): it records what is sent."""

    def __init__(self):
        self.sent = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    def last(self, kind: str) -> dict:
        return next(m for m in reversed(self.sent) if m.get("type") == kind)


def _clear():
    web.CODES.clear(); web.TOKENS.clear(); web.CLIENTS.clear()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A store holding only the studio's two passkeys (the file that came over to the laptop)."""
    store, enrol = tmp_path / "settings" / "passkeys.json", tmp_path / "settings" / "enrol.json"
    monkeypatch.setattr(webauth, "STORE", store)
    monkeypatch.setattr(webauth, "ENROL", enrol, raising=False)   # absent before per-host enrol
    _clear()
    mac, yubikey = Key(STUDIO), Key(STUDIO)
    webauth.save([mac.cred("Mac"), yubikey.cred("YubiKey 5C Nano")])
    yield type("Env", (), {"store": store, "enrol": enrol, "tmp": tmp_path})
    _clear()


def connect(origin: str):
    """A LOCKED connection from `origin` that has had its offer. Returns (ws, st, offer)."""
    ws = FakeWS()
    st = {"origin": origin, "authed": False, "chal": None, "token": None, "session": None}
    asyncio.run(web.offer(ws, st))
    return ws, st, ws.last("auth")


def finish(ws, st, m: dict) -> dict:
    """Send one auth-finish on a locked connection; the reply (auth-ok or auth-failed)."""
    n = len(ws.sent)
    asyncio.run(web.unlock(ws, st, m))
    return next(x for x in ws.sent[n:] if x.get("type") in ("auth-ok", "auth-failed"))


# --- webauth: the enrol code ------------------------------------------------------

def test_enrol_constants():
    assert webauth.ENROL_TTL == 600
    assert webauth.ENROL_TRIES == 5


def test_enrol_file_sits_beside_passkeys_json(tmp_path):
    """ENROL is read at import: SETTINGS_ENROL, else STORE.parent / 'enrol.json'."""
    probe = "from settled import webauth; print(webauth.ENROL)"
    env = {**os.environ, "HOME": str(tmp_path), "SETTINGS_PASSKEYS": str(tmp_path / "pk" / "passkeys.json")}
    env.pop("SETTINGS_ENROL", None)
    out = run([sys.executable, "-c", probe], cwd=ROOT, env=env)
    assert out.returncode == 0, out.stderr
    assert Path(out.stdout.strip()) == tmp_path / "pk" / "enrol.json"
    env["SETTINGS_ENROL"] = str(tmp_path / "elsewhere.json")
    out = run([sys.executable, "-c", probe], cwd=ROOT, env=env)
    assert out.returncode == 0, out.stderr
    assert Path(out.stdout.strip()) == tmp_path / "elsewhere.json"


def test_code_is_short_and_unambiguous(env):
    for _ in range(20):
        code = webauth.new_enrol_code(now=T0)
        assert isinstance(code, str) and 8 <= len(code) <= 16, code
        assert not set(code) & set("0O1lI"), code      # nothing to misread off a screen


def test_code_works_once(env):
    code = webauth.new_enrol_code(now=T0)
    assert webauth.check_enrol_code(code, now=T0 + 1) == ""
    assert webauth.check_enrol_code(code, now=T0 + 2) != ""     # consumed


def test_code_expires(env):
    code = webauth.new_enrol_code(now=T0)
    assert webauth.check_enrol_code(code, now=T0 + webauth.ENROL_TTL + 1) != ""
    code = webauth.new_enrol_code(now=T0)
    assert webauth.check_enrol_code(code, now=T0 + webauth.ENROL_TTL - 1) == ""


def test_code_burns_after_five_wrong_tries(env):
    code = webauth.new_enrol_code(now=T0)
    for i in range(webauth.ENROL_TRIES):
        assert webauth.check_enrol_code(f"WRONG{i}XYZ", now=T0 + 1) != ""
    assert webauth.check_enrol_code(code, now=T0 + 2) != ""      # burnt: even the right one fails


def test_four_wrong_tries_leave_the_code_good(env):
    code = webauth.new_enrol_code(now=T0)
    for i in range(webauth.ENROL_TRIES - 1):
        assert webauth.check_enrol_code(f"WRONG{i}XYZ", now=T0 + 1) != ""
    assert webauth.check_enrol_code(code, now=T0 + 2) == ""


def test_no_pending_code_fails(env):
    assert not env.enrol.exists()
    assert webauth.check_enrol_code("ABCDEFGHJK", now=T0) != ""
    assert webauth.check_enrol_code("", now=T0) != ""


def test_new_code_replaces_the_earlier_one(env):
    old = webauth.new_enrol_code(now=T0)
    new = webauth.new_enrol_code(now=T0)
    assert old != new
    assert webauth.check_enrol_code(old, now=T0 + 1) != ""
    assert webauth.check_enrol_code(new, now=T0 + 1) == ""


def test_only_a_hash_is_on_disk_and_the_file_is_0600(env):
    code = webauth.new_enrol_code(now=T0)
    assert webauth.ENROL == env.enrol and env.enrol.parent == env.store.parent
    assert stat.S_IMODE(os.stat(env.enrol).st_mode) == 0o600
    raw = env.enrol.read_text()
    json.loads(raw)                                     # a JSON record
    for form in {code, code.lower(), code.upper()}:
        assert form not in raw, "the enrol code itself is on disk"
    webauth.check_enrol_code("WRONG0XYZ", now=T0 + 1)   # a wrong try is recorded, still 0600, still no code
    assert stat.S_IMODE(os.stat(env.enrol).st_mode) == 0o600
    assert code not in env.enrol.read_text()


# --- webauth.offer: a second host is offered "enrol" ----------------------------------

def test_second_host_is_offered_enrol_and_m5_still_logs_in(env):
    creds = webauth.load()                             # the studio's two passkeys only
    o, chal = webauth.offer(creds, LAPTOP, may_register=False)
    assert o["mode"] == "enrol" and o["rpId"] == LAPTOP and o["allow"] == []
    assert o["challenge"] == b64u(chal) and set(o["user"]) == {"id", "name", "displayName"}
    o, _ = webauth.offer(creds, STUDIO, may_register=False)
    assert o["mode"] == "login" and len(o["allow"]) == 2


# --- web.unlock: the wire --------------------------------------------------------------

def test_wire_offers_enrol_to_the_second_host(env):
    _, _, o = connect(LAPTOP_ORIGIN)
    assert o["mode"] == "enrol" and o["rpId"] == LAPTOP
    _, _, o = connect(STUDIO_ORIGIN)
    assert o["mode"] == "login"


def test_enrol_with_the_code_registers_this_hosts_passkey_and_unlocks(env):
    ws, st, o = connect(LAPTOP_ORIGIN)
    code, key = webauth.new_enrol_code(), Key(LAPTOP)
    reply = finish(ws, st, key.create(o, LAPTOP_ORIGIN, "enrol", code))
    assert reply["type"] == "auth-ok", reply
    assert st["authed"] and st["token"] in web.TOKENS
    creds = webauth.load()
    assert len(creds) == 3
    new = next(c for c in creds if c["id"] == key.id)
    assert new["rp_id"] == LAPTOP == webauth.rp_of(LAPTOP_ORIGIN)
    assert [c["rp_id"] for c in creds if c["id"] != key.id] == [STUDIO, STUDIO]   # the studio's untouched
    assert webauth.check_enrol_code(code) != ""        # spent by the enrol


def test_enrol_code_works_once_on_the_wire(env):
    """The same code cannot enrol a second passkey, even after the laptop's is removed again."""
    ws, st, o = connect(LAPTOP_ORIGIN)
    code = webauth.new_enrol_code()
    assert finish(ws, st, Key(LAPTOP).create(o, LAPTOP_ORIGIN, "enrol", code))["type"] == "auth-ok"
    webauth.save([c for c in webauth.load() if c["rp_id"] == STUDIO])   # back to the studio's only
    ws, st, o = connect(LAPTOP_ORIGIN)
    assert o["mode"] == "enrol"
    before = env.store.read_bytes()
    assert finish(ws, st, Key(LAPTOP).create(o, LAPTOP_ORIGIN, "enrol", code))["type"] == "auth-failed"
    assert env.store.read_bytes() == before


@pytest.mark.parametrize("code", [None, "", "WRONGCODE9"], ids=["no-code", "empty-code", "wrong-code"])
def test_stranger_without_the_code_cannot_enrol(env, code):
    webauth.new_enrol_code()                           # a code IS pending; the stranger lacks it
    ws, st, o = connect(LAPTOP_ORIGIN)
    assert o["mode"] == "enrol"
    before = env.store.read_bytes()
    reply = finish(ws, st, Key(LAPTOP).create(o, LAPTOP_ORIGIN, "enrol", code))
    assert reply["type"] == "auth-failed", reply
    assert env.store.read_bytes() == before and not st["authed"]
    assert ws.last("auth")["mode"] == "enrol"          # re-offered, still locked


def test_enrol_with_no_pending_code_is_refused(env):
    ws, st, o = connect(LAPTOP_ORIGIN)
    assert o["mode"] == "enrol" and not env.enrol.exists()
    before = env.store.read_bytes()
    reply = finish(ws, st, Key(LAPTOP).create(o, LAPTOP_ORIGIN, "enrol", "ABCDEFGHJK"))
    assert reply["type"] == "auth-failed" and env.store.read_bytes() == before


def test_enrol_refused_when_this_rp_already_has_a_passkey(env):
    """Enrol is for a host with NO passkey of its own: never a way to add another.
    The refusal comes before the code is checked, so a pending code stays good."""
    webauth.save(webauth.load() + [Key(LAPTOP).cred("laptop Touch ID")])
    ws = FakeWS()
    st = {"origin": LAPTOP_ORIGIN, "authed": False, "chal": None, "token": None, "session": None}
    asyncio.run(web.offer(ws, st))
    o = ws.last("auth")
    assert o["mode"] == "login"
    o = {**o, "mode": "enrol"}                          # a forged enrol over the login challenge
    code = webauth.new_enrol_code()
    before = env.store.read_bytes()
    reply = finish(ws, st, Key(LAPTOP).create(o, LAPTOP_ORIGIN, "enrol", code))
    assert reply["type"] == "auth-failed" and env.store.read_bytes() == before
    assert webauth.check_enrol_code(code) == ""        # not consumed by the refusal


def test_register_on_a_non_empty_store_stays_refused_even_with_a_code(env):
    """Guard: mode "register" never opens a locked page; a code does not open it."""
    ws, st, o = connect(LAPTOP_ORIGIN)
    code = webauth.new_enrol_code() if hasattr(webauth, "new_enrol_code") else "ABCDEFGHJK"
    before = env.store.read_bytes()
    reply = finish(ws, st, Key(LAPTOP).create(o, LAPTOP_ORIGIN, "register", code))
    assert reply["type"] == "auth-failed" and env.store.read_bytes() == before


# --- no trust on first use ----------------------------------------------------------
# The FIRST passkey needs the enrol code too. An empty store refuses a bare register
# from every origin: Origin is set by the client, and behind `tailscale serve` a
# tailnet device can claim localhost, so no origin is trusted to register bare.

LOCAL_ORIGIN = "http://localhost:8770"
ORIGINS = {"localhost": (LOCAL_ORIGIN, "localhost"), "tailnet": (LAPTOP_ORIGIN, LAPTOP)}


@pytest.mark.parametrize("where", sorted(ORIGINS))
def test_a_fresh_empty_store_offers_enrol_never_register(env, where):
    env.store.unlink()
    origin, rp = ORIGINS[where]
    _, _, o = connect(origin)
    assert o["mode"] == "enrol" and o["rpId"] == rp and o["allow"] == []


@pytest.mark.parametrize("where", sorted(ORIGINS))
def test_a_fresh_empty_store_refuses_a_bare_register(env, where):
    """A forged register over the enrol offer, with no code: refused, nothing stored,
    the page still locked and offered enrol again."""
    env.store.unlink()
    origin, rp = ORIGINS[where]
    ws, st, o = connect(origin)
    reply = finish(ws, st, Key(rp).create({**o, "mode": "register"}, origin, "register"))
    assert reply["type"] == "auth-failed" and "settled enrol" in reply["why"], reply
    assert not env.store.exists() and webauth.load() == [] and not st["authed"]
    assert st["token"] is None and not web.TOKENS
    assert ws.last("auth")["mode"] == "enrol"


@pytest.mark.parametrize("where", sorted(ORIGINS))
def test_a_bare_register_does_not_spend_a_pending_code(env, where):
    """The refusal comes before any code is looked at: a pending code stays good."""
    env.store.unlink()
    origin, rp = ORIGINS[where]
    code = webauth.new_enrol_code()
    ws, st, o = connect(origin)
    assert finish(ws, st, Key(rp).create(o, origin, "register"))["type"] == "auth-failed"
    assert webauth.check_enrol_code(code, consume=False) == ""


@pytest.mark.parametrize("where", sorted(ORIGINS))
def test_the_first_passkey_enrols_with_a_valid_code(env, where):
    env.store.unlink()
    origin, rp = ORIGINS[where]
    ws, st, o = connect(origin)
    code, key = webauth.new_enrol_code(), Key(rp)
    asyncio.run(web.unlock(ws, st, {"type": "enrol-check", "code": code}))   # what the page sends first
    assert ws.sent[-1] == {"type": "enrol-ok"}
    reply = finish(ws, st, key.create(o, origin, "enrol", code))
    assert reply["type"] == "auth-ok", reply
    assert st["authed"] and st["token"] in web.TOKENS
    assert [(c["id"], c["rp_id"]) for c in webauth.load()] == [(key.id, rp)]
    assert webauth.check_enrol_code(code) != ""        # spent by the enrol


def test_the_first_passkey_refuses_a_spent_code(env):
    """A code enrols one passkey only: emptied again, the store will not take a second
    first passkey on the same code."""
    env.store.unlink()
    ws, st, o = connect(LOCAL_ORIGIN)
    code = webauth.new_enrol_code()
    assert finish(ws, st, Key("localhost").create(o, LOCAL_ORIGIN, "enrol", code))["type"] == "auth-ok"
    env.store.unlink()                                 # empty once more
    ws, st, o = connect(LOCAL_ORIGIN)
    assert o["mode"] == "enrol"
    reply = finish(ws, st, Key("localhost").create(o, LOCAL_ORIGIN, "enrol", code))
    assert reply["type"] == "auth-failed" and "settled enrol" in reply["why"], reply
    assert not env.store.exists() and not st["authed"]


def test_the_first_passkey_refuses_an_expired_code(env):
    import time
    env.store.unlink()
    stale = time.time() - webauth.ENROL_TTL - 1
    code = webauth.new_enrol_code(now=stale)
    ws, st, o = connect(LOCAL_ORIGIN)
    asyncio.run(web.unlock(ws, st, {"type": "enrol-check", "code": code}))   # the page's pre-check
    assert ws.sent[-1]["type"] == "enrol-failed" and "expired" in ws.sent[-1]["why"]
    code = webauth.new_enrol_code(now=stale)           # and a finish sent with no pre-check
    reply = finish(ws, st, Key("localhost").create(o, LOCAL_ORIGIN, "enrol", code))
    assert reply["type"] == "auth-failed" and "expired" in reply["why"], reply
    assert not env.store.exists() and not st["authed"]


# --- the CLI and the Makefile -------------------------------------------------------

def _candidates(out: str) -> list:
    """Tokens in the CLI's output that could be the code, likeliest first."""
    toks = {t.strip(".,:;()[]'\"`") for t in out.split()}
    toks = [t for t in toks if re.fullmatch(r"[A-Za-z0-9-]{6,20}", t)]
    return sorted(toks, key=lambda t: (not t.isupper(), not re.search(r"\d", t), t))


def test_cli_enrol_prints_a_working_code_and_exits(env):
    run_env = {**os.environ, "HOME": str(env.tmp), "SETTINGS_PASSKEYS": str(env.store),
               "SETTINGS_ENROL": str(env.enrol)}
    out = run([sys.executable, "-m", "settled.web", "enrol"], cwd=ROOT, env=run_env, timeout=30)   # a server start would hang here
    assert out.returncode == 0, out.stderr[-800:]
    assert re.search(r"expir|valid|until", out.stdout, re.I), out.stdout     # says when it runs out
    assert env.enrol.exists() and stat.S_IMODE(os.stat(env.enrol).st_mode) == 0o600
    for tok in _candidates(out.stdout)[: webauth.ENROL_TRIES - 1]:            # stay under the burn
        if webauth.check_enrol_code(tok) == "":
            return
    pytest.fail(f"no token in the CLI output is a working code: {out.stdout!r}")


def _make_n(target: str) -> subprocess.CompletedProcess:
    """`make -n`: print the recipe, run nothing (a real `make enrol` would write under HOME)."""
    return run(["make", "-n", target], cwd=ROOT, timeout=30)


def test_make_enrol_runs_the_web_module_enrol():
    out = _make_n("enrol")
    assert out.returncode == 0, out.stderr
    assert re.search(r"-m settled\.web\s+enrol", out.stdout), out.stdout


def test_make_test_runs_the_suite_under_a_scratch_home():
    out = _make_n("test")
    assert out.returncode == 0, out.stderr
    assert "mktemp -d" in out.stdout and "pytest" in out.stdout and "HOME=" in out.stdout, out.stdout


# --- the code is PRE-CHECKED before the passkey ceremony --------------------
# THE TRAP: the device made its passkey BEFORE the server saw
# the code, so a mistyped code left a dead passkey on the phone. CONTRACT 2: the page
# sends {"type": "enrol-check", "code"} first; the server checks the code WITHOUT
# consuming it (check_enrol_code(..., consume=False)) and says enrol-ok or
# enrol-failed {why}; only enrol-ok starts navigator.credentials.create(). auth-finish
# mode "enrol" still checks AND consumes, so a pre-check alone never registers.

def _wrong(path: Path) -> int:
    """The wrong-try count on disk."""
    return json.loads(path.read_text())["wrong"]


def precheck(ws, st, code) -> dict:
    """Send one enrol-check on a locked connection; its reply (enrol-ok or enrol-failed)."""
    n = len(ws.sent)
    m = {"type": "enrol-check"}
    if code is not None:
        m["code"] = code
    asyncio.run(web.unlock(ws, st, m))
    got = [x for x in ws.sent[n:] if x.get("type") in ("enrol-ok", "enrol-failed")]
    assert got, f"no enrol-ok/enrol-failed reply to enrol-check: {ws.sent[n:]}"
    return got[-1]


def test_precheck_right_code_is_not_consumed(env):
    code = webauth.new_enrol_code(now=T0)
    webauth.check_enrol_code("WRONG0XYZ", now=T0 + 1)           # one wrong try on the count
    before = env.enrol.read_bytes()
    assert webauth.check_enrol_code(code, now=T0 + 2, consume=False) == ""
    assert webauth.check_enrol_code(code, now=T0 + 3, consume=False) == ""
    assert env.enrol.read_bytes() == before and _wrong(env.enrol) == 1   # still pending, count unchanged
    assert webauth.check_enrol_code(code, now=T0 + 4) == ""      # consume=True (the default) spends it
    assert webauth.check_enrol_code(code, now=T0 + 5, consume=False) != ""


def test_precheck_wrong_codes_count_toward_the_burn(env):
    code = webauth.new_enrol_code(now=T0)
    for i in range(webauth.ENROL_TRIES):
        assert webauth.check_enrol_code(f"WRONG{i}XYZ", now=T0 + 1, consume=False) != ""
    assert webauth.check_enrol_code(code, now=T0 + 2, consume=False) != ""   # burnt
    assert webauth.check_enrol_code(code, now=T0 + 2) != ""


def test_precheck_fails_expired_and_with_no_code_pending(env):
    assert webauth.check_enrol_code("ABCDEFGHJK", now=T0, consume=False) != ""   # none pending
    code = webauth.new_enrol_code(now=T0)
    assert webauth.check_enrol_code(code, now=T0 + webauth.ENROL_TTL + 1, consume=False) != ""
    assert webauth.check_enrol_code(code, now=T0 + 1) != ""      # an expired code is gone


def test_wire_precheck_then_finish_with_the_same_code_enrols(env):
    ws, st, o = connect(LAPTOP_ORIGIN)
    code, key = webauth.new_enrol_code(), Key(LAPTOP)
    before = env.store.read_bytes()
    assert precheck(ws, st, code) == {"type": "enrol-ok"}
    assert env.store.read_bytes() == before and not st["authed"] and st["token"] is None   # never registers, never unlocks
    assert not any(x.get("type") == "auth-ok" for x in ws.sent)
    reply = finish(ws, st, key.create(o, LAPTOP_ORIGIN, "enrol", code))   # the offer's challenge is still good
    assert reply["type"] == "auth-ok", reply
    assert [c["rp_id"] for c in webauth.load()] == [STUDIO, STUDIO, LAPTOP]
    assert webauth.check_enrol_code(code) != ""                  # spent by the finish


def test_wire_wrong_prechecks_burn_the_code(env):
    ws, st, o = connect(LAPTOP_ORIGIN)
    code = webauth.new_enrol_code()
    for i in range(webauth.ENROL_TRIES):
        r = precheck(ws, st, f"WRONG{i}XYZ")
        assert r["type"] == "enrol-failed" and "wrong" in r["why"], r
    r = precheck(ws, st, code)
    assert r["type"] == "enrol-failed" and r["why"], r          # the right code, too late
    before = env.store.read_bytes()
    assert finish(ws, st, Key(LAPTOP).create(o, LAPTOP_ORIGIN, "enrol", code))["type"] == "auth-failed"
    assert env.store.read_bytes() == before


@pytest.mark.parametrize("code", [None, "", "WRONGCODE9"], ids=["no-code", "empty-code", "wrong-code"])
def test_wire_finish_still_needs_the_code_after_a_precheck(env, code):
    """A pre-check alone never registers: finish checks the code again."""
    ws, st, o = connect(LAPTOP_ORIGIN)
    good = webauth.new_enrol_code()
    assert precheck(ws, st, good)["type"] == "enrol-ok"
    before = env.store.read_bytes()
    reply = finish(ws, st, Key(LAPTOP).create(o, LAPTOP_ORIGIN, "enrol", code))
    assert reply["type"] == "auth-failed" and env.store.read_bytes() == before and not st["authed"]


def test_wire_code_expiring_between_precheck_and_finish_refuses_at_finish(env, monkeypatch):
    clock = [T0]
    monkeypatch.setattr(webauth.time, "time", lambda: clock[0])
    ws, st, o = connect(LAPTOP_ORIGIN)
    code = webauth.new_enrol_code()                              # expires at T0 + ENROL_TTL
    clock[0] = T0 + webauth.ENROL_TTL - 1
    assert precheck(ws, st, code)["type"] == "enrol-ok"
    clock[0] = T0 + webauth.ENROL_TTL + 1
    before = env.store.read_bytes()
    reply = finish(ws, st, Key(LAPTOP).create(o, LAPTOP_ORIGIN, "enrol", code))
    assert reply["type"] == "auth-failed" and "expired" in reply["why"], reply
    assert env.store.read_bytes() == before


def test_wire_code_burnt_between_precheck_and_finish_refuses_at_finish(env):
    ws, st, o = connect(LAPTOP_ORIGIN)
    code = webauth.new_enrol_code()
    assert precheck(ws, st, code)["type"] == "enrol-ok"
    for i in range(webauth.ENROL_TRIES):                         # another tab spends the tries
        webauth.check_enrol_code(f"WRONG{i}XYZ")
    before = env.store.read_bytes()
    assert finish(ws, st, Key(LAPTOP).create(o, LAPTOP_ORIGIN, "enrol", code))["type"] == "auth-failed"
    assert env.store.read_bytes() == before


@pytest.mark.parametrize("code", ["right", "WRONGCODE9"])
def test_wire_precheck_refused_when_this_rp_has_a_passkey_and_the_code_is_left_alone(env, code):
    webauth.save(webauth.load() + [Key(LAPTOP).cred("laptop Touch ID")])
    ws, st, o = connect(LAPTOP_ORIGIN)
    assert o["mode"] == "login"
    good = webauth.new_enrol_code()
    pending, store = env.enrol.read_bytes(), env.store.read_bytes()
    r = precheck(ws, st, good if code == "right" else code)
    assert r["type"] == "enrol-failed" and "has a passkey" in r["why"], r
    assert env.enrol.read_bytes() == pending and env.store.read_bytes() == store   # code untouched, no try counted
    assert not st["authed"]
    assert webauth.check_enrol_code(good) == ""


def test_wire_precheck_with_no_pending_code_fails(env):
    ws, st, _ = connect(LAPTOP_ORIGIN)
    r = precheck(ws, st, "ABCDEFGHJK")
    assert r["type"] == "enrol-failed" and "no enrol code" in r["why"], r
    assert not env.enrol.exists() and not st["authed"]
