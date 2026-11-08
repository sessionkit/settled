"""Tests for vm_check.check(): the optional stdlib client for GET /auth/check.

The 200/401 cases run against the real web.main() on a spare port (test_web_session's
fixture: webauth.login mocked, temp SETTINGS_SESSIONS/PASSKEYS, the live stores never
touched). The fail-closed cases (refused, timeout, 5xx, bad bodies, redirect) run
against tiny stdlib stub servers on spare ports. Named test_web_* on purpose: it imports
web (and so desk), and must collect AFTER test_voice_browser.py, which points desk at a
temp dir before its first import. Run: python -m pytest -q tests/test_web_vm_check.py
"""
import http.server, json, socket, sys, threading, time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from settled import vm_check  # noqa: E402
from settled.vm_check import check  # noqa: E402

from test_web_check import signed_in  # noqa: E402
from test_web_session import server, sessions, spare_port  # noqa: E402,F401


def url(srv):
    return f"http://127.0.0.1:{srv.port}/auth/check"


# --- against a live web.py -------------------------------------------------------

def test_live_cookie_returns_the_owner(server):
    ws, raw = signed_in(server)
    ws.close()
    assert check(f"theme=dark; vm_session={raw}; x=1", url=url(server)) == "owner"


def test_no_cache_a_sign_out_is_seen_at_once(server):
    ws, raw = signed_in(server)
    ws.close()
    hdr = f"vm_session={raw}"
    assert check(hdr, url=url(server)) == "owner"
    assert sessions.revoke(raw)
    assert check(hdr, url=url(server)) is None          # the very next call asks again


def test_signed_out_via_http_sign_out_is_none(server):
    ws, raw = signed_in(server)
    ws.close()
    import http.client
    c = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    c.request("GET", "/auth/sign-out", headers={"Cookie": f"vm_session={raw}"})
    assert c.getresponse().status == 200
    c.close()
    assert check(f"vm_session={raw}", url=url(server)) is None


@pytest.mark.parametrize("hdr", [
    "theme=dark; other=1",                 # no vm_session at all
    "vm_session=" + "A" * 43,              # forged
    "vm_session=",                         # empty value
])
def test_401_is_none(server, hdr):
    sessions.mint()                        # a live session exists, but not this one
    assert check(hdr, url=url(server)) is None


def test_expired_is_none(server):
    raw, _ = sessions.mint(now=time.time() - sessions.TTL - 1)
    assert check(f"vm_session={raw}", url=url(server)) is None


@pytest.mark.parametrize("hdr", [None, "", b"vm_session=x", 42])
def test_no_header_is_none_without_asking(hdr, monkeypatch):
    monkeypatch.setattr(vm_check._OPENER, "open", lambda *a, **k: pytest.fail("asked Settings"))
    assert check(hdr) is None


def test_header_with_crlf_is_none(server):
    ws, raw = signed_in(server)
    ws.close()
    assert check(f"vm_session={raw}\r\nX-Evil: 1", url=url(server)) is None


# --- fail closed: stub servers ------------------------------------------------------

class Stub:
    """A one-route stdlib HTTP server on a spare port answering (status, body, headers).
    Records the Cookie header of every request it saw."""

    def __init__(self, status=200, body=b'{"user": "owner"}', headers=None):
        stub = self
        self.seen = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                stub.seen.append(self.headers.get("Cookie"))
                self.send_response(status)
                for k, v in (headers or {"Content-Type": "application/json"}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.httpd = http.server.HTTPServer(("127.0.0.1", spare_port()), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def stub():
    made = []

    def make(*a, **k):
        s = Stub(*a, **k)
        made.append(s)
        return s
    yield make
    for s in made:
        s.close()


def test_forwards_the_header_exactly_as_given(stub):
    s = stub()
    hdr = "theme=dark;  vm_session=abc-_123 ; x=\"q\""
    assert check(hdr, url=url(s)) == "owner"
    assert s.seen == [hdr]


def test_refused_connection_is_none():
    port = spare_port()                    # nothing listens here
    t = time.monotonic()
    assert check("vm_session=x", url=f"http://127.0.0.1:{port}/auth/check") is None
    assert time.monotonic() - t < 1.0


def test_timeout_is_none_and_bounded():
    lsock = socket.socket()                # accepts (kernel backlog) but never answers
    lsock.bind(("127.0.0.1", 0))
    lsock.listen(5)
    try:
        t = time.monotonic()
        assert check("vm_session=x", url=f"http://127.0.0.1:{lsock.getsockname()[1]}/auth/check",
                     timeout=0.3) is None
        assert time.monotonic() - t < 1.0
        t = time.monotonic()               # the default is ~1s
        assert check("vm_session=x", url=f"http://127.0.0.1:{lsock.getsockname()[1]}/auth/check") is None
        assert 0.8 < time.monotonic() - t < 2.0
    finally:
        lsock.close()


@pytest.mark.parametrize("status", [500, 502, 503, 401, 403, 404, 204])
def test_non_200_is_none(stub, status):
    assert check("vm_session=x", url=url(stub(status=status))) is None


def test_redirect_is_not_followed(stub):
    target = stub()                        # would name the owner if the redirect were followed
    s = stub(status=302, body=b"", headers={"Location": url(target)})
    assert check("vm_session=x", url=url(s)) is None
    assert target.seen == []


@pytest.mark.parametrize("body", [
    b"not json",
    b"",
    b"[\"owner\"]",
    b"\"owner\"",
    b"null",
    b"{}",
    b"{\"user\": null}",
    b"{\"user\": 7}",
    b"{\"user\": [\"owner\"]}",
    b"{\"user\": \"\"}",
    b"{\"user\": \"Mi",                    # truncated
    b"\xff\xfe\x00garbage",
])
def test_malformed_200_body_is_none(stub, body):
    assert check("vm_session=x", url=url(stub(body=body))) is None


def test_oversized_body_is_none(stub):
    body = json.dumps({"user": "owner", "pad": "x" * (vm_check.MAX_BODY + 10)}).encode()
    assert check("vm_session=x", url=url(stub(body=body))) is None


def test_200_with_extra_keys_returns_user(stub):
    assert check("vm_session=x", url=url(stub(body=b'{"user": "owner", "expires": "2030-06-09T00:00:00+10:00"}'))) == "owner"


def test_proxy_env_is_ignored(stub, monkeypatch):
    s = stub()
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9/")    # discard port: a proxy would fail
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9/")
    monkeypatch.delenv("no_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    assert check("vm_session=x", url=url(s)) == "owner"
    assert s.seen == ["vm_session=x"]


def test_bad_url_is_none():
    assert check("vm_session=x", url="not a url") is None


def test_stdlib_only():
    src = Path(vm_check.__file__).read_text()
    imports = {l.split()[1].split(".")[0] for l in src.splitlines() if l.startswith(("import ", "from "))}
    assert imports <= {"json", "urllib"}
