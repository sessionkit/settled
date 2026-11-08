#!/usr/bin/env python3
"""An end-to-end smoke of the host sign-in, shaped like production.

    $PY tests/smoke/e2e.py          # exit 0 = every check passed; prints a transcript

What it drives, with nothing mocked inside the auth path:
- the real web.main(), as its own process, plain HTTP on 127.0.0.1:<spare port>,
  exactly as launchd runs it behind `tailscale serve`. Only tailnet_name() (it
  answers a pretend ts.net name, so that https origin is allowed), desk.state()
  and poll() are stubbed: they touch the Mac's audio, not the sign-in.
- the browser side as the proxy hands it on: the ws upgrade carries
  Origin: https://<ts name>, and HTTP requests carry the Cookie header.
- real WebAuthn: an ES256 key made here; the first passkey's enrolment (with a
  code from the real `settled.web enrol`: no trust on first use)
  and a later login are verified by the unmodified webauth.register()/login().
- a real restart: the server process is killed and a NEW process reads the store.
- vm_check.check() against the live server, then against nothing (down) and
  against a socket that never answers (timeout).
- On the cookie-unlocked socket, every keyring change (add, rename,
  remove) is refused until a step-up touch (a fresh login assertion over its own
  challenge) grants it; a replayed step-up and a reused grant are refused.

Spare ports and a temp dir only: SETTINGS_PASSKEYS and SETTINGS_SESSIONS point
into it. Never the live server (8770) or a live settings folder. Not collected by pytest.
"""
import asyncio, base64, hashlib, json, os, socket, subprocess, sys, tempfile, threading, time
import urllib.request, urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from settled import sessions, vm_check  # noqa: E402
from websockets.asyncio.client import connect  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402

TS = "studio.smoke-test.ts.net"
ORIGIN = f"https://{TS}"
SERVER = r"""
import asyncio, sys
sys.path.insert(0, sys.argv[1])
from settled import web
web.tailnet_name = lambda: sys.argv[3]
web.desk.state = lambda: {"kind": "state"}
async def forever(): await asyncio.Event().wait()
web.poll = forever
asyncio.run(web.main(int(sys.argv[2])))
"""
FAILS = []


def b64u(b): return base64.urlsafe_b64encode(b).rstrip(b"=").decode()
def unb64u(s): return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def ok(cond, what, detail=""):
    print(f"{'PASS' if cond else 'FAIL'} {what}" + (f"  [{detail}]" if detail != "" else ""), flush=True)
    if not cond:
        FAILS.append(what)


def spare():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def store_env(tmp):
    return {**os.environ, "SETTINGS_PASSKEYS": str(tmp / "passkeys.json"),
            "SETTINGS_SESSIONS": str(tmp / "sessions.json"), "SETTINGS_ENROL": str(tmp / "enrol.json")}


def enrol_code(tmp):
    """A one-time code from the real CLI, as a user gets it: `settled enrol` on the machine."""
    out = subprocess.run([sys.executable, "-m", "settled.web", "enrol"], cwd=ROOT, env=store_env(tmp),
                         capture_output=True, text=True, timeout=30).stdout
    return next(ln.split()[2] for ln in out.splitlines() if ln.startswith("enrol code:"))


class Server:
    def __init__(self, tmp, port):
        env = store_env(tmp)
        self.log = open(tmp / f"server-{port}.log", "w")
        self.p = subprocess.Popen([sys.executable, "-c", SERVER, str(ROOT), str(port), TS],
                                  env=env, stdout=self.log, stderr=subprocess.STDOUT)
        self.port = port
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), 0.2).close(); return
            except OSError:
                time.sleep(0.1)
        raise SystemExit(f"server on {port} never came up")

    def stop(self):
        self.p.terminate(); self.p.wait(5); self.log.close()


def get(port, path, cookie=None):
    """(status, headers as a list of (k, v), parsed body) for a plain loopback GET."""
    h = {"Cookie": cookie} if cookie else {}
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=h)
    try:
        r = urllib.request.urlopen(req, timeout=3)
    except urllib.error.HTTPError as e:
        r = e
    body = r.read()
    try:
        body = json.loads(body)
    except ValueError:
        pass
    return r.status, list(r.headers.items()), body


def set_cookies(hdrs): return [v for k, v in hdrs if k.lower() == "set-cookie"]


class Authenticator:
    """A software passkey: an ES256 key and the bytes a browser + Touch ID would hand over."""
    def __init__(self, rp):
        self.rp, self.key, self.id = rp, ec.generate_private_key(ec.SECP256R1()), b64u(os.urandom(16))

    def _ad(self): return hashlib.sha256(self.rp.encode()).digest() + bytes([0x05]) + (1).to_bytes(4, "big")

    def _cd(self, kind, chal, origin):
        return json.dumps({"type": kind, "challenge": chal, "origin": origin}).encode()

    def register(self, offer, origin):
        spki = self.key.public_key().public_bytes(serialization.Encoding.DER,
                                                  serialization.PublicFormat.SubjectPublicKeyInfo)
        return {"type": "auth-finish", "mode": "register", "id": self.id, "alg": -7, "name": "smoke",
                "clientDataJSON": b64u(self._cd("webauthn.create", offer["challenge"], origin)),
                "authenticatorData": b64u(self._ad()), "publicKey": b64u(spki)}

    def login(self, offer, origin, kind="auth-finish"):
        cd, ad = self._cd("webauthn.get", offer["challenge"], origin), self._ad()
        sig = self.key.sign(ad + hashlib.sha256(cd).digest(), ec.ECDSA(hashes.SHA256()))
        m = {"type": kind, "id": self.id,
             "clientDataJSON": b64u(cd), "authenticatorData": b64u(ad), "signature": b64u(sig)}
        if kind == "auth-finish":
            m["mode"] = "login"
        return m


async def ws_open(port, origin, cookie=None):
    """Connect as the proxy would; return (ws, the first message after hello)."""
    h = {"Origin": origin}
    if cookie:
        h["Cookie"] = cookie
    ws = await connect(f"ws://127.0.0.1:{port}/ws", additional_headers=h, origin=None)
    hello = json.loads(await ws.recv())
    assert hello["type"] == "hello", hello
    return ws, json.loads(await ws.recv())


async def recv_type(ws, kind):
    while True:
        m = json.loads(await asyncio.wait_for(ws.recv(), 3))
        if m["type"] == kind or m["type"].startswith("auth-f") or m["type"] == "step-up-failed":
            return m


def names(m): return [k["name"] for k in m.get("passkeys", [])]


async def step_up(ws, key, op, pid=None):
    """The page's step-up: begin, then a real assertion over that challenge. -> (offer, ok reply, the message sent)."""
    await ws.send(json.dumps({"type": "step-up-begin", "op": op, "id": pid}))
    offer = await recv_type(ws, "step-up")
    msg = key.login(offer, ORIGIN, kind="step-up")
    await ws.send(json.dumps(msg))
    return offer, await recv_type(ws, "step-up-ok"), msg


async def run(tmp):
    port = spare()
    print(f"# temp dir {tmp}; server A on 127.0.0.1:{port}; origin {ORIGIN}")
    a = Server(tmp, port)
    ts_key = Authenticator(TS)

    # 1. first touch on the ts.net origin: enrol with the code (no TOFU), auth-ok carries a code
    ws, offer = await ws_open(port, ORIGIN)
    ok(offer["type"] == "auth" and offer["mode"] == "enrol" and offer["rpId"] == TS,
       "fresh ts.net connect gets the passkey offer (an empty store: enrol, never register)", offer.get("mode"))
    await ws.send(json.dumps(ts_key.register(offer, ORIGIN)))
    m = await recv_type(ws, "auth-failed")
    ok("settled enrol" in m.get("why", ""), "a bare register on the empty store is refused", m.get("why"))
    offer = await recv_type(ws, "auth")
    await ws.send(json.dumps({**ts_key.register(offer, ORIGIN), "mode": "enrol", "code": enrol_code(tmp)}))
    m = await recv_type(ws, "auth-ok")
    ok(m["type"] == "auth-ok" and m.get("code"), "real registration verified -> auth-ok with a one-time code")
    code = m["code"]

    # 2. the exchange: Set-Cookie exact
    st, hd, body = get(port, f"/auth/session?code={code}&return=/talk/")
    sc = set_cookies(hd)
    ok(st == 200 and body == {"ok": True, "return": "/talk/"}, "GET /auth/session 200, return=/talk/ echoed", body)
    ok(len(sc) == 1, "exactly one Set-Cookie", sc)
    name, _, rest = sc[0].partition("=")
    raw, _, attrs = rest.partition("; ")
    ok(name == "vm_session" and attrs == f"Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age={sessions.TTL}",
       "Set-Cookie attributes exact, no Domain (host-only)", f"vm_session=<{len(raw)} chars>; {attrs}")
    ok(len(unb64u(raw)) >= 32, "cookie value is >= 32 random bytes", f"{len(unb64u(raw))} bytes")
    cookie = f"other=1; vm_session={raw}; theme=dark"
    ok("no-store" in dict(hd).get("Cache-Control", ""), "auth route is Cache-Control: no-store")
    st2, hd2, _ = get(port, f"/auth/session?code={code}")
    ok(st2 == 401 and not set_cookies(hd2), "the code is single use (second redemption 401, no cookie)", st2)
    await ws.close()

    # 3. the store holds only the hash, 0600
    store = tmp / "sessions.json"
    text = store.read_text()
    ok(raw not in text and sessions._hash(raw) in text, "store keeps sha256(id) only, never the raw id")
    ok(oct(store.stat().st_mode & 0o777) == "0o600", "store is 0600", oct(store.stat().st_mode & 0o777))

    # 4. /auth/check: live 200; missing/forged/expired 401
    st, _, body = get(port, "/auth/check", cookie)
    ok(st == 200 and set(body) == {"user", "expires"} and body["user"] == "owner",
       "/auth/check live -> 200 {user, expires}", body)
    for label, ck in [("missing", None), ("no vm_session", "theme=dark"),
                      ("forged", "vm_session=" + b64u(os.urandom(32))),
                      ("the stored hash as cookie", "vm_session=" + sessions._hash(raw))]:
        st, _, body = get(port, "/auth/check", ck)
        ok(st == 401 and body == {}, f"/auth/check {label} -> 401 {{}}", st)
    exp_raw, _ = sessions.mint("owner", path=store, now=time.time() - sessions.TTL - 60)   # minted 7d+1m ago
    st, _, body = get(port, "/auth/check", f"vm_session={exp_raw}")
    ok(st == 401 and body == {}, "/auth/check expired -> 401 {}", st)
    ok(vm_check.check(cookie, url=f"http://127.0.0.1:{port}/auth/check") == "owner",
       "vm_check.check(live) -> 'owner' over loopback")

    # 5. restart: a NEW process reads the store
    a.stop()
    ok(vm_check.check(cookie, url=f"http://127.0.0.1:{port}/auth/check") is None,
       "vm_check.check -> None while Settings is down (refused)")
    port_b = spare()
    b = Server(tmp, port_b)
    print(f"# server A stopped; server B (new process, same store) on 127.0.0.1:{port_b}")
    st, _, body = get(port_b, "/auth/check", cookie)
    ok(st == 200 and body["user"] == "owner", "session survives the restart: B answers 200 for A's cookie", body)

    # 6. a live cookie skips the passkey offer
    ws, first = await ws_open(port_b, ORIGIN, cookie)
    ok(first["type"] == "auth-ok", "connect with a live cookie: first message is auth-ok, no passkey offer",
       first["type"])
    ws2, first2 = await ws_open(port_b, ORIGIN, "vm_session=" + b64u(os.urandom(32)))
    ok(first2["type"] == "auth" and first2["mode"] == "login", "a forged cookie still gets the offer", first2["type"])
    await ws2.close()

    # 7. return=: /talk/ bounces straight back with no new cookie; //evil and https://x refused
    st, hd, body = get(port_b, f"/auth/session?code={first['code']}&return=/talk/")
    ok(st == 200 and body["return"] == "/talk/" and not set_cookies(hd),
       "signed in + ?return=/talk/ -> 200 return /talk/, nothing minted (no touch)", body)
    for bad in ["//evil", "https://x", "/\\evil.com"]:
        q = urllib.request.quote(bad, safe="")
        # each refusal needs its own code: a fresh cookie connect gives one
        w, f = await ws_open(port_b, ORIGIN, cookie)
        st, hd, body = get(port_b, f"/auth/session?code={f['code']}&return={q}")
        ok(st == 400 and not set_cookies(hd), f"?return={bad} refused (400, no cookie)", st)
        await w.close()

    # 7b. The cookie unlocked this socket with no touch; keyring changes still need one
    ok("addOffer" not in first, "cookie auth-ok carries no addOffer (no registration challenge unprompted)",
       sorted(first))
    pk = tmp / "passkeys.json"
    before = pk.read_text()
    key2 = Authenticator(TS)
    forged = key2.register({"challenge": b64u(os.urandom(32))}, ORIGIN)
    forged["type"] = "passkey-add"
    for label, msg, reply in [("passkey-add", forged, "passkey-added"),
                              ("passkey-rename", {"type": "passkey-rename", "id": ts_key.id, "name": "pwned"},
                               "passkey-renamed"),
                              ("passkey-remove", {"type": "passkey-remove", "id": ts_key.id}, "passkey-renamed")]:
        await ws.send(json.dumps(msg))
        m = await recv_type(ws, reply)
        ok(m.get("why") == "touch to confirm first" and pk.read_text() == before,
           f"cookie socket: {label} without a step-up is refused, store unchanged", m.get("why"))
    offer, m, sent = await step_up(ws, ts_key, "add")
    ok(offer["rpId"] == TS and offer["allow"] == [ts_key.id], "step-up-begin add -> a login challenge for the stored passkey")
    ok(m["type"] == "step-up-ok" and m.get("addOffer", {}).get("mode") == "register",
       "real step-up assertion verified -> step-up-ok carrying the addOffer", m.get("why", m["type"]))
    await ws.send(json.dumps(sent))
    r = await recv_type(ws, "step-up-ok")
    ok(r["type"] == "step-up-failed", "the same step-up replayed is refused", r.get("why"))
    add = key2.register(m["addOffer"], ORIGIN)
    add.update(type="passkey-add", name="smoke-2")
    await ws.send(json.dumps(add))
    r = await recv_type(ws, "passkey-added")
    ok(not r.get("why") and names(r) == ["smoke", "smoke-2"], "after the step-up, passkey-add lands, the name rides on it",
       r.get("why") or names(r))
    await ws.send(json.dumps({"type": "passkey-rename", "id": key2.id, "name": "again"}))
    r = await recv_type(ws, "passkey-renamed")
    ok(r.get("why") and names(r) == ["smoke", "smoke-2"], "the grant was spent on that add: a second change is refused",
       r.get("why"))
    _, m, _ = await step_up(ws, ts_key, "rename", key2.id)
    await ws.send(json.dumps({"type": "passkey-rename", "id": key2.id, "name": "smoke-renamed"}))
    r = await recv_type(ws, "passkey-renamed")
    ok(m["type"] == "step-up-ok" and not r.get("why") and names(r) == ["smoke", "smoke-renamed"],
       "step-up rename -> passkey-rename lands", r.get("why") or names(r))
    _, m, _ = await step_up(ws, key2, "remove", key2.id)   # the new passkey can confirm too
    await ws.send(json.dumps({"type": "passkey-remove", "id": key2.id}))
    r = await recv_type(ws, "passkey-renamed")
    ok(m["type"] == "step-up-ok" and not r.get("why") and names(r) == ["smoke"],
       "step-up remove -> passkey-remove lands; back to the one passkey", r.get("why") or names(r))

    # 8. a passkey LOGIN (not first use) also mints a session: real assertion verified
    w, offer = await ws_open(port_b, ORIGIN)
    ok(offer["mode"] == "login" and offer["allow"] == [ts_key.id], "no cookie -> login offer for the stored passkey")
    await w.send(json.dumps(ts_key.login(offer, ORIGIN)))
    m = await recv_type(w, "auth-ok")
    st, hd, body = get(port_b, f"/auth/session?code={m['code']}")
    raw2 = set_cookies(hd)[0].split(";")[0].split("=", 1)[1] if set_cookies(hd) else ""
    ok(m["type"] == "auth-ok" and st == 200 and raw2 and raw2 != raw,
       "real login verified -> a second, different session cookie")
    await w.close()

    # 9. sign-out ends the session for every app at once
    st, hd, body = get(port_b, "/auth/sign-out", cookie)
    ok(st == 200 and set_cookies(hd) == ["vm_session=; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=0"],
       "GET /auth/sign-out 200 clears the cookie with the same attributes", set_cookies(hd))
    st, _, _ = get(port_b, "/auth/check", cookie)
    ok(st == 401, "/auth/check signed-out -> 401 at once", st)
    ok(vm_check.check(cookie, url=f"http://127.0.0.1:{port_b}/auth/check") is None,
       "vm_check.check(signed out) -> None (401)")
    st, _, _ = get(port_b, "/auth/check", f"vm_session={raw2}")
    ok(st == 200, "the other session (the login one) is untouched by that sign-out", st)
    # ws sign-out on a cookie-bound connection
    w, f = await ws_open(port_b, ORIGIN, f"vm_session={raw2}")
    await w.send(json.dumps({"type": "sign-out"}))
    try:
        await asyncio.wait_for(w.wait_closed(), 3)
    except asyncio.TimeoutError:
        pass
    st, _, _ = get(port_b, "/auth/check", f"vm_session={raw2}")
    ok(st == 401, "ws sign-out on a cookie-bound tab revokes it: /auth/check 401", st)
    await ws.close()

    # 10. localhost:<port> (http) keeps the token flow: no code, no cookie, resume works
    lk = Authenticator("localhost")
    creds = json.loads((tmp / "passkeys.json").read_text())
    spki = lk.key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    creds["credentials"].append({"id": lk.id, "rp_id": "localhost", "spki": b64u(spki), "alg": -7,
                                 "added": "fixture", "name": "smoke-localhost"})
    (tmp / "passkeys.json").write_text(json.dumps(creds))   # a passkey for both RPs, as a two-host user has
    lo = f"http://localhost:{port_b}"
    w, offer = await ws_open(port_b, lo, f"vm_session={raw2}")
    ok(offer["type"] == "auth" and offer["rpId"] == "localhost" and offer["mode"] == "login",
       "localhost origin ignores any cookie and offers the passkey", offer.get("mode"))
    await w.send(json.dumps(lk.login(offer, lo)))
    m = await recv_type(w, "auth-ok")
    ok(m["type"] == "auth-ok" and "code" not in m and m.get("token"), "localhost login -> auth-ok with a token, no code")
    token = m["token"]
    await w.close()
    w, offer = await ws_open(port_b, lo)
    await w.send(json.dumps({"type": "resume", "token": token}))
    m = await recv_type(w, "auth-ok")
    ok(m["type"] == "auth-ok", "localhost reload resumes with the tab token, no touch")
    await w.close()
    b.stop()

    # 11. vm_check fails closed on a timeout (a socket that accepts and never answers)
    srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen(1)
    held = []
    threading.Thread(target=lambda: held.append(srv.accept()), daemon=True).start()
    t0 = time.monotonic()
    r = vm_check.check(cookie, url=f"http://127.0.0.1:{srv.getsockname()[1]}/auth/check")
    dt = time.monotonic() - t0
    ok(r is None and dt < 2.5, "vm_check.check -> None on a timeout, bounded", f"{dt:.2f}s")
    srv.close()

    for p in (port, port_b):
        log = (tmp / f"server-{p}.log").read_text()
        print(f"--- server {p} auth log")
        print("\n".join(l for l in log.splitlines() if '"auth"' in l))


def main():
    with tempfile.TemporaryDirectory(prefix="settled-smoke-") as d:
        asyncio.run(run(Path(d)))
    print(f"\n{'ALL PASS' if not FAILS else 'FAILED: ' + '; '.join(FAILS)}")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
