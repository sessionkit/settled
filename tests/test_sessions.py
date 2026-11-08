"""Offline tests for sessions.py: the vm_session store, cookie parse and return= rule.

Every test runs against a temp store via SETTINGS_SESSIONS; the real
the real sessions.json is never touched.
Run: python -m pytest -q tests/test_sessions.py
"""
import base64, hashlib, json, os, stat, subprocess, sys, tempfile
from pathlib import Path

import pytest

os.environ["SETTINGS_SESSIONS"] = str(Path(tempfile.mkdtemp(prefix="settings-sess-")) / "sessions.json")
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from settled import sessions as s  # noqa: E402


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    p = tmp_path / "sub" / "sessions.json"
    monkeypatch.setenv("SETTINGS_SESSIONS", str(p))
    return p


# --- store path -------------------------------------------------------------

def test_store_path_env_and_default(store, monkeypatch):
    assert s.store_path() == store
    monkeypatch.delenv("SETTINGS_SESSIONS")
    assert s.store_path() == Path.home() / ".sessionkit" / "settled" / "sessions.json"  # paths.settled_home()


# --- mint -------------------------------------------------------------------

def test_mint_raw_id_is_32_random_bytes_and_never_stored(store):
    raw, rec = s.mint()
    assert len(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))) >= 32
    text = store.read_text()
    assert raw not in text
    data = json.loads(text)
    assert list(data["sessions"]) == [hashlib.sha256(raw.encode()).hexdigest()]
    assert rec["user"] == "owner"
    assert rec["expires_at"] - rec["created_at"] == s.TTL


def test_mint_ids_differ(store):
    assert s.mint()[0] != s.mint()[0]
    assert len(json.loads(store.read_text())["sessions"]) == 2


def test_file_0600_dir_0700(store):
    s.mint()
    assert stat.S_IMODE(store.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.parent.stat().st_mode) == 0o700
    assert not (store.parent / (store.name + ".tmp")).exists()


# --- lookup -----------------------------------------------------------------

def test_lookup_live_unknown_forged():
    raw, _ = s.mint()
    rec = s.lookup(raw)
    assert rec and rec["user"] == "owner" and "expires" in rec
    assert s.lookup("nope") is None
    assert s.lookup(raw[:-1] + ("A" if raw[-1] != "A" else "B")) is None
    assert s.lookup("") is None
    assert s.lookup(None) is None
    # the hash itself is not a credential
    assert s.lookup(hashlib.sha256(raw.encode()).hexdigest()) is None


def test_lookup_expired_with_injected_clock():
    t0 = 1_800_000_000.0
    raw, _ = s.mint(now=t0)
    assert s.lookup(raw, now=t0 + s.TTL - 1)
    assert s.lookup(raw, now=t0 + s.TTL) is None
    assert s.lookup(raw, now=t0 + s.TTL + 1) is None


def test_lookup_expired_with_monkeypatched_time(monkeypatch):
    raw, _ = s.mint()
    real = s.time.time()
    monkeypatch.setattr(s.time, "time", lambda: real + s.TTL + 5)
    assert s.lookup(raw) is None


def test_expired_pruned_on_save(store):
    t0 = 1_800_000_000.0
    old, _ = s.mint(now=t0)
    new, _ = s.mint(now=t0 + s.TTL + 10)
    keys = set(json.loads(store.read_text())["sessions"])
    assert keys == {hashlib.sha256(new.encode()).hexdigest()}


# --- revoke -----------------------------------------------------------------

def test_revoke():
    a, _ = s.mint()
    b, _ = s.mint()
    assert s.revoke(a) is True
    assert s.lookup(a) is None
    assert s.lookup(b)
    assert s.revoke(a) is False
    assert s.revoke("nope") is False
    assert s.revoke(None) is False


def test_revoke_hash():
    raw, _ = s.mint()
    h = hashlib.sha256(raw.encode()).hexdigest()
    assert s.lookup_hash(h)
    assert s.revoke_hash(h) is True
    assert s.lookup(raw) is None
    assert s.revoke_hash(h) is False


# --- missing / empty / corrupt ---------------------------------------------

@pytest.mark.parametrize("body", ["", "   ", "{not json", "[]", '"x"', '{"sessions": []}',
                                  '{"sessions": {"h": "notadict"}}', '{"sessions": {"h": {"user": "x"}}}'])
def test_bad_file_reads_as_no_sessions(store, body):
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(body)
    assert s.load() == {}
    assert s.lookup("anything") is None
    raw, _ = s.mint()          # and minting over it recovers
    assert s.lookup(raw)


def test_missing_file_reads_as_no_sessions(store):
    assert not store.exists()
    assert s.load() == {}
    assert s.lookup("x") is None
    assert s.revoke("x") is False


# --- restart survival -------------------------------------------------------

def _other_process(store, raw):
    """A genuinely new interpreter. Popen, not subprocess.run: test_voice_browser.py
    replaces subprocess.run module-wide with a fake."""
    code = ("import sys; sys.path.insert(0, %r); from settled import sessions; "
            "r = sessions.lookup(%r); print(r['user'] if r else 'NONE')") % (str(ROOT), raw)
    with subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True,
                          env={**os.environ, "SETTINGS_SESSIONS": str(store)}) as p:
        out, _ = p.communicate(timeout=30)
    assert p.returncode == 0
    return out.strip()


def test_new_process_sees_session(store):
    raw, _ = s.mint()
    assert _other_process(store, raw) == "owner"
    # and a revoke in this process is seen by the next one
    s.revoke(raw)
    assert _other_process(store, raw) == "NONE"


def test_fresh_module_state_sees_session(store):
    import importlib
    raw, _ = s.mint()
    fresh = importlib.reload(s)
    assert fresh.lookup(raw)["user"] == "owner"


# --- cookie_value -----------------------------------------------------------

@pytest.mark.parametrize("header,want", [
    ("vm_session=abc", "abc"),
    ("a=1; vm_session=abc-_9; b=2", "abc-_9"),
    ("theme=dark;vm_session=xyz", "xyz"),
    ('vm_session="q1"', "q1"),
    ("junk; =; a=\"unterminated; vm_session=tok; c", "tok"),
    ("x=[bad]; vm_session=tok2", "tok2"),
    ("vm_sessionx=nope; xvm_session=nope", None),
    ("vm_session=", None),
    ("a=1; b=2", None),
    ("", None),
    (None, None),
    ("garbage", None),
])
def test_cookie_value(header, want):
    assert s.cookie_value(header) == want


# --- safe_return ------------------------------------------------------------

@pytest.mark.parametrize("ok", ["/", "/talk/", "/talk/?x=1", "/settings/#a", "/a/b:c"])
def test_safe_return_accepts(ok):
    assert s.safe_return(ok) == ok


@pytest.mark.parametrize("bad", [
    "//evil", "//evil.com/x", "https://x", "http://x/", "/\\evil", "/\\evil.com", "\\\\evil",
    "javascript:alert(1)", "/javascript:alert(1)", "", " ", " /talk/", "/talk/ ", "/ta lk",
    "/talk\n", "/talk\r\n/x", "/\t/evil", "/\x00", "/\x7f", "talk/", None, 5,
])
def test_safe_return_refuses(bad):
    assert s.safe_return(bad) is None
