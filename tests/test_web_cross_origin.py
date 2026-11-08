"""Tests for web.py's cross-origin refusal on the state-changing /auth/ GETs.

SameSite=Strict lets a same-SITE page on another PORT send the vm_session cookie,
so a show-me page could sign the user out everywhere with one no-cors
fetch(".../auth/sign-out"). GET /auth/sign-out and GET
/auth/session therefore refuse a cross-origin request:

- Sec-Fetch-Site present: only `same-origin` and `none` pass;
- Sec-Fetch-Site absent (an old browser, curl, a loopback caller): pass, unless
  an Origin header is present and is not one of the page's own origins.

A refusal is 403 {"ok": false, "why": "cross-origin"}, no-store, no Set-Cookie,
and does nothing: the session is not revoked, a code is not consumed.
GET /auth/check is read-only and asked over loopback by other apps: untouched.

The real main() on a spare port, through test_web_session's fixture and helpers
(webauth.login mocked, temp SETTINGS_SESSIONS/PASSKEYS: the live stores are
never touched). Run: python -m pytest -q tests/test_web_cross_origin.py
"""
import http.client, json

import pytest

from test_web_session import HTTPS, cookie_of, login, server, sessions, web  # noqa: F401

OTHER_PORT = "https://studio.example.ts.net:8443"   # same site, another port: the probing page
FOREIGN = "https://evil.example"
CLEARED = "vm_session=; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=0"
REFUSED = {"ok": False, "why": "cross-origin"}


def req(srv, path, headers=None):
    """A real HTTP GET with exactly these headers. Returns (status, Set-Cookie list, body, headers)."""
    c = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
    try:
        c.request("GET", path, headers=headers or {})
        r = c.getresponse()
        raw = r.read()
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
        return r.status, r.headers.get_all("Set-Cookie") or [], body, r.headers
    finally:
        c.close()


def assert_refused(result):
    status, cookies, body, h = result
    assert status == 403
    assert body == REFUSED
    assert cookies == []                                 # no Set-Cookie: nothing minted, nothing cleared
    assert h["Cache-Control"] == "no-store"
    assert h["Content-Type"] == "application/json"


def live_cookie():
    raw, _ = sessions.mint()
    return raw, f"vm_session={raw}"


def a_code(srv):
    """A passkey login on https: (ws, its one-time code). Keep the ws open: closing it drops the code."""
    ws, ok = login(srv)
    return ws, ok["code"]


# --- the helper ---------------------------------------------------------------------

@pytest.mark.parametrize("headers, refused", [
    ({"Sec-Fetch-Site": "same-origin"}, False),
    ({"Sec-Fetch-Site": "none"}, False),
    ({"Sec-Fetch-Site": "same-site"}, True),
    ({"Sec-Fetch-Site": "cross-site"}, True),
    ({"Sec-Fetch-Site": "bogus"}, True),
    ({"Sec-Fetch-Site": ""}, True),
    ({"Sec-Fetch-Site": "same-site", "Origin": HTTPS}, True),      # the site header wins over a good Origin
    ({"Sec-Fetch-Site": "same-origin", "Origin": FOREIGN}, False),  # ...and over a bad one
    ({}, False),                                                    # curl, a loopback caller, an old browser
    ({"Origin": HTTPS}, False),
    ({"Origin": FOREIGN}, True),
    ({"Origin": OTHER_PORT}, True),
    ({"Origin": "null"}, True),
])
def test_cross_origin_helper(server, headers, refused):
    h = web.Headers()
    for k, v in headers.items():
        h[k] = v
    assert web.cross_origin(h) is refused


def test_cross_origin_helper_refuses_a_doubled_sec_fetch_site(server):
    h = web.Headers()
    h["Sec-Fetch-Site"] = "same-origin"
    h["Sec-Fetch-Site"] = "cross-site"
    assert web.cross_origin(h) is True


# --- GET /auth/sign-out -------------------------------------------------------------

@pytest.mark.parametrize("site", ["same-site", "cross-site"])
def test_sign_out_cross_origin_is_refused_and_the_session_survives(server, site):
    raw, cookie = live_cookie()
    assert_refused(req(server, "/auth/sign-out", {"Cookie": cookie, "Sec-Fetch-Site": site,
                                                   "Origin": OTHER_PORT}))
    assert sessions.lookup(raw) is not None              # not revoked


@pytest.mark.parametrize("site", ["same-site", "cross-site"])
def test_sign_out_no_cors_shape_without_origin_is_refused(server, site):
    """A no-cors GET carries no Origin header: Sec-Fetch-Site alone must refuse it."""
    raw, cookie = live_cookie()
    assert_refused(req(server, "/auth/sign-out", {"Cookie": cookie, "Sec-Fetch-Site": site}))
    assert sessions.lookup(raw) is not None


def test_sign_out_foreign_origin_without_sec_fetch_site_is_refused(server):
    raw, cookie = live_cookie()
    assert_refused(req(server, "/auth/sign-out", {"Cookie": cookie, "Origin": FOREIGN}))
    assert sessions.lookup(raw) is not None


@pytest.mark.parametrize("headers", [
    {"Sec-Fetch-Site": "same-origin"},
    {"Sec-Fetch-Site": "same-origin", "Origin": HTTPS},
    {"Sec-Fetch-Site": "none"},
    {},                                                  # curl / an old browser
    {"Origin": HTTPS},                                   # an old browser with the page's own Origin
])
def test_sign_out_same_origin_still_revokes_and_clears(server, headers):
    raw, cookie = live_cookie()
    status, cookies, body, h = req(server, "/auth/sign-out", {"Cookie": cookie, **headers})
    assert status == 200 and body == {"ok": True}
    assert cookies == [CLEARED]
    assert sessions.lookup(raw) is None                  # revoked


# --- GET /auth/session --------------------------------------------------------------

@pytest.mark.parametrize("headers", [
    {"Sec-Fetch-Site": "same-site"},
    {"Sec-Fetch-Site": "same-site", "Origin": OTHER_PORT},
    {"Sec-Fetch-Site": "cross-site"},
    {"Origin": FOREIGN},
])
def test_session_cross_origin_is_refused_and_the_code_is_not_consumed(server, headers):
    ws, code = a_code(server)
    try:
        before = len(sessions.load())
        assert_refused(req(server, f"/auth/session?code={code}&return=/talk/", headers))
        assert len(sessions.load()) == before             # nothing minted
        status, cookies, body, _ = req(server, f"/auth/session?code={code}&return=/talk/",
                                       {"Sec-Fetch-Site": "same-origin"})
        assert status == 200 and body == {"ok": True, "return": "/talk/"}
        assert len(cookies) == 1 and sessions.lookup(cookie_of(cookies[0])) is not None
    finally:
        ws.close()


def test_session_cross_origin_with_a_bad_return_is_still_403_not_400(server):
    """The origin check runs before anything else: before the return= check, before the code pop."""
    ws, code = a_code(server)
    try:
        assert_refused(req(server, f"/auth/session?code={code}&return=//evil", {"Sec-Fetch-Site": "same-site"}))
        assert req(server, f"/auth/session?code={code}", {"Sec-Fetch-Site": "same-origin"})[0] == 200
    finally:
        ws.close()


@pytest.mark.parametrize("headers", [
    {"Sec-Fetch-Site": "same-origin"},
    {"Sec-Fetch-Site": "none"},
    {},
    {"Origin": HTTPS},
])
def test_session_same_origin_still_mints(server, headers):
    ws, code = a_code(server)
    try:
        status, cookies, body, _ = req(server, f"/auth/session?code={code}", headers)
        assert status == 200 and body == {"ok": True, "return": None}
        assert len(cookies) == 1 and sessions.lookup(cookie_of(cookies[0])) is not None
    finally:
        ws.close()


# --- GET /auth/check: untouched -----------------------------------------------------

@pytest.mark.parametrize("headers", [
    {"Sec-Fetch-Site": "cross-site", "Origin": FOREIGN},
    {"Sec-Fetch-Site": "same-site"},
    {"Origin": FOREIGN},
])
def test_check_is_not_origin_checked(server, headers):
    raw, cookie = live_cookie()
    status, cookies, body, _ = req(server, "/auth/check", {"Cookie": cookie, **headers})
    assert status == 200 and body["user"] == "owner"
    assert cookies == []


# --- the log ------------------------------------------------------------------------

def test_a_refusal_is_logged(server, capfd):
    raw, cookie = live_cookie()
    capfd.readouterr()
    req(server, "/auth/sign-out", {"Cookie": cookie, "Sec-Fetch-Site": "same-site"})
    lines = [json.loads(x) for x in capfd.readouterr().out.splitlines() if x.startswith("{")]
    hit = [x for x in lines if x.get("auth") == "sign-out" and x.get("result", "").startswith("refused")]
    assert hit, lines
    assert "cross-origin" in hit[0]["result"] and "same-site" in hit[0]["result"]
