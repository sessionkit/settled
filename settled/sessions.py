"""sessions.py: the host-wide sign-in session store behind the `vm_session` cookie.

The owner touches a passkey once and every app on the ts.net origin knows
it is him. Settings mints the session, keeps it on disk, and answers
`GET /auth/check` for the other apps. The interface (cookie name, /auth/check,
200/401, the return= rule) was agreed with Talk.

Stdlib only. The store keeps only sha256(raw id), never the raw id, so reading
the file does not hand anyone a session. Every call re-reads the file: a second
server process (a restart) sees what the first wrote, and a revoke takes effect
everywhere at once. There is no in-memory cache to outlive a revoke.

A missing, empty or corrupt store reads as "no sessions": it fails closed.
"""
import hashlib, json, os, secrets, time
from datetime import datetime, timezone
from http.cookies import SimpleCookie, CookieError
from pathlib import Path

from settled import paths  # settled's own folder

COOKIE = "vm_session"
#: The desk's one user: whoever holds a passkey for this Mac. Named once, here,
#: and never after a person: the page and every session say this.
OWNER = "owner"
TTL = 7 * 24 * 3600          # one constant: the session length is still an open question
DEFAULT_STORE = paths.settled_home() / "sessions.json"


def store_path() -> Path:
    """$SETTINGS_SESSIONS, else sessions.json under settled's own folder. Read per call,
    so a test (or a caller) that sets the variable later is still honoured."""
    env = os.environ.get("SETTINGS_SESSIONS")
    return Path(env) if env else DEFAULT_STORE


def _hash(raw_id: str) -> str:
    return hashlib.sha256(raw_id.encode()).hexdigest()


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).astimezone().isoformat(timespec="seconds")


def _clock(now):
    return time.time() if now is None else now


def load(path: Path = None) -> dict:
    """{sha256 hex: record}. Missing, empty, corrupt or wrong-shaped: {}."""
    p = path or store_path()
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return {}
    sess = data.get("sessions") if isinstance(data, dict) else None
    if not isinstance(sess, dict):
        return {}
    return {h: r for h, r in sess.items()
            if isinstance(h, str) and isinstance(r, dict) and isinstance(r.get("expires_at"), (int, float))}


def save(sessions: dict, path: Path = None, now: float = None) -> None:
    """Atomic write (tmp, 0600, replace), dir 0700, the same as webauth.save.
    Expired records are pruned on the way out."""
    p = path or store_path()
    t = _clock(now)
    live = {h: r for h, r in sessions.items() if r.get("expires_at", 0) > t}
    p.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(p.parent, 0o700)
    tmp = p.with_name(p.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps({"sessions": live}, indent=2) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(p)


def mint(user: str = OWNER, path: Path = None, now: float = None) -> tuple:
    """(raw_id, record). raw_id is 32 random bytes, url-safe; it is returned once
    and never written. The store holds only its sha256."""
    t = _clock(now)
    raw = secrets.token_urlsafe(32)
    rec = {"user": user, "created": _iso(t), "expires": _iso(t + TTL),
           "created_at": t, "expires_at": t + TTL}
    p = path or store_path()
    sess = load(p)
    sess[_hash(raw)] = rec
    save(sess, p, now=t)
    return raw, dict(rec)


def lookup_hash(h: str, path: Path = None, now: float = None):
    rec = load(path).get(h) if isinstance(h, str) else None
    if rec is None or rec["expires_at"] <= _clock(now):
        return None
    return dict(rec)


def lookup(raw_id, path: Path = None, now: float = None):
    """The live record for this raw id, or None (unknown, forged, expired, revoked)."""
    if not isinstance(raw_id, str) or not raw_id:
        return None
    return lookup_hash(_hash(raw_id), path, now)


def revoke_hash(h: str, path: Path = None) -> bool:
    p = path or store_path()
    sess = load(p)
    if not isinstance(h, str) or h not in sess:
        return False
    del sess[h]
    save(sess, p)
    return True


def revoke(raw_id, path: Path = None) -> bool:
    if not isinstance(raw_id, str) or not raw_id:
        return False
    return revoke_hash(_hash(raw_id), path)


def cookie_value(cookie_header):
    """vm_session's value from a raw `Cookie:` header, or None. Junk elsewhere in
    the header (bad pairs, stray quotes) does not hide it."""
    if not isinstance(cookie_header, str) or not cookie_header:
        return None
    try:
        c = SimpleCookie()
        c.load(cookie_header)
        if COOKIE in c and c[COOKIE].value:
            return c[COOKIE].value
    except CookieError:
        pass
    # SimpleCookie gives up on the whole header at the first bad pair: split by hand.
    for part in cookie_header.split(";"):
        name, sep, value = part.strip().partition("=")
        if sep and name.strip() == COOKIE:
            v = value.strip().strip('"')
            if v:
                return v
    return None


def safe_return(s):
    """A same-origin path, or None. It must start with one `/` (not `//`), and
    carry no `\\` (browsers read `/\\evil.com` as `//evil.com`), no whitespace or
    control characters, and no scheme."""
    if not isinstance(s, str) or not s.startswith("/") or s.startswith("//"):
        return None
    if "\\" in s or any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7f for ch in s):
        return None
    if ":" in s.split("?", 1)[0].split("#", 1)[0].split("/", 2)[1]:
        return None          # a scheme-looking first segment, e.g. /javascript:...
    return s
