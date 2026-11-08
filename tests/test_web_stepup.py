"""Tests for the step-up touch on every keyring change.

A live vm_session cookie unlocks the Settings websocket with no
touch. Before the step-up, any unlocked socket could then passkey-add (from the
addOffer every auth-ok carried), passkey-rename and passkey-remove, so a
same-origin script could plant a persistent passkey of its own. Now each
keyring change needs a STEP-UP GRANT: a fresh WebAuthn login assertion made on
this socket over its own single-use challenge, at most web.STEP_TTL (60s) old,
spent on exactly one change of exactly the op (and passkey id) it was asked for.

The real web.main() runs in a thread on a spare port and is driven over a real
websocket. Nothing in the auth path is mocked: a software ES256 authenticator
(as tests/smoke/e2e.py has) makes real registrations and assertions, checked by
the unmodified webauth.register()/login(). Only tailnet_name(), desk.state()
and the poll loop are stubbed, and web's own clock is skewed (web.time is
swapped for a wrapper; the real time module is untouched) to step past 60s.
SETTINGS_SESSIONS and webauth.STORE point at temp paths: never the live stores.
Run: python -m pytest -q tests/test_web_stepup.py
"""
import asyncio, base64, hashlib, json, os, socket, sys, threading, time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from settled import sessions  # noqa: E402
from settled import web  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from websockets.sync.client import connect as ws_connect  # noqa: E402

TS = "studio.stepup-test.ts.net"
HTTPS = f"https://{TS}"
WAIT = 3          # seconds to wait for a reply; a server without step-up sends none


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


class Authenticator:
    """A software passkey: an ES256 key and the bytes a browser + Touch ID hand over."""

    def __init__(self, rp: str, uv: bool = True):
        self.rp, self.uv = rp, uv
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.id = b64u(os.urandom(16))

    def spki(self) -> bytes:
        return self.key.public_key().public_bytes(serialization.Encoding.DER,
                                                  serialization.PublicFormat.SubjectPublicKeyInfo)

    def cred(self, name: str) -> dict:
        """The record webauth.register() would have stored for this key."""
        return {"id": self.id, "rp_id": self.rp, "spki": b64u(self.spki()), "alg": -7,
                "added": "2030-06-09T09:00:00+1000", "name": name}

    def _ad(self) -> bytes:
        flags = 0x05 if self.uv else 0x01          # UP|UV, or UP only (no Touch ID)
        return hashlib.sha256(self.rp.encode()).digest() + bytes([flags]) + (1).to_bytes(4, "big")

    @staticmethod
    def _cd(kind: str, chal: str, origin: str) -> bytes:
        return json.dumps({"type": kind, "challenge": chal, "origin": origin}).encode()

    def create(self, offer: dict, origin: str, kind: str = "auth-finish", name: str = "") -> dict:
        """navigator.credentials.create over `offer`: a registration message of type `kind`."""
        m = {"type": kind, "id": self.id, "alg": -7, "name": name,
             "clientDataJSON": b64u(self._cd("webauthn.create", offer["challenge"], origin)),
             "authenticatorData": b64u(self._ad()), "publicKey": b64u(self.spki())}
        if kind == "auth-finish":
            m["mode"] = "register"
        return m

    def get(self, offer: dict, origin: str, kind: str = "step-up") -> dict:
        """navigator.credentials.get over `offer`: an assertion message of type `kind`."""
        cd, ad = self._cd("webauthn.get", offer["challenge"], origin), self._ad()
        sig = self.key.sign(ad + hashlib.sha256(cd).digest(), ec.ECDSA(hashes.SHA256()))
        m = {"type": kind, "id": self.id, "clientDataJSON": b64u(cd),
             "authenticatorData": b64u(ad), "signature": b64u(sig)}
        if kind == "auth-finish":
            m["mode"] = "login"
        return m


class Clock:
    """web.py's view of time: the real module, with monotonic() skewed by `skew` seconds."""

    def __init__(self):
        self.skew = 0.0

    def monotonic(self) -> float:
        return time.monotonic() + self.skew

    def __getattr__(self, name):
        return getattr(time, name)


def spare_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _forever():
    await asyncio.Event().wait()


class Server:
    """The real web.main(port), in a thread with its own event loop."""

    def __init__(self):
        self.port = spare_port()
        self.loop = asyncio.new_event_loop()
        self.task = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.task = self.loop.create_task(web.main(self.port))
        try:
            self.loop.run_until_complete(self.task)
        except (asyncio.CancelledError, Exception):
            pass

    def start(self):
        self.thread.start()
        for _ in range(200):
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.1):
                    return self
            except OSError:
                time.sleep(0.02)
        raise RuntimeError("server did not start")

    def stop(self):
        self.loop.call_soon_threadsafe(self.task.cancel)
        self.thread.join(5)


def _clear():
    web.ORIGINS.clear(); web.CODES.clear(); web.TOKENS.clear(); web.LOG.clear(); web.CLIENTS.clear()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A server whose store holds two passkeys for the ts.net origin: the Mac's and the phone's."""
    store = tmp_path / "passkeys.json"
    monkeypatch.setenv("SETTINGS_SESSIONS", str(tmp_path / "sessions.json"))
    monkeypatch.setattr(web.webauth, "STORE", store)
    clock = Clock()
    monkeypatch.setattr(web, "time", clock)
    monkeypatch.setattr(web, "tailnet_name", lambda: TS)
    monkeypatch.setattr(web, "poll", _forever)
    monkeypatch.setattr(web.desk, "state", lambda: {"kind": "state"})
    _clear()
    web.LAST_LOOKUP = float("-inf")
    mac, phone = Authenticator(TS), Authenticator(TS)
    web.webauth.save([mac.cred("Mac"), phone.cred("iPhone")])
    srv = Server().start()
    yield SimpleNamespace(srv=srv, clock=clock, mac=mac, phone=phone, store=store,
                          local=f"http://localhost:{srv.port}")
    srv.stop()
    _clear()


# --- helpers --------------------------------------------------------------------

def open_ws(e, origin=HTTPS, cookie=None):
    headers = {"Cookie": cookie} if cookie else None
    return ws_connect(f"ws://127.0.0.1:{e.srv.port}/ws", origin=origin, additional_headers=headers,
                      open_timeout=5)


def recv(ws) -> dict:
    """The next message that is not a broadcast update."""
    while True:
        m = json.loads(ws.recv(timeout=WAIT))
        if m.get("type") != "update":
            return m


def first_two(ws) -> dict:
    assert recv(ws)["type"] == "hello"
    return recv(ws)


def cookie_ws(e):
    """A socket unlocked by a live vm_session cookie alone: no touch. Returns (ws, auth-ok)."""
    raw, _ = sessions.mint()
    ws = open_ws(e, cookie=f"vm_session={raw}")
    ok = first_two(ws)
    assert ok["type"] == "auth-ok"
    return ws, ok


def touch_ws(e, key=None, origin=HTTPS):
    """A socket unlocked by a real passkey login. Returns (ws, auth-ok)."""
    ws = open_ws(e, origin)
    offer = first_two(ws)
    assert offer["type"] == "auth" and offer["mode"] == "login"
    ws.send(json.dumps((key or e.mac).get(offer, origin, kind="auth-finish")))
    ok = recv(ws)
    assert ok["type"] == "auth-ok"
    return ws, ok


def send(ws, **m):
    ws.send(json.dumps(m))


def begin(ws, op, pid=None) -> dict:
    m = {"type": "step-up-begin", "op": op}
    if pid is not None:
        m["id"] = pid
    ws.send(json.dumps(m))
    return recv(ws)


def step_up(ws, key, op, pid=None, origin=HTTPS) -> dict:
    """A whole step-up touch for (op, pid). Returns the server's answer to the assertion."""
    offer = begin(ws, op, pid)
    assert offer["type"] == "step-up", offer
    ws.send(json.dumps(key.get(offer, origin)))
    return recv(ws)


def store_bytes(e) -> bytes:
    return e.store.read_bytes()


def names(e) -> dict:
    return {c["id"]: c["name"] for c in web.webauth.load()}


def forged_add(e, ok: dict) -> dict:
    """What a same-origin script would send to plant its own passkey: a registration
    over auth-ok's addOffer if the server hands one out, else over a made-up challenge."""
    offer = ok.get("addOffer") or {"challenge": b64u(os.urandom(32))}
    return Authenticator(TS).create(offer, HTTPS, kind="passkey-add", name="attacker")


# --- the constant ---------------------------------------------------------------

def test_step_ttl_is_sixty_seconds():
    assert web.STEP_TTL == 60.0


# --- refused without a step-up, on a cookie-authed socket: the planted-passkey hole

def test_cookie_add_refused_without_step_up(env):
    ws, ok = cookie_ws(env)
    try:
        before = store_bytes(env)
        ws.send(json.dumps(forged_add(env, ok)))
        r = recv(ws)
        assert r["type"] == "passkey-added" and r["why"]
        assert store_bytes(env) == before                      # nothing planted
    finally:
        ws.close()


def test_cookie_rename_refused_without_step_up(env):
    ws, _ = cookie_ws(env)
    try:
        before = store_bytes(env)
        send(ws, type="passkey-rename", id=env.mac.id, name="pwned")
        r = recv(ws)
        assert r["type"] == "passkey-renamed" and r["why"]
        assert store_bytes(env) == before
    finally:
        ws.close()


def test_cookie_remove_refused_without_step_up(env):
    ws, _ = cookie_ws(env)
    try:
        before = store_bytes(env)
        send(ws, type="passkey-remove", id=env.phone.id)          # not the last one: only the touch is missing
        r = recv(ws)
        assert r["type"] == "passkey-renamed" and r["why"]
        assert store_bytes(env) == before
    finally:
        ws.close()


def test_a_begin_alone_is_not_a_grant(env):
    ws, _ = cookie_ws(env)
    try:
        before = store_bytes(env)
        assert begin(ws, "rename", env.mac.id)["type"] == "step-up"
        send(ws, type="passkey-rename", id=env.mac.id, name="pwned")
        r = recv(ws)
        assert r["type"] == "passkey-renamed" and r["why"]
        assert store_bytes(env) == before
    finally:
        ws.close()


def test_a_touch_unlocked_socket_also_needs_the_step_up(env):
    ws, _ = touch_ws(env)
    try:
        before = store_bytes(env)
        send(ws, type="passkey-rename", id=env.mac.id, name="no touch")
        r = recv(ws)
        assert r["type"] == "passkey-renamed" and r["why"]
        assert store_bytes(env) == before
    finally:
        ws.close()


# --- no add offer without a step-up -----------------------------------------------

@pytest.mark.parametrize("how", ["cookie", "token", "touch"])
def test_auth_ok_carries_no_add_offer(env, how):
    if how == "cookie":
        ws, ok = cookie_ws(env)
    elif how == "touch":
        ws, ok = touch_ws(env)
    else:                                                       # localhost: touch once, then the tab token resumes
        local = Authenticator("localhost")
        web.webauth.save(web.webauth.load() + [local.cred("Mac (localhost)")])
        ws0, ok0 = touch_ws(env, local, env.local)
        assert "addOffer" not in ok0
        ws0.close()
        ws = open_ws(env, env.local)
        assert first_two(ws)["type"] == "auth"
        send(ws, type="resume", token=ok0["token"])
        ok = recv(ws)
        assert ok["type"] == "auth-ok"
    try:
        assert "addOffer" not in ok
        assert [p["id"] for p in ok["passkeys"]][:2] == [env.mac.id, env.phone.id]
    finally:
        ws.close()


def test_the_add_offer_comes_only_with_step_up_ok_for_add(env):
    ws, _ = cookie_ws(env)
    try:
        r = step_up(ws, env.mac, "rename", env.mac.id)
        assert r["type"] == "step-up-ok" and "addOffer" not in r
        send(ws, type="passkey-rename", id=env.mac.id, name="Mac mini")
        r = recv(ws)
        assert r["type"] == "passkey-renamed" and not r.get("why") and "addOffer" not in r
        r = step_up(ws, env.mac, "add")
        assert r["type"] == "step-up-ok" and r["op"] == "add"
        o = r["addOffer"]
        assert o["mode"] == "register" and o["rpId"] == TS and o["challenge"]
        assert sorted(o["exclude"]) == sorted([env.mac.id, env.phone.id])   # no second copy of a key we have
    finally:
        ws.close()


# --- with a fresh step-up each succeeds ----------------------------------------------

def test_step_up_offer_is_a_login_for_this_rp(env):
    ws, _ = cookie_ws(env)
    try:
        o = begin(ws, "remove", env.phone.id)
        assert o["type"] == "step-up" and o["op"] == "remove" and o["id"] == env.phone.id
        assert o["rpId"] == TS and o["challenge"]
        assert sorted(o["allow"]) == sorted([env.mac.id, env.phone.id])
    finally:
        ws.close()


def test_add_with_a_fresh_step_up_and_the_name_rides_on_it(env):
    ws, _ = cookie_ws(env)
    try:
        r = step_up(ws, env.mac, "add")
        assert r["type"] == "step-up-ok"
        yubi = Authenticator(TS)
        ws.send(json.dumps(yubi.create(r["addOffer"], HTTPS, kind="passkey-add", name="YubiKey")))
        r = recv(ws)
        assert r["type"] == "passkey-added" and not r["why"]
        assert names(env)[yubi.id] == "YubiKey"                # named at the add: no rename follows
        assert "addOffer" not in r
        assert yubi.id in [p["id"] for p in r["passkeys"]]
    finally:
        ws.close()


def test_rename_with_a_fresh_step_up(env):
    ws, _ = cookie_ws(env)
    try:
        assert step_up(ws, env.phone, "rename", env.mac.id)["type"] == "step-up-ok"   # any of his keys may confirm
        send(ws, type="passkey-rename", id=env.mac.id, name="Mac Studio")
        r = recv(ws)
        assert r["type"] == "passkey-renamed" and not r.get("why")
        assert names(env) == {env.mac.id: "Mac Studio", env.phone.id: "iPhone"}
    finally:
        ws.close()


def test_remove_with_a_fresh_step_up(env):
    ws, _ = cookie_ws(env)
    try:
        assert step_up(ws, env.mac, "remove", env.phone.id)["type"] == "step-up-ok"
        send(ws, type="passkey-remove", id=env.phone.id)
        r = recv(ws)
        assert r["type"] == "passkey-renamed" and not r["why"]
        assert names(env) == {env.mac.id: "Mac"}
    finally:
        ws.close()


def test_the_last_passkey_is_still_kept_after_a_step_up(env):
    web.webauth.save([env.mac.cred("Mac")])
    ws, _ = cookie_ws(env)
    try:
        assert step_up(ws, env.mac, "remove", env.mac.id)["type"] == "step-up-ok"
        send(ws, type="passkey-remove", id=env.mac.id)
        r = recv(ws)
        assert r["type"] == "passkey-renamed" and "last passkey" in r["why"]
        assert names(env) == {env.mac.id: "Mac"}
    finally:
        ws.close()


def test_a_step_up_59s_after_the_begin_still_counts(env):
    ws, _ = cookie_ws(env)
    try:
        o = begin(ws, "rename", env.mac.id)
        env.clock.skew += 59
        ws.send(json.dumps(env.mac.get(o, HTTPS)))
        assert recv(ws)["type"] == "step-up-ok"
        env.clock.skew += 59                                   # and the grant, 59s on
        send(ws, type="passkey-rename", id=env.mac.id, name="late but fine")
        assert not recv(ws).get("why")
        assert names(env)[env.mac.id] == "late but fine"
    finally:
        ws.close()


# --- stale, replayed, wrong ---------------------------------------------------------

def test_a_stale_assertion_is_refused(env):
    ws, _ = cookie_ws(env)
    try:
        before = store_bytes(env)
        o = begin(ws, "rename", env.mac.id)
        env.clock.skew += 61                                   # the user tapped a minute and a bit later
        ws.send(json.dumps(env.mac.get(o, HTTPS)))
        r = recv(ws)
        assert r["type"] == "step-up-failed" and "stale" in r["why"]
        send(ws, type="passkey-rename", id=env.mac.id, name="pwned")
        assert recv(ws)["why"]
        assert store_bytes(env) == before
    finally:
        ws.close()


def test_a_replayed_step_up_is_refused(env):
    ws, _ = cookie_ws(env)
    try:
        o = begin(ws, "rename", env.mac.id)
        assertion = env.mac.get(o, HTTPS)
        ws.send(json.dumps(assertion))
        assert recv(ws)["type"] == "step-up-ok"
        send(ws, type="passkey-rename", id=env.mac.id, name="once")
        assert not recv(ws).get("why")
        ws.send(json.dumps(assertion))                         # the same bytes again
        r = recv(ws)
        assert r["type"] == "step-up-failed"
        send(ws, type="passkey-rename", id=env.mac.id, name="twice")
        assert recv(ws)["why"]
        assert names(env)[env.mac.id] == "once"
    finally:
        ws.close()


def test_an_assertion_from_another_socket_is_refused(env):
    a, _ = cookie_ws(env)
    b, _ = cookie_ws(env)
    try:
        before = store_bytes(env)
        stolen = env.mac.get(begin(a, "rename", env.mac.id), HTTPS)   # made for socket a
        assert begin(b, "rename", env.mac.id)["type"] == "step-up"     # b's own challenge
        b.send(json.dumps(stolen))
        assert recv(b)["type"] == "step-up-failed"
        send(b, type="passkey-rename", id=env.mac.id, name="pwned")
        assert recv(b)["why"]
        assert store_bytes(env) == before
    finally:
        a.close(); b.close()


def test_a_new_begin_replaces_the_earlier_challenge(env):
    ws, _ = cookie_ws(env)
    try:
        first = begin(ws, "rename", env.mac.id)
        assert begin(ws, "rename", env.mac.id)["challenge"] != first["challenge"]
        ws.send(json.dumps(env.mac.get(first, HTTPS)))
        assert recv(ws)["type"] == "step-up-failed"
    finally:
        ws.close()


def test_a_failed_step_up_spends_its_challenge(env):
    ws, _ = cookie_ws(env)
    try:
        o = begin(ws, "rename", env.mac.id)
        forger = Authenticator(TS)
        forger.id = env.mac.id                                 # claims to be the Mac's key, signs with another
        ws.send(json.dumps(forger.get(o, HTTPS)))
        r = recv(ws)
        assert r["type"] == "step-up-failed" and "signature" in r["why"]
        ws.send(json.dumps(env.mac.get(o, HTTPS)))              # the real key, same (spent) challenge
        assert recv(ws)["type"] == "step-up-failed"
    finally:
        ws.close()


def test_a_step_up_without_user_verification_is_refused(env):
    ws, _ = cookie_ws(env)
    try:
        nouv = Authenticator(TS, uv=False)
        web.webauth.save(web.webauth.load() + [nouv.cred("no UV")])
        r = step_up(ws, nouv, "rename", env.mac.id)
        assert r["type"] == "step-up-failed" and "verification" in r["why"]
    finally:
        ws.close()


def test_a_step_up_with_an_unknown_key_is_refused(env):
    ws, _ = cookie_ws(env)
    try:
        r = step_up(ws, Authenticator(TS), "add")
        assert r["type"] == "step-up-failed" and "addOffer" not in r
    finally:
        ws.close()


def test_a_grant_older_than_60s_is_refused(env):
    ws, _ = cookie_ws(env)
    try:
        before = store_bytes(env)
        assert step_up(ws, env.mac, "rename", env.mac.id)["type"] == "step-up-ok"
        env.clock.skew += 61
        send(ws, type="passkey-rename", id=env.mac.id, name="too late")
        r = recv(ws)
        assert r["type"] == "passkey-renamed" and r["why"]
        assert store_bytes(env) == before
    finally:
        ws.close()


def test_a_stale_add_grant_is_refused(env):
    ws, _ = cookie_ws(env)
    try:
        before = store_bytes(env)
        r = step_up(ws, env.mac, "add")
        env.clock.skew += 61
        ws.send(json.dumps(Authenticator(TS).create(r["addOffer"], HTTPS, kind="passkey-add", name="late")))
        r = recv(ws)
        assert r["type"] == "passkey-added" and r["why"]
        assert store_bytes(env) == before
    finally:
        ws.close()


def test_a_grant_is_single_use(env):
    ws, _ = cookie_ws(env)
    try:
        assert step_up(ws, env.mac, "rename", env.mac.id)["type"] == "step-up-ok"
        send(ws, type="passkey-rename", id=env.mac.id, name="first")
        assert not recv(ws).get("why")
        send(ws, type="passkey-rename", id=env.mac.id, name="second")
        r = recv(ws)
        assert r["type"] == "passkey-renamed" and r["why"]
        assert names(env)[env.mac.id] == "first"
    finally:
        ws.close()


def test_an_add_grant_is_single_use(env):
    ws, _ = cookie_ws(env)
    try:
        r = step_up(ws, env.mac, "add")
        offer = r["addOffer"]
        yubi, extra = Authenticator(TS), Authenticator(TS)
        ws.send(json.dumps(yubi.create(offer, HTTPS, kind="passkey-add", name="YubiKey")))
        assert not recv(ws)["why"]
        after_one = store_bytes(env)
        ws.send(json.dumps(extra.create(offer, HTTPS, kind="passkey-add", name="one more")))
        r = recv(ws)
        assert r["type"] == "passkey-added" and r["why"]
        assert store_bytes(env) == after_one
    finally:
        ws.close()


@pytest.mark.parametrize("granted,attempt", [
    (("rename", "mac"), ("passkey-remove", "phone")),      # another op, another id
    (("rename", "mac"), ("passkey-remove", "mac")),        # another op, same id
    (("rename", "mac"), ("passkey-rename", "phone")),      # same op, another id
    (("remove", "phone"), ("passkey-remove", "mac")),
    (("add", None), ("passkey-rename", "mac")),
    (("add", None), ("passkey-remove", "phone")),
    (("rename", "mac"), ("passkey-add", None)),
])
def test_a_grant_for_one_op_or_id_does_not_cover_another(env, granted, attempt):
    ws, _ = cookie_ws(env)
    try:
        key_of = {"mac": env.mac.id, "phone": env.phone.id, None: None}
        op, who = granted
        assert step_up(ws, env.mac, op, key_of[who])["type"] == "step-up-ok"
        before = store_bytes(env)
        kind, target = attempt
        if kind == "passkey-add":
            ws.send(json.dumps(forged_add(env, {})))
        else:
            send(ws, type=kind, id=key_of[target], name="pwned")
        r = recv(ws)
        assert r["why"]
        assert store_bytes(env) == before
        if op != "add":                                       # and the refused attempt spent it
            send(ws, type=f"passkey-{op}", id=key_of[who], name="after")
            assert recv(ws)["why"]
            assert store_bytes(env) == before
    finally:
        ws.close()


@pytest.mark.parametrize("m", [{"op": "bogus"}, {"op": "rename"}, {"op": "remove", "id": ""}, {}])
def test_a_begin_for_no_real_change_is_refused(env, m):
    ws, _ = cookie_ws(env)
    try:
        ws.send(json.dumps({"type": "step-up-begin", **m}))
        r = recv(ws)
        assert r["type"] == "step-up-failed" and r["why"]
    finally:
        ws.close()


def test_no_passkey_for_this_origin_means_no_step_up(env):
    web.webauth.save([Authenticator("localhost").cred("elsewhere")])
    ws, _ = cookie_ws(env)
    try:
        r = begin(ws, "add")
        assert r["type"] == "step-up-failed" and r["why"]
    finally:
        ws.close()


def test_a_locked_socket_cannot_step_up(env):
    ws = open_ws(env)
    try:
        assert first_two(ws)["type"] == "auth"
        send(ws, type="step-up-begin", op="add")               # ignored: it waits for the lock
        send(ws, type="passkey-rename", id=env.mac.id, name="pwned")
        with pytest.raises(TimeoutError):
            recv(ws)
        assert names(env)[env.mac.id] == "Mac"
    finally:
        ws.close()


def test_step_ups_are_logged(env, capsys):
    ws, _ = cookie_ws(env)
    try:
        step_up(ws, env.mac, "rename", env.mac.id)
        step_up(ws, Authenticator(TS), "rename", env.mac.id)
    finally:
        ws.close()
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines() if x.startswith("{")]
    ups = [x for x in lines if x.get("auth") == "step-up"]
    assert [x["result"] for x in ups] == ["ok", "unknown passkey"]
    assert all(x["origin"] == HTTPS for x in ups)


# --- no trust on first use -------------------------------------------------------
# Over the real websocket: an empty store never registers bare, from localhost or the
# tailnet's https name; the first passkey is enrolled with the `settled enrol` code.

@pytest.mark.parametrize("where", ["localhost", "tailnet"])
def test_an_empty_store_refuses_a_bare_register_over_the_wire(env, where):
    env.store.unlink()                                        # no passkey at all
    origin, rp = (env.local, "localhost") if where == "localhost" else (HTTPS, TS)
    ws = open_ws(env, origin)
    try:
        offer = first_two(ws)
        assert offer["type"] == "auth" and offer["mode"] == "enrol"   # never "register"
        ws.send(json.dumps(Authenticator(rp).create(offer, origin, name="Mac")))   # a bare register
        r = recv(ws)
        assert r["type"] == "auth-failed" and "settled enrol" in r["why"], r
        assert recv(ws)["mode"] == "enrol"                    # re-offered, still locked
        assert not env.store.exists() and web.webauth.load() == []
    finally:
        ws.close()


def test_the_first_passkey_enrols_with_the_code_over_the_wire(env, monkeypatch):
    env.store.unlink()
    monkeypatch.setattr(web.webauth, "ENROL", env.store.parent / "enrol.json")
    code = web.webauth.new_enrol_code()
    ws = open_ws(env)
    try:
        offer = first_two(ws)
        assert offer["type"] == "auth" and offer["mode"] == "enrol"
        send(ws, type="enrol-check", code=code)
        assert recv(ws)["type"] == "enrol-ok"
        first = Authenticator(TS)
        ws.send(json.dumps({**first.create(offer, HTTPS, name="Mac"), "mode": "enrol", "code": code}))
        ok = recv(ws)
        assert ok["type"] == "auth-ok" and "addOffer" not in ok
        assert names(env) == {first.id: "Mac"}
    finally:
        ws.close()
