"""vm_check.py: the optional stdlib client for Settings' GET /auth/check.

The contract, for Talk and any other Python app on the same Mac:

    from settled.vm_check import check
    user = check(request_headers.get("Cookie"))
    if user is None:
        ...  # not signed in: send the browser to /settings/?return=<this path>

check(cookie_header, url=URL, timeout=1.0) -> str | None

- Forwards the browser's raw ``Cookie`` header to Settings exactly as given
  (Settings picks ``vm_session`` out of it; the caller never parses it).
- Returns the user (e.g. ``"owner"``) ONLY when Settings answers ``200`` with a
  JSON object whose ``user`` is a non-empty string.
- Returns ``None`` for everything else, so it FAILS CLOSED: no or empty header,
  Settings down (connection refused), a timeout (``timeout`` seconds per socket
  operation, ~1s by default), any non-200 (401 for a missing, forged, expired or
  signed-out cookie; a 5xx; a redirect, which is never followed), a non-JSON or
  malformed body, or any other exception.
- No cache: every call asks Settings, so a sign-out is seen at once by every app.
- Loopback only, never via a proxy: ``http_proxy`` and friends are ignored, so
  the cookie is never handed to anything but the URL given.

Stdlib only (urllib). It takes no import dependency on the Settings repo: Talk
may copy this file as it stands.
"""
import json
import urllib.request

URL = "http://127.0.0.1:8770/auth/check"
MAX_BODY = 64 * 1024          # /auth/check answers ~80 bytes; anything huge is not it


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A 3xx is not a 200: refuse to follow it (it surfaces as an HTTPError -> None)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


def check(cookie_header, url=URL, timeout=1.0):
    """The signed-in user for this Cookie header, or None. Never raises; fails closed."""
    if not cookie_header or not isinstance(cookie_header, str):
        return None
    try:
        req = urllib.request.Request(url, headers={"Cookie": cookie_header}, method="GET")
        with _OPENER.open(req, timeout=timeout) as r:
            if r.status != 200:
                return None
            raw = r.read(MAX_BODY + 1)
        if len(raw) > MAX_BODY:
            return None
        body = json.loads(raw)
    except Exception:             # refused, timeout, 4xx/5xx/3xx (HTTPError), bad header, bad JSON
        return None
    if not isinstance(body, dict):
        return None
    user = body.get("user")
    return user if isinstance(user, str) and user else None
