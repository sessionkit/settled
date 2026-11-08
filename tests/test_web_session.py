"""Tests for web.py's vm_session exchange and cookie-on-connect.

A websocket message cannot set a cookie, so an https auth-ok carries a one-time
code and the page swaps it at GET /auth/session for Set-Cookie: vm_session.
A connection that arrives with a live vm_session is authed on connect, with no
passkey offer. localhost (http) keeps its per-tab token flow, with no code.

The real main() runs in a thread on a spare port: real websockets client, real
HTTP GETs, the raw Set-Cookie header. webauth.login is mocked (no passkey), and
tailnet_name(), desk.state() and the poll loop are stubbed. SETTINGS_PASSKEYS
and SETTINGS_SESSIONS point at temp paths: the live stores are never touched.
Run: python -m pytest -q tests/test_web_session.py
"""
import asyncio, http.client, json, os, socket, subprocess, sys, tempfile, textwrap, threading, time
from pathlib import Path

import pytest

TMP = Path(tempfile.mkdtemp(prefix="settings-websess-"))
os.environ.setdefault("SETTINGS_PASSKEYS", str(TMP / "passkeys.json"))
os.environ["SETTINGS_SESSIONS"] = str(TMP / "sessions.json")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from settled import sessions  # noqa: E402
from settled import web  # noqa: E402
from websockets.sync.client import connect as ws_connect  # noqa: E402

TS = "studio.example.ts.net"
HTTPS = f"https://{TS}"
REAL = (web.tailnet_name, web.poll, web.desk.state, web.webauth.login)


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


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("SETTINGS_SESSIONS", str(tmp_path / "sessions.json"))
    web.tailnet_name = lambda: TS
    web.poll = _forever
    web.desk.state = lambda: {"kind": "state"}
    web.webauth.login = lambda m, chal, origin, creds: ""      # the touch, mocked: always good
    web.ORIGINS.clear(); web.CODES.clear(); web.TOKENS.clear(); web.LOG.clear(); web.CLIENTS.clear()
    web.LAST_LOOKUP = float("-inf")
    srv = Server().start()
    yield srv
    srv.stop()
    web.tailnet_name, web.poll, web.desk.state, web.webauth.login = REAL
    web.ORIGINS.clear(); web.CODES.clear(); web.TOKENS.clear(); web.LOG.clear(); web.CLIENTS.clear()


def open_ws(srv, origin=HTTPS, cookie=None):
    headers = {"Cookie": cookie} if cookie else None
    return ws_connect(f"ws://127.0.0.1:{srv.port}/ws", origin=origin, additional_headers=headers,
                      open_timeout=5)


def recv(ws):
    return json.loads(ws.recv(timeout=5))


def first_two(ws):
    """hello, then either the passkey offer (type auth) or auth-ok."""
    hello = recv(ws)
    assert hello["type"] == "hello"
    return recv(ws)


def login(srv, origin=HTTPS):
    """A passkey login on `origin` (webauth.login mocked). Returns (ws, auth-ok)."""
    ws = open_ws(srv, origin)
    assert first_two(ws)["type"] == "auth"
    ws.send(json.dumps({"type": "auth-finish", "mode": "login"}))
    ok = recv(ws)
    assert ok["type"] == "auth-ok"
    return ws, ok


def get(srv, path):
    """A real HTTP GET. Returns (status, Set-Cookie headers (raw), body)."""
    c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
    try:
        c.request("GET", path)
        r = c.getresponse()
        body = r.read()
        return r.status, r.headers.get_all("Set-Cookie") or [], body, r.headers
    finally:
        c.close()


def cookie_of(set_cookie: str) -> str:
    return set_cookie.split(";", 1)[0].split("=", 1)[1]


EXACT = "vm_session={}; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=" + str(sessions.TTL)


# --- the code at auth-ok ------------------------------------------------------

def test_https_auth_ok_carries_a_code_bound_to_that_ws(server):
    ws, ok = login(server)
    try:
        code = ok["code"]
        assert isinstance(code, str) and len(code) >= 40       # 32 random bytes, url-safe
        assert web.CODES[code]["expires"] - time.monotonic() <= web.CODE_TTL
        assert web.CODES[code]["st"]["origin"] == HTTPS
    finally:
        ws.close()


def test_localhost_auth_ok_has_no_code_and_keeps_the_token_flow(server):
    local = f"http://localhost:{server.port}"
    ws, ok = login(server, local)
    try:
        assert "code" not in ok
        assert web.TOKENS[ok["token"]] == local                # the per-tab token, as before
        assert web.CODES == {}
    finally:
        ws.close()
    ws2 = open_ws(server, local)                               # a reload resumes with the token
    try:
        assert first_two(ws2)["type"] == "auth"
        ws2.send(json.dumps({"type": "resume", "token": ok["token"]}))
        ok2 = recv(ws2)
        assert ok2["type"] == "auth-ok" and "code" not in ok2
    finally:
        ws2.close()


def test_localhost_ignores_a_cookie_and_offers_the_passkey(server):
    raw, _ = sessions.mint()
    ws = open_ws(server, f"http://localhost:{server.port}", cookie=f"vm_session={raw}")
    try:
        assert first_two(ws)["type"] == "auth"
    finally:
        ws.close()


# --- GET /auth/session --------------------------------------------------------

def test_good_code_sets_exactly_the_cookie(server):
    ws, ok = login(server)
    try:
        status, cookies, body, headers = get(server, f"/auth/session?code={ok['code']}")
        assert status == 200
        assert len(cookies) == 1
        raw = cookie_of(cookies[0])
        assert cookies[0] == EXACT.format(raw)                 # the raw header, attribute for attribute
        assert "domain" not in cookies[0].lower()
        assert headers["Cache-Control"] == "no-store"
        assert json.loads(body) == {"ok": True, "return": None}
        assert sessions.lookup(raw)["user"] == "owner"          # a live session in the store
        assert raw not in Path(os.environ["SETTINGS_SESSIONS"]).read_text()
    finally:
        ws.close()


def test_the_code_is_single_use(server):
    ws, ok = login(server)
    try:
        assert get(server, f"/auth/session?code={ok['code']}")[0] == 200
        status, cookies, _, _ = get(server, f"/auth/session?code={ok['code']}")
        assert (status, cookies) == (401, [])
        assert len(sessions.load()) == 1
    finally:
        ws.close()


@pytest.mark.parametrize("query", ["", "?code=", "?code=forged", "?code=a&code=b", "?return=/talk/"])
def test_bad_code_is_401_and_no_cookie(server, query):
    status, cookies, _, _ = get(server, "/auth/session" + query)
    assert (status, cookies) == (401, [])
    assert sessions.load() == {}


def test_expired_code_is_401(server):
    ws, ok = login(server)
    try:
        web.CODES[ok["code"]]["expires"] = time.monotonic() - 0.1   # 30s have passed
        status, cookies, _, _ = get(server, f"/auth/session?code={ok['code']}")
        assert (status, cookies) == (401, [])
        assert sessions.load() == {}
    finally:
        ws.close()


def test_a_closed_ws_drops_its_code(server):
    ws, ok = login(server)
    ws.close()
    for _ in range(100):                                       # the server's finally runs
        if ok["code"] not in web.CODES:
            break
        time.sleep(0.02)
    status, cookies, _, _ = get(server, f"/auth/session?code={ok['code']}")
    assert (status, cookies) == (401, [])
    assert sessions.load() == {}


def test_return_is_echoed(server):
    ws, ok = login(server)
    try:
        status, cookies, body, _ = get(server, f"/auth/session?code={ok['code']}&return=/talk/")
        assert status == 200 and len(cookies) == 1
        assert json.loads(body) == {"ok": True, "return": "/talk/"}
    finally:
        ws.close()


@pytest.mark.parametrize("ret", ["//evil", "https://x", "%2F%2Fevil", "/%5Cevil.com", "javascript:x",
                                 "/talk/&return=//evil"])
def test_a_refused_return_is_400_and_mints_nothing(server, ret):
    ws, ok = login(server)
    try:
        q = f"/auth/session?code={ok['code']}&return={ret}"
        if ret.startswith("/talk/&"):
            q = f"/auth/session?code={ok['code']}&return=/talk/&return=//evil"   # two returns: refused
        status, cookies, body, _ = get(server, q)
        assert (status, cookies) == (400, [])
        assert sessions.load() == {}
        assert ok["code"] not in web.CODES                     # the code is still consumed
        assert get(server, f"/auth/session?code={ok['code']}")[0] == 401
    finally:
        ws.close()


def test_static_files_still_served(server):
    status, cookies, body, _ = get(server, "/webxdc.js")
    assert status == 200 and cookies == [] and body
    assert get(server, "/auth/nope")[0] == 404


# --- cookie on connect ----------------------------------------------------------

def test_a_live_cookie_skips_the_passkey_offer(server):
    ws, ok = login(server)
    _, cookies, _, _ = get(server, f"/auth/session?code={ok['code']}")
    ws.close()
    raw = cookie_of(cookies[0])
    ws2 = open_ws(server, cookie=f"theme=dark; vm_session={raw}; other=1")
    try:
        second = first_two(ws2)
        assert second["type"] == "auth-ok"                     # no {"type": "auth"} before it
        assert second["user"] == "owner" and "passkeys" in second and "code" in second
        ws2.send(json.dumps({"type": "since", "serial": 0}))   # and it is really unlocked
        types = []
        while not types or types[-1] != "caught-up":
            types.append(recv(ws2)["type"])
        assert "update" in types
    finally:
        ws2.close()


@pytest.mark.parametrize("cookie", ["vm_session=forged", "vm_session=", "other=1", "vm_session=EXPIRED"])
def test_a_dead_or_forged_cookie_falls_back_to_the_offer(server, cookie):
    if cookie == "vm_session=EXPIRED":
        raw, _ = sessions.mint(now=time.time() - sessions.TTL - 1)
        cookie = f"vm_session={raw}"
    ws = open_ws(server, cookie=cookie)
    try:
        assert first_two(ws)["type"] == "auth"
    finally:
        ws.close()


def test_a_revoked_cookie_falls_back_to_the_offer(server):
    raw, _ = sessions.mint()
    sessions.revoke(raw)
    ws = open_ws(server, cookie=f"vm_session={raw}")
    try:
        assert first_two(ws)["type"] == "auth"
    finally:
        ws.close()


def test_a_code_on_a_cookie_authed_ws_mints_nothing(server):
    """Sign in from Talk when already signed in: bounce straight back, no touch."""
    raw, _ = sessions.mint()
    ws = open_ws(server, cookie=f"vm_session={raw}")
    try:
        ok = first_two(ws)
        assert ok["type"] == "auth-ok"
        status, cookies, body, _ = get(server, f"/auth/session?code={ok['code']}&return=/talk/")
        assert (status, cookies) == (200, [])
        assert json.loads(body) == {"ok": True, "return": "/talk/"}
        assert len(sessions.load()) == 1                       # still only the one session
    finally:
        ws.close()


def test_a_code_on_a_cookie_authed_ws_does_not_revive_a_revoked_session(server):
    raw, _ = sessions.mint()
    ws = open_ws(server, cookie=f"vm_session={raw}")
    try:
        ok = first_two(ws)
        sessions.revoke(raw)                                   # signed out elsewhere meanwhile
        status, cookies, _, _ = get(server, f"/auth/session?code={ok['code']}")
        assert (status, cookies) == (401, [])
        assert sessions.load() == {}
    finally:
        ws.close()


def test_handler_cookie_header_split_by_a_proxy():
    """Two Cookie lines (as a proxy may send them) still find vm_session."""
    from websockets.datastructures import Headers
    h = Headers()
    h["Cookie"] = "a=1"
    h["Cookie"] = "vm_session=abc"
    assert sessions.cookie_value(web.cookie_header(h)) == "abc"
    assert web.cookie_header(Headers()) == ""
    assert web.cookie_header({"Cookie": "vm_session=x"}) == "vm_session=x"


# --- restart --------------------------------------------------------------------

def test_a_session_survives_a_restart_new_process(server):
    """Minted by this server; a NEW process (fresh web module) reading the same
    SETTINGS_SESSIONS accepts it on connect with no passkey offer."""
    ws, ok = login(server)
    _, cookies, _, _ = get(server, f"/auth/session?code={ok['code']}")
    ws.close()
    raw = cookie_of(cookies[0])
    script = textwrap.dedent(f"""
        import asyncio, json, sys
        sys.path.insert(0, {str(ROOT)!r})
        from settled import web
        web.desk.state = lambda: {{"kind": "state"}}
        web.ORIGINS.add({HTTPS!r})
        class WS:
            def __init__(self):
                self.request = type("R", (), {{"headers": {{"Origin": {HTTPS!r},
                                                            "Cookie": "vm_session=" + sys.argv[1]}}}})()
                self.sent = []
            async def send(self, t): self.sent.append(json.loads(t)["type"])
            def __aiter__(self): return self
            async def __anext__(self): raise StopAsyncIteration
        ws = WS()
        asyncio.run(web.handler(ws))
        print(json.dumps(ws.sent))
    """)
    env = dict(os.environ, SETTINGS_SESSIONS=os.environ["SETTINGS_SESSIONS"],
               SETTINGS_PASSKEYS=os.environ["SETTINGS_PASSKEYS"])

    def fresh(cookie):     # Popen, not run: test_voice_browser.py fakes subprocess.run module-wide
        p = subprocess.Popen([sys.executable, "-c", script, cookie], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, env=env)
        out, err = p.communicate(timeout=30)
        assert p.returncode == 0, err
        return json.loads(out.strip().splitlines()[-1])

    assert fresh(raw) == ["hello", "auth-ok"]
    assert fresh("forged") == ["hello", "auth"]
