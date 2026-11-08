"""Offline tests for web.py's origin check when Tailscale comes up late.

launchd can start settings-web before Tailscale answers. main()'s one lookup of
the tailnet name then finds nothing, ORIGINS holds only localhost, and
https://<host>.ts.net was refused ("This address can't hold a passkey") until a
restart. The handler must look the name up again on a miss, but not on every
request: at most once every web.RECHECK seconds.

main() really runs; tailnet_name(), the listening socket, the poll loop, the
state read and the passkey offer are stubbed (no real Tailscale, no
passkeys.json read). Run: python3 tests/test_web_origins.py (or pytest).
Needs `websockets`.
"""
import asyncio, json, os, sys, tempfile, time
from pathlib import Path

# Never the live passkeys, even if something below reaches webauth.
os.environ.setdefault("SETTINGS_PASSKEYS", str(Path(tempfile.mkdtemp(prefix="settings-origins-")) / "passkeys.json"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from settled import web  # noqa: E402

PORT = 8770
LOCAL = f"http://localhost:{PORT}"
TS = "studio.example.ts.net"


class FakeWS:
    """Just enough of a websockets connection for handler(): an Origin header,
    a send() that records, and no incoming messages."""

    def __init__(self, origin):
        self.request = type("Req", (), {"headers": {"Origin": origin}})()
        self.sent = []

    async def send(self, text):
        self.sent.append(json.loads(text))

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


class Tailnet:
    """A stand-in for tailnet_name(): answers `name`, counts the calls."""

    def __init__(self, name=""):
        self.name, self.calls = name, 0

    def __call__(self):
        self.calls += 1
        return self.name


class _Serve:
    """web.serve, stubbed: main() runs its whole start-up but listens on nothing."""

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


async def _poll():
    return None


def booted(tailnet):
    """The server as the real main() leaves it, started with `tailnet` as the lookup."""
    web.tailnet_name = tailnet
    asyncio.run(web.main(PORT))


async def _offer(ws, st):
    await ws.send(json.dumps({"type": "auth", "rpId": st["origin"]}))


def connect(origin):
    """One websocket connection from `origin`; what the server sent it."""
    ws = FakeWS(origin)
    asyncio.run(web.handler(ws))
    return [m["type"] for m in ws.sent]


REAL = (web.tailnet_name, web.offer, web.serve, web.poll, web.desk.state)


def setup_function(_=None):
    web.offer, web.serve, web.poll, web.desk.state = _offer, _Serve, _poll, lambda: {"kind": "state"}
    web.ORIGINS.clear()
    web.LAST_LOOKUP = float("-inf")


def teardown_function(_=None):
    web.tailnet_name, web.offer, web.serve, web.poll, web.desk.state = REAL
    web.ORIGINS.clear()
    web.LAST_LOOKUP = float("-inf")
    web.LOG.clear()


def test_boot_before_tailscale_then_tailscale_comes_up():
    """The bug: no tailnet name at start, then one; the ts.net origin is then let in."""
    tailnet = Tailnet("")
    booted(tailnet)
    assert web.ORIGINS == {LOCAL}                      # Tailscale not up yet
    tailnet.name = TS                                  # ... and now it is
    web.LAST_LOOKUP = time.monotonic() - getattr(web, "RECHECK", 0)   # the boot lookup is old enough to retry
    assert connect(f"https://{TS}") == ["hello", "auth"]
    assert connect(f"https://{TS}:{PORT}") == ["hello", "auth"]
    assert {f"https://{TS}", f"https://{TS}:{PORT}"} <= web.ORIGINS


def test_boot_with_tailscale_up_needs_no_second_lookup():
    tailnet = Tailnet(TS)
    booted(tailnet)
    assert web.ORIGINS == {LOCAL, f"https://{TS}", f"https://{TS}:{PORT}"}
    assert connect(f"https://{TS}") == ["hello", "auth"]
    assert connect(LOCAL) == ["hello", "auth"]
    assert tailnet.calls == 1                          # only main()'s


def test_misses_within_the_backoff_do_not_look_again():
    tailnet = Tailnet("")
    booted(tailnet)
    assert tailnet.calls == 1
    for _ in range(5):                                 # a page that keeps retrying, just after boot
        assert connect(f"https://{TS}") == ["hello", "auth-refused"]
    assert tailnet.calls == 1                          # inside RECHECK: no lookups at all
    web.LAST_LOOKUP = time.monotonic() - web.RECHECK   # the backoff runs out
    assert connect(f"https://{TS}") == ["hello", "auth-refused"]
    assert tailnet.calls == 2                          # one lookup ...
    for _ in range(5):
        connect(f"https://{TS}")
    assert tailnet.calls == 2                          # ... then quiet again for RECHECK


def test_a_burst_of_misses_at_once_costs_one_lookup():
    """Several pages retrying together while tailscale is slow to answer."""
    tailnet = Tailnet("")
    booted(tailnet)
    web.LAST_LOOKUP = float("-inf")

    async def burst():
        wss = [FakeWS(f"https://{TS}") for _ in range(5)]
        await asyncio.gather(*(web.handler(ws) for ws in wss))
        return [[m["type"] for m in ws.sent] for ws in wss]

    assert asyncio.run(burst()) == [["hello", "auth-refused"]] * 5
    assert tailnet.calls == 2                          # main()'s, then ONE for the whole burst


def test_a_hit_never_looks_up():
    tailnet = Tailnet("")
    booted(tailnet)
    web.LAST_LOOKUP = float("-inf")                    # a miss now WOULD look
    for _ in range(3):
        assert connect(LOCAL) == ["hello", "auth"]
    assert tailnet.calls == 1


def test_a_stranger_is_still_refused_once_tailscale_is_up():
    tailnet = Tailnet("")
    booted(tailnet)
    tailnet.name = TS
    web.LAST_LOOKUP = float("-inf")
    assert connect("https://evil.example") == ["hello", "auth-refused"]
    assert tailnet.calls == 2                          # it looked, and still said no
    assert "https://evil.example" not in web.ORIGINS
    assert connect(f"https://{TS}") == ["hello", "auth"]   # the lookup the stranger caused let the host in


if __name__ == "__main__":
    for name, f in list(globals().items()):
        if name.startswith("test_"):
            setup_function()
            try:
                f()
            finally:
                teardown_function()
            print("ok", name)
