"""Tests for web.py's GET /auth/check and sign-out.

/auth/check is the other apps' question: Talk forwards the browser's Cookie
header to http://127.0.0.1:8770/auth/check and gets 200 {"user", "expires"} for
a live vm_session, 401 for anything else. Sign-out ends the session for every
app at once: the ws `sign-out` revokes the connection's session, and
GET /auth/sign-out revokes the cookie's session and clears the cookie.

The real main() on a spare port, through test_web_session's fixture and helpers
(webauth.login mocked, temp SETTINGS_SESSIONS/PASSKEYS: the live stores are
never touched). Run: python -m pytest -q tests/test_web_check.py
"""
import http.client, json, time

import pytest

from test_web_session import (HTTPS, cookie_of, first_two, get, login, open_ws, recv,  # noqa: F401
                              server, sessions, web)

CLEARED = "vm_session=; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=0"


def get_c(srv, path, cookie=None):
    """A real HTTP GET carrying a Cookie header, as Talk forwards it.
    Returns (status, Set-Cookie list, parsed body or None, headers)."""
    c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
    try:
        c.request("GET", path, headers={"Cookie": cookie} if cookie is not None else {})
        r = c.getresponse()
        raw = r.read()
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
        return r.status, r.headers.get_all("Set-Cookie") or [], body, r.headers
    finally:
        c.close()


def signed_in(srv):
    """A passkey login on https, the code swapped for the cookie. Returns (ws, raw id)."""
    ws, ok = login(srv)
    status, cookies, _, _ = get(srv, f"/auth/session?code={ok['code']}")
    assert status == 200 and cookies
    return ws, cookie_of(cookies[0])


def assert_401(srv, cookie):
    status, cookies, body, h = get_c(srv, "/auth/check", cookie)
    assert status == 401
    assert body == {}                                   # nothing about the session
    assert cookies == []
    assert h["Cache-Control"] == "no-store"


# --- GET /auth/check --------------------------------------------------------------

def test_check_live_session_is_200_user_and_expires_exactly(server):
    ws, raw = signed_in(server)
    ws.close()
    status, cookies, body, h = get_c(server, "/auth/check", f"theme=dark; vm_session={raw}; x=1")
    assert status == 200
    assert body == {"user": "owner", "expires": sessions.lookup(raw)["expires"]}   # exactly these keys
    assert set(body) == {"user", "expires"}
    assert "T" in body["expires"] and body["expires"][-6] in "+-"                 # iso with offset
    assert h["Cache-Control"] == "no-store"
    assert h["Content-Type"] == "application/json"
    assert cookies == []                                # a check never sets or touches the cookie


def test_check_missing_cookie_is_401(server):
    assert_401(server, None)


def test_check_cookie_header_without_vm_session_is_401(server):
    assert_401(server, "theme=dark; other=1")


def test_check_forged_cookie_is_401(server):
    sessions.mint()                                     # a live session exists, but not this one
    assert_401(server, "vm_session=" + "A" * 43)


def test_check_empty_cookie_is_401(server):
    assert_401(server, "vm_session=")


def test_check_expired_session_is_401(server):
    raw, _ = sessions.mint(now=time.time() - sessions.TTL - 1)
    assert_401(server, f"vm_session={raw}")


def test_check_signed_out_session_is_401(server):
    ws, raw = signed_in(server)
    ws.close()
    assert get_c(server, "/auth/check", f"vm_session={raw}")[0] == 200
    sessions.revoke(raw)
    assert_401(server, f"vm_session={raw}")


def test_check_does_not_accept_the_stored_hash_as_a_cookie(server):
    raw, _ = sessions.mint()
    assert_401(server, f"vm_session={sessions._hash(raw)}")   # reading the store hands no one a session


def test_check_with_a_query_string_is_still_the_check(server):
    raw, _ = sessions.mint()
    assert get_c(server, "/auth/check?x=1", f"vm_session={raw}")[0] == 200


# --- ws sign-out ------------------------------------------------------------------

def test_ws_sign_out_on_a_cookie_bound_connection_revokes_at_once(server):
    ws, raw = signed_in(server)
    ws.close()
    ws2 = open_ws(server, cookie=f"vm_session={raw}")          # authed by the cookie on connect
    assert first_two(ws2)["type"] == "auth-ok"
    assert get_c(server, "/auth/check", f"vm_session={raw}")[0] == 200
    ws2.send(json.dumps({"type": "sign-out"}))
    for _ in range(100):                                        # the server closes the ws when done
        try:
            recv(ws2)
        except Exception:
            break
    assert_401(server, f"vm_session={raw}")
    ws3 = open_ws(server, cookie=f"vm_session={raw}")          # and Settings asks for the touch again
    try:
        assert first_two(ws3)["type"] == "auth"
    finally:
        ws3.close()


def test_ws_sign_out_on_the_tab_that_minted_the_cookie_revokes_it(server):
    ws, raw = signed_in(server)                                 # st["session"] set by the exchange
    ws.send(json.dumps({"type": "sign-out"}))
    for _ in range(100):
        try:
            recv(ws)
        except Exception:
            break
    assert_401(server, f"vm_session={raw}")


def test_ws_sign_out_revokes_only_its_own_session(server):
    other, _ = sessions.mint()                                  # another browser's session
    ws, raw = signed_in(server)
    ws.send(json.dumps({"type": "sign-out"}))
    for _ in range(100):
        try:
            recv(ws)
        except Exception:
            break
    assert_401(server, f"vm_session={raw}")
    assert get_c(server, "/auth/check", f"vm_session={other}")[0] == 200


# --- GET /auth/sign-out -----------------------------------------------------------

def test_get_sign_out_revokes_and_clears_the_cookie(server):
    ws, raw = signed_in(server)
    ws.close()
    status, cookies, body, h = get_c(server, "/auth/sign-out", f"vm_session={raw}")
    assert status == 200 and body == {"ok": True}
    assert cookies == [CLEARED]                                 # exactly, on the raw header
    assert h["Cache-Control"] == "no-store"
    assert sessions.lookup(raw) is None
    assert_401(server, f"vm_session={raw}")


def test_get_sign_out_leaves_other_sessions_alone(server):
    other, _ = sessions.mint()
    raw, _ = sessions.mint()
    get_c(server, "/auth/sign-out", f"vm_session={raw}")
    assert_401(server, f"vm_session={raw}")
    assert get_c(server, "/auth/check", f"vm_session={other}")[0] == 200


@pytest.mark.parametrize("cookie", [None, "vm_session=forged", "vm_session="])
def test_get_sign_out_without_a_live_session_still_clears(server, cookie):
    live, _ = sessions.mint()
    status, cookies, body, _ = get_c(server, "/auth/sign-out", cookie)
    assert status == 200 and body == {"ok": True}
    assert cookies == [CLEARED]
    assert get_c(server, "/auth/check", f"vm_session={live}")[0] == 200   # nothing else revoked


def test_after_get_sign_out_a_code_cannot_revive_the_session(server):
    ws, raw = signed_in(server)
    ws.close()
    ws2 = open_ws(server, cookie=f"vm_session={raw}")
    try:
        ok = first_two(ws2)
        assert ok["type"] == "auth-ok"
        get_c(server, "/auth/sign-out", f"vm_session={raw}")
        status, cookies, _, _ = get(server, f"/auth/session?code={ok['code']}")
        assert status == 401 and cookies == []
    finally:
        ws2.close()
    assert_401(server, f"vm_session={raw}")


# --- localhost unchanged ----------------------------------------------------------

def test_localhost_resume_still_works_and_sign_out_ends_only_the_token(server):
    live, _ = sessions.mint()                                   # an https session elsewhere
    local = f"http://localhost:{server.port}"
    ws, ok = login(server, local)
    ws.close()
    ws2 = open_ws(server, local)                                # a reload resumes with the tab token
    assert first_two(ws2)["type"] == "auth"
    ws2.send(json.dumps({"type": "resume", "token": ok["token"]}))
    ok2 = recv(ws2)
    assert ok2["type"] == "auth-ok" and "code" not in ok2
    ws2.send(json.dumps({"type": "sign-out"}))
    for _ in range(100):
        try:
            recv(ws2)
        except Exception:
            break
    assert ok["token"] not in web.TOKENS                        # the tab's token is gone, as before
    assert get_c(server, "/auth/check", f"vm_session={live}")[0] == 200   # no session was touched
    ws3 = open_ws(server, local)
    try:
        assert first_two(ws3)["type"] == "auth"
        ws3.send(json.dumps({"type": "resume", "token": ok["token"]}))
        assert recv(ws3)["type"] == "auth-failed"
    finally:
        ws3.close()
