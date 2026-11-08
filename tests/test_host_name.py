"""The app names the host it is on, never a hard-coded one.

Served from a second Mac, the page said "Waiting for <the first Mac>..."
forever and labelled its passkey with the first Mac's name. The SERVER names
the host, and the host rides in the state: desk.state() is the one thing both
paths share (the phone's applier and the browser via web.py), so the client
never guesses it.

The host is faked with socket.gethostname -> "laptop.local" (short name
"laptop") at CALL time, so the server must read it when it builds the state /
hello, not freeze it at import. (The static scan for the old host's literal
name is a private test, export-ignored, since it must name it.)
Run: python -m pytest -q tests/test_host_name.py
"""
import asyncio, json, socket, sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from settled import desk  # noqa: E402
from settled import web  # noqa: E402

HOSTS = [("laptop.local", "laptop"), ("studio.tail1234.ts.net", "studio"), ("laptop", "laptop")]


def fake_host(monkeypatch, name):
    monkeypatch.setattr(socket, "gethostname", lambda: name)


@pytest.fixture
def quiet_state(monkeypatch):
    """desk.state() with every device / tool probe stubbed: no subprocess, no network."""
    monkeypatch.setattr(desk, "capture", lambda: {"up": False, "device": ""})
    monkeypatch.setattr(desk, "devices", lambda kind: [])
    monkeypatch.setattr(desk, "mouth_voices", lambda: {})
    monkeypatch.setattr(desk, "run", lambda *cmd: "")
    monkeypatch.setattr(desk, "tools", lambda ears, cap: {})
    monkeypatch.setattr(desk, "save", lambda p, d: None)
    monkeypatch.setattr(desk, "event", lambda *a, **k: None)


# (1) the state carries the serving host's short name

@pytest.mark.parametrize("hostname,short", HOSTS)
def test_state_carries_the_host_short_name(quiet_state, monkeypatch, hostname, short):
    fake_host(monkeypatch, hostname)
    s = desk.state()
    assert s.get("host") == short, f"state() should name the host it is on; keys: {sorted(s)}"


# (2) web.py's hello names the host, so webxdc.js can label the passkey "<host> Settings"

class _Stop(Exception):
    pass


class FakeWS:
    """Just enough of a websockets connection for handler() to send its hello."""

    class request:
        headers = {"Origin": "http://localhost:8790"}

    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))
        raise _Stop  # the hello is all this test needs

    async def close(self):
        pass


def hello_of():
    ws = FakeWS()
    try:
        asyncio.run(web.handler(ws))
    except _Stop:
        pass
    assert ws.sent and ws.sent[0]["type"] == "hello", ws.sent
    return ws.sent[0]


@pytest.mark.parametrize("hostname,short", HOSTS)
def test_hello_names_the_host(monkeypatch, hostname, short):
    fake_host(monkeypatch, hostname)
    hello = hello_of()
    assert hello.get("host") == short, f"hello should name the host; got {hello}"
