#!/usr/bin/env python3
"""settled.web [PORT] | enrol (`settled serve` / `settled enrol`): the Settings app in a web browser, over a websocket.

The same app/index.html that runs in Delta Chat, served with app/webxdc.js
beside it. Where Delta carries an update as email through the chat, the
browser sends it down a websocket to here. This keeps the numbered update log
(webxdc's `serial`) and plays the part of the bot plus the applier: a "set" is
applied on the Mac by desk.py's apply() (the set/read core), then the whole state goes
back as a "state" update. The Mac stays the truth.

The ask: see the settings app working in a web browser, over WebSockets.

A tap here changes the same files the phone's applier reads, so the phone
catches up on the applier's next poll (every desk.POLL seconds).

Localhost only; the tailnet reaches it through `tailscale serve`. Needs the
`websockets` package (16.x) and `cryptography`.

LOCKED with a passkey, so a user must authenticate to use it and make
changes. Until a connection has
passed Touch ID or Face ID it gets nothing: no state, no taps. Only the page's
own origins may connect at all, so another web page in his browser cannot
drive this websocket. webauth.py holds the checks.

Changing the keyring (add, rename or remove a passkey) needs a STEP-UP touch
on top, however the connection was unlocked: a vm_session cookie
unlocks with no touch, so a same-origin script could otherwise plant a passkey
of its own. step-up-begin {op, id} gets a fresh login challenge; the assertion
over it (step-up) grants that one change for STEP_TTL seconds.

Every address enrols its first passkey with a one-time code, and so does
the very first passkey of all on an empty store: there is no trust on first use.
Origin is set by the client, and behind `tailscale serve` a tailnet device can
claim any origin it likes, so no origin is trusted to register bare. `settled
enrol` (or `python3 -m settled.web enrol`, `make enrol`) prints a code on the
machine itself; being there is what it proves. The
page sends enrol-check {code} first, which checks the code WITHOUT spending it
(enrol-ok, or enrol-failed {why}), and makes the passkey only after enrol-ok, so
a mistyped code leaves no passkey on the device. It then sends the code
again with the registration (auth-finish, mode "enrol"), which checks and spends it.
"""
import asyncio, json, mimetypes, secrets, subprocess, sys, time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

HERE = Path(__file__).resolve().parent   # the settled package
APP = HERE / "app"                  # installed as the settled wheel: the app rides inside the package
if not APP.is_dir():                # a checkout: settled.app IS the repo's app/ (pyproject package-dir)
    APP = HERE.parent / "app"


def enrol() -> None:
    """`settled enrol`: print a one-time code that lets this host's page enrol its
    first passkey of its own, the very first of all included (no trust on
    first use). Run it on the machine itself: being here is what the code proves.
    The server need not restart; it reads the code's hash. It needs only webauth,
    so it runs before the websockets import: a bare python3 with `cryptography`
    will do."""
    from settled import webauth
    code = webauth.new_enrol_code()
    until = time.strftime("%H:%M:%S", time.localtime(time.time() + webauth.ENROL_TTL))
    print(f"enrol code:  {code}")
    print(f"valid until {until} ({webauth.ENROL_TTL // 60} minutes), for one passkey; "
          f"{webauth.ENROL_TRIES} wrong tries burn it.")
    print("Open Settings at the address with no passkey yet (on a fresh install: http://localhost:8770/,")
    print("or the --port you gave install), choose Enrol this device, type the code.")


if __name__ == "__main__" and sys.argv[1:] == ["enrol"]:
    enrol()
    sys.exit(0)

from websockets.asyncio.server import serve, broadcast  # noqa: E402
from websockets.datastructures import Headers  # noqa: E402
from websockets.http11 import Response  # noqa: E402

from settled import desk  # noqa: E402  the set/read core: its apply(), state() and paths
from settled import paths  # noqa: E402
from settled import webauth  # noqa: E402  the passkey checks
from settled import sessions  # noqa: E402  the host-wide vm_session store

HOST = "127.0.0.1"
LOG: list = []          # the update log, in memory: {"payload", "serial", ...}
CLIENTS: set = set()     # UNLOCKED connections only: nothing is broadcast to a locked one
LAST_STATE: dict = {}
LAST_READ = 0.0         # when the read behind LAST_STATE began; poll() takes only snapshots begun later
ORIGINS: set = set()     # the page's own origins (main() fills it; allowed() may add the tailnet's)
TOKENS: dict = {}        # resume token -> origin, for this server's life: a reload is not a new touch
PORT = 8770              # main() sets it; the tailnet origins carry it
RECHECK = 30.0           # seconds between tailnet lookups on an origin miss
LAST_LOOKUP = float("-inf")  # time.monotonic() of the last tailnet_name() call
CODES: dict = {}         # one-time exchange code -> {"st": that ws's state, "expires": monotonic}
CODE_TTL = 30.0          # seconds a code lives; it is also dropped when its ws closes
STEP_TTL = 60.0          # seconds a step-up challenge, and then its grant, stays good
OPS = ("add", "rename", "remove")   # the keyring changes; each needs its own step-up touch


def https(origin: str) -> bool:
    """The vm_session cookie is for the https (ts.net) origin only; localhost keeps
    the per-tab resume token exactly as before."""
    return origin.startswith("https://")


def cookie_header(headers) -> str:
    """The request's Cookie header as one string. A proxy may split it into several
    Cookie lines; join them (websockets' Headers.get raises on a repeated name)."""
    get_all = getattr(headers, "get_all", None)
    if get_all is not None:
        return "; ".join(get_all("Cookie"))
    return headers.get("Cookie", "") or ""


def new_code(st: dict) -> str:
    """A one-time code for the auth-ok on this ws: single use, CODE_TTL seconds,
    bound to this connection's state, and dropped when the ws closes."""
    code = secrets.token_urlsafe(32)
    CODES[code] = {"st": st, "expires": time.monotonic() + CODE_TTL}
    return code


def drop_codes(st: dict) -> None:
    """A closed ws invalidates its codes; expired ones go at the same time."""
    now = time.monotonic()
    for c in [c for c, e in CODES.items() if e["st"] is st or e["expires"] <= now]:
        CODES.pop(c, None)


def tailnet_name() -> str:
    """This Mac's tailnet DNS name, '' if tailscale does not answer."""
    try:
        r = subprocess.run(["tailscale", "status", "--self", "--json"], capture_output=True, text=True, timeout=5)
        return json.loads(r.stdout)["Self"]["DNSName"].rstrip(".")
    except Exception:
        return ""


def tailnet_origins(ts: str, port: int) -> set:
    return {f"https://{ts}", f"https://{ts}:{port}"} if ts else set()


def resolve_origins() -> None:
    """Look up the tailnet name now and allow its origins."""
    global LAST_LOOKUP
    LAST_LOOKUP = time.monotonic()
    ORIGINS.update(tailnet_origins(tailnet_name(), PORT))


async def allowed(origin: str) -> bool:
    """Is this one of the page's own origins? On a miss, look the tailnet name up
    again (at most once every RECHECK seconds) before saying no. launchd can start
    this server before Tailscale answers; main()'s lookup then finds no name and,
    without this, https://<host>.ts.net was refused until a restart."""
    global LAST_LOOKUP
    if origin in ORIGINS:
        return True
    if time.monotonic() - LAST_LOOKUP >= RECHECK:
        LAST_LOOKUP = time.monotonic()   # before the await: misses meanwhile do not look too
        before = set(ORIGINS)
        await asyncio.to_thread(resolve_origins)
        if ORIGINS != before:
            print(f"  origins allowed (tailnet came up): {sorted(ORIGINS)}", flush=True)
    return origin in ORIGINS


def add(payload: dict, info: str = "") -> dict:
    """Append one update to the log, give it the next serial, send it to everyone."""
    u = {"payload": payload, "serial": len(LOG) + 1}
    if info:
        u["info"] = info
    LOG.append(u)
    for x in LOG:          # max_serial is the log's length, as Delta reports it
        x["max_serial"] = len(LOG)
    broadcast(CLIENTS, json.dumps({"type": "update", "update": u}))
    return u


def since(n: int) -> list:
    """What a client that has serial n still needs. A "state" replaces all before it,
    so a new page starts at the newest state instead of replaying the whole log."""
    start = n
    if n == 0:
        for u in reversed(LOG):
            if u["payload"].get("kind") == "state":
                start = u["serial"] - 1
                break
    return LOG[start:]


def event(path: list, old, new, by: str) -> int:
    """The config change, in the ears' event log, marked as from the web (the
    phone's applier marks its own `phone`); desk.event writes it, 127 with no writer."""
    return desk.event(path, old, new, by, source="web")


def tap(p: dict) -> dict:
    """One "set", applied the way the phone applier applies a tap. Runs in a thread."""
    global LAST_STATE, LAST_READ
    path = list(p.get("path") or [])
    old = desk.lookup(LAST_STATE, path)
    try:
        result = desk.apply(path, p.get("value"))
    except Exception as x:  # a bad tap must not kill the server
        result = f"error: {type(x).__name__}: {x}"
    ev = event(path, old, p.get("value"), p.get("by") or "") \
        if result == "ok" and path not in desk.ACTIONS else None
    t0 = time.time()
    LAST_STATE, LAST_READ = desk.said(desk.state(), path, result), t0
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "via": "web", "by": p.get("by"),
           "path": p.get("path"), "value": p.get("value"), "result": result, "event_rc": ev}
    desk.log(rec)
    print(json.dumps(rec), flush=True)
    return LAST_STATE


async def unlock(ws, st: dict, m: dict) -> None:
    """One message on a LOCKED connection: resume, pre-check an enrol code, or
    finish a passkey ceremony."""
    origin = st["origin"]
    if m.get("type") == "enrol-check":   # before the ceremony: never registers, never unlocks
        if any(c.get("rp_id") == webauth.rp_of(origin) for c in webauth.load()):
            why = "this address has a passkey: unlock with it, then add another"   # the code is left untouched
        else:
            why = webauth.check_enrol_code(m.get("code"), consume=False)
        print(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "auth": "enrol-check",
                          "origin": origin, "result": why or "ok"}), flush=True)
        await ws.send(json.dumps({"type": "enrol-failed", "why": why} if why else {"type": "enrol-ok"}))
        return
    if m.get("type") == "resume":
        if TOKENS.get(m.get("token")) == origin:
            st["token"] = m["token"]
        else:
            await ws.send(json.dumps({"type": "auth-failed", "why": "resume", "quiet": True}))
            return
    elif m.get("type") == "auth-finish":
        creds, chal, st["chal"] = webauth.load(), st["chal"], None   # a challenge is used once
        if m.get("mode") == "register":   # never on a locked page, an empty store included (no TOFU)
            why = "a passkey exists: unlock with it, then add another" if creds else \
                "the first passkey needs the enrol code: run `settled enrol` on this machine"
        elif m.get("mode") == "enrol":    # a host with no passkey of its own, by the code from `make enrol`
            if any(c.get("rp_id") == webauth.rp_of(origin) for c in creds):
                why = "this address has a passkey: unlock with it, then add another"   # the code is left unspent
            else:   # checked again and SPENT here: a pre-check alone never enrols
                why = webauth.check_enrol_code(m.get("code"))
                if not why:
                    cred, why = webauth.register(m, chal, origin)
                    if cred:
                        webauth.save(creds + [cred])
        else:
            why = webauth.login(m, chal, origin, creds)
        print(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "auth": m.get("mode"),
                          "origin": origin, "result": why or "ok"}), flush=True)
        if why:
            await ws.send(json.dumps({"type": "auth-failed", "why": why}))
            await offer(ws, st)
            return
        st["token"] = secrets.token_urlsafe(32)
        TOKENS[st["token"]] = origin
    else:
        return                           # anything else waits for the lock
    await authed(ws, st)


async def authed(ws, st: dict) -> None:
    """The connection is unlocked: say auth-ok. On https it carries a one-time
    `code` the page swaps for the vm_session cookie at GET /auth/session (a ws
    message cannot set a cookie). localhost gets no code: its token flow is as before."""
    st["authed"] = True
    CLIENTS.add(ws)
    ok = {"type": "auth-ok", "token": st["token"], "user": sessions.OWNER, "since": time.strftime("%H:%M")}
    if https(st["origin"]):
        ok["code"] = new_code(st)
    await ws.send(json.dumps({**ok, **keyring(st)}))


def keyring(st: dict) -> dict:
    """The passkeys. No offer to add one rides along any more: an unlocked socket (a
    cookie may have unlocked it, with no touch) gets one only from a step-up."""
    return {"passkeys": [{k: c.get(k) for k in ("id", "name", "added", "rp_id")} for c in webauth.load()]}


def add_offer(st: dict) -> dict:
    """A registration offer for one more passkey (adding another key),
    its challenge kept on this socket for the passkey-add. Issued only by a step-up for
    op "add"."""
    creds = webauth.load()
    o, st["add_chal"] = webauth.offer([], webauth.rp_of(st["origin"]), may_register=True)
    o["exclude"] = [c["id"] for c in creds if c.get("rp_id") == o["rpId"]]
    return o


async def step_up_begin(ws, st: dict, m: dict) -> None:
    """step-up-begin {op, id?}: a fresh login challenge for ONE keyring change. It
    replaces any earlier one; it is good for STEP_TTL seconds and for one assertion."""
    st.pop("step", None)
    op, pid = m.get("op"), m.get("id")
    if op not in OPS:
        why = "no such change"
    elif op != "add" and not (isinstance(pid, str) and pid):
        why = "which passkey?"
    else:
        o, chal = webauth.offer(webauth.load(), webauth.rp_of(st["origin"]), may_register=False)
        if o["mode"] == "login":
            st["step"] = {"chal": chal, "issued": time.monotonic(), "op": op, "id": pid if op != "add" else None}
            await ws.send(json.dumps({"type": "step-up", "op": op, "id": st["step"]["id"],
                                      "challenge": o["challenge"], "rpId": o["rpId"], "allow": o["allow"]}))
            return
        why = "no passkey here to confirm with"
    await ws.send(json.dumps({"type": "step-up-failed", "why": why}))


async def step_up(ws, st: dict, m: dict) -> None:
    """step-up {assertion}: the touch. The outstanding challenge is spent whatever the
    result, so a replay finds none. A good touch grants ONE change: the op (and id) it
    was begun for, within STEP_TTL seconds."""
    step = st.pop("step", None)
    if step is None:
        why = "step up first"
    elif time.monotonic() - step["issued"] > STEP_TTL:
        why = "stale: touch again"
    else:
        why = webauth.login(m, step["chal"], st["origin"], webauth.load())
    log_auth("step-up", st["origin"], why or "ok")
    if why:
        await ws.send(json.dumps({"type": "step-up-failed", "why": why}))
        return
    st["grant"] = {"at": time.monotonic(), "op": step["op"], "id": step["id"]}
    st.pop("add_chal", None)
    ok = {"type": "step-up-ok", "op": step["op"], "id": step["id"]}
    if step["op"] == "add":
        ok["addOffer"] = add_offer(st)
    await ws.send(json.dumps(ok))


def granted(st: dict, op: str, pid=None) -> str:
    """'' if this socket holds a live grant for exactly this change, else why not.
    The grant is spent either way: one keyring change per touch, refused or not."""
    g = st.pop("grant", None)
    if g is None:
        return "touch to confirm first"
    if time.monotonic() - g["at"] > STEP_TTL:
        return "too long since the touch: touch again"
    if g["op"] != op or (op != "add" and g["id"] != pid):
        return "that touch was for another change: touch again"
    return ""


def hello() -> dict:
    """The first message on every ws: who the page is, and the host it is on, so
    webxdc.js can name the passkey "<host> Settings" before any state arrives."""
    h = desk.host()
    return {"type": "hello", "selfAddr": f"web@{h}", "selfName": f"{sessions.OWNER} (browser)", "host": h}


async def offer(ws, st: dict) -> None:
    # never "register" on a locked page: an empty store is offered enrol too (no TOFU)
    o, st["chal"] = webauth.offer(webauth.load(), webauth.rp_of(st["origin"]), may_register=False)
    await ws.send(json.dumps(o))


async def handler(ws):
    origin = ws.request.headers.get("Origin", "")
    st = {"origin": origin, "authed": False, "chal": None, "token": None, "session": None}
    try:
        await ws.send(json.dumps(hello()))
        if not await allowed(origin):
            await ws.send(json.dumps({"type": "auth-refused", "origin": origin,
                                      "open": sorted(o for o in ORIGINS if "localhost" not in o) or sorted(ORIGINS)}))
            return
        raw_id = sessions.cookie_value(cookie_header(ws.request.headers)) if https(origin) else None
        if raw_id and sessions.lookup(raw_id):     # a live vm_session: no touch, no passkey offer
            st["session"] = sessions._hash(raw_id)
            print(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "auth": "cookie",
                              "origin": origin, "result": "ok"}), flush=True)
            await authed(ws, st)
        else:
            await offer(ws, st)
        async for raw in ws:
            try:
                m = json.loads(raw)
            except ValueError:
                continue
            if not st["authed"]:
                await unlock(ws, st, m)
                continue
            if m.get("type") == "step-up-begin":   # every keyring change needs a fresh touch
                await step_up_begin(ws, st, m)
                continue
            if m.get("type") == "step-up":
                await step_up(ws, st, m)
                continue
            if m.get("type") == "passkey-add":     # unlocked: register one more (a YubiKey, another device)
                chal = st.pop("add_chal", None)    # single use, as is the grant
                why = granted(st, "add")
                if not why:                        # the name rides on the add (webauth keeps msg["name"])
                    cred, why = webauth.register(m, chal, st["origin"])
                    if cred:
                        webauth.save(webauth.load() + [cred])
                log_auth("add", st["origin"], why or "ok")
                await ws.send(json.dumps({"type": "passkey-added", "why": why, **keyring(st)}))
                continue
            if m.get("type") == "passkey-rename":  # 📝 on the You page: edit the passkey's name
                why = granted(st, "rename", m.get("id"))
                if not why:
                    creds = webauth.load()
                    for c in creds:
                        if c["id"] == m.get("id"):
                            c["name"] = webauth.clean_label(str(m.get("name") or "").strip())
                    webauth.save(creds)
                log_auth("rename", st["origin"], why or "ok")
                await ws.send(json.dumps({"type": "passkey-renamed", "why": why, **keyring(st)}))
                continue
            if m.get("type") == "passkey-remove":  # remove the passkey
                why = granted(st, "remove", m.get("id"))
                if not why:
                    creds = webauth.load()
                    keep = [c for c in creds if c["id"] != m.get("id")]
                    why = "that's the last passkey: add another first, or the page would open to anyone" \
                        if not keep else "" if len(keep) < len(creds) else "no such passkey"
                if not why:
                    webauth.save(keep)
                print(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "auth": "remove",
                                  "origin": st["origin"], "result": why or "ok"}), flush=True)
                await ws.send(json.dumps({"type": "passkey-renamed", "why": why, **keyring(st)}))
                continue
            if m.get("type") == "sign-out":        # this tab's token dies; the next visit touches again
                TOKENS.pop(st["token"], None)
                if st.get("session"):              # and the vm_session too: /auth/check is 401 for every app
                    sessions.revoke_hash(st["session"])
                    log_auth("sign-out", st["origin"], "revoked")
                await ws.close()
                return
            if m.get("type") == "since":
                for u in since(int(m.get("serial") or 0)):
                    await ws.send(json.dumps({"type": "update", "update": u}))
                await ws.send(json.dumps({"type": "caught-up"}))
            elif m.get("type") == "update":
                p = (m.get("update") or {}).get("payload") or {}
                add(p)                                   # everyone sees the tap, as in a chat
                if p.get("kind") == "set":
                    s = await asyncio.to_thread(tap, p)
                    add(s)
    finally:
        CLIENTS.discard(ws)
        drop_codes(st)


def reply(status: int, body: dict, *cookies: str) -> Response:
    """A small JSON answer for the /auth/ routes: never cached, maybe a Set-Cookie."""
    data = (json.dumps(body) + "\n").encode()
    h = Headers({"Content-Type": "application/json", "Content-Length": str(len(data)),
                 "Cache-Control": "no-store"})
    for c in cookies:
        h["Set-Cookie"] = c
    return Response(status, {200: "OK", 400: "Bad Request", 401: "Unauthorized", 403: "Forbidden"}[status],
                    h, data)


SAME_ORIGIN = ("same-origin", "none")   # the Sec-Fetch-Site values a state-changing /auth/ GET accepts


def cross_origin(headers) -> bool:
    """Is this request from another origin? GET /auth/sign-out and /auth/session
    change state on the cookie, and SameSite=Strict still sends it from a same-SITE
    page on another PORT (one no-cors fetch signed the user out). With
    Sec-Fetch-Site, only same-origin and none (typed, bookmarked) pass; a doubled
    header is refused. Without it (an old browser, curl, a loopback caller) the
    request passes unless it carries an Origin that is not one of ours. A no-cors
    GET sends no Origin at all, which is why Sec-Fetch-Site is the primary signal."""
    sites = headers.get_all("Sec-Fetch-Site")
    if sites:
        return len(sites) != 1 or sites[0].strip().lower() not in SAME_ORIGIN
    return any(o not in ORIGINS for o in headers.get_all("Origin"))


def refuse_cross_origin(route: str, headers) -> Response:
    """403 for cross_origin(): no Set-Cookie and nothing done (no revoke, no code consumed)."""
    site = ",".join(headers.get_all("Sec-Fetch-Site")) or "absent"
    log_auth(route, ",".join(headers.get_all("Origin")) or "-",
             f"refused: cross-origin (Sec-Fetch-Site {site}"
             f"{', cookie' if sessions.cookie_value(cookie_header(headers)) else ''})")
    return reply(403, {"ok": False, "why": "cross-origin"})


def session_cookie(raw_id: str) -> str:
    """Host-only (no Domain), whole host (Path=/), https only, out of script's reach,
    never sent on a cross-site request. The interface agreed with Talk."""
    return f"{sessions.COOKIE}={raw_id}; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age={sessions.TTL}"


def exchange(query: str) -> Response:
    """GET /auth/session?code=C[&return=R]: swap a one-time code from an https
    auth-ok for the vm_session cookie. The code is consumed on the first try, good
    or bad. A refused return= answers 400 and mints nothing. A ws that is already
    on a live session (it came in with the cookie) mints nothing either: the page
    is just sent on to return=, so a sign-in from Talk bounces straight back.
    page() has already refused a cross-origin request, so its code survives."""
    q = parse_qs(query)
    codes, rets = q.get("code", []), q.get("return", [])
    entry = CODES.pop(codes[0], None) if len(codes) == 1 else None
    if len(rets) > 1 or (rets and sessions.safe_return(rets[0]) is None):
        return reply(400, {"ok": False, "why": "return must be a path on this site"})
    ret = rets[0] if rets else None
    if entry is None or entry["expires"] <= time.monotonic():
        return reply(401, {"ok": False})
    st = entry["st"]
    if st.get("session"):                # already on a session: never mint a second one from it,
        if sessions.lookup_hash(st["session"]):   # and a revoked one is not revived without a touch
            return reply(200, {"ok": True, "return": ret})
        return reply(401, {"ok": False})
    raw_id, _ = sessions.mint(sessions.OWNER)
    st["session"] = sessions._hash(raw_id)
    print(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "auth": "session",
                      "origin": st["origin"], "result": "minted"}), flush=True)
    return reply(200, {"ok": True, "return": ret}, session_cookie(raw_id))


def log_auth(kind: str, origin: str, result: str) -> None:
    print(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "auth": kind,
                      "origin": origin, "result": result}), flush=True)


def check(headers) -> Response:
    """GET /auth/check: the other apps' question, asked over loopback with the
    browser's Cookie header forwarded as it came. A live vm_session answers 200
    {"user", "expires"}; anything else (no cookie, forged, expired, signed out)
    answers 401 with an empty object, so nothing about the session is revealed."""
    rec = sessions.lookup(sessions.cookie_value(cookie_header(headers)))
    if rec is None:
        return reply(401, {})
    return reply(200, {"user": rec["user"], "expires": rec["expires"]})


def clear_cookie() -> str:
    """The same attributes as session_cookie(), so the browser replaces that cookie, and Max-Age=0."""
    return f"{sessions.COOKIE}=; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age=0"


def sign_out(headers) -> Response:
    """GET /auth/sign-out: revoke the session the request's cookie names (so
    /auth/check answers 401 for every app at once) and clear the cookie. It is a
    GET because the parser takes nothing else (design.md, measured); SameSite=Strict
    keeps a cross-site page from firing it with the cookie, and page() refuses a
    same-site one from another port (cross_origin()). Always 200: signing
    out with no live session is still signed out."""
    raw_id = sessions.cookie_value(cookie_header(headers))
    if raw_id and sessions.revoke(raw_id):
        log_auth("sign-out", "GET /auth/sign-out", "revoked")
    return reply(200, {"ok": True}, clear_cookie())


def page(connection, request):
    """Plain HTTP for everything but /ws: the /auth/ routes, then the app's files."""
    if request.path == "/ws":
        return None
    url = urlsplit(request.path)
    if url.path in ("/auth/session", "/auth/sign-out") and cross_origin(request.headers):
        return refuse_cross_origin(url.path.rsplit("/", 1)[1], request.headers)   # before anything is done
    if url.path == "/auth/session":
        return exchange(url.query)
    if url.path == "/auth/check":                          # read-only, asked over loopback: no origin check
        return check(request.headers)
    if url.path == "/auth/sign-out":
        return sign_out(request.headers)
    if url.path.startswith("/docs/"):                      # the kit's documents: one file, read where installed
        return doc(url.path[len("/docs/"):])
    name = request.path.split("?")[0].lstrip("/") or "index.html"
    f = APP / name
    if "/" in name or not f.is_file():
        return connection.respond(404, "not found\n")
    body = f.read_bytes()
    ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
    return Response(200, "OK", Headers({"Content-Type": ctype, "Content-Length": str(len(body)),
                                        "Cache-Control": "no-store"}), body)


def doc(name: str) -> Response:
    """One of the kit's installed documents as text/markdown; 404 with the path it would
    be at when it is not installed, so the screen can say so and point at the repo."""
    f = paths.docs() / name
    if "/" in name or not name.endswith(".md") or not f.is_file():
        body = json.dumps({"missing": str(paths.docs() / name)}).encode()
        return Response(404, "Not Found", Headers({"Content-Type": "application/json", "Content-Length": str(len(body)),
                                                   "Cache-Control": "no-store"}), body)
    body = f.read_bytes()
    return Response(200, "OK", Headers({"Content-Type": "text/markdown; charset=utf-8", "Content-Length": str(len(body)),
                                        "Cache-Control": "no-store"}), body)


async def poll():
    """Devices come and go and the capture moves: push when the Mac changed (the applier's poll).
    It takes the applier's own read (desk.snapshot) rather than reading the devices a
    second time; it reads them itself only while the applier is down."""
    global LAST_STATE, LAST_READ
    while True:
        await asyncio.sleep(desk.POLL)
        try:
            got = await asyncio.to_thread(desk.snapshot, LAST_READ)
        except Exception as x:
            print(f"poll error: {type(x).__name__}: {x}", flush=True)
            continue
        if got is None:
            continue
        now, LAST_READ = got
        if not desk.same(now, LAST_STATE):
            LAST_STATE = now
            add(now)


async def main(port: int):
    global LAST_STATE, LAST_READ, PORT
    PORT = port
    ORIGINS.add(f"http://localhost:{port}")
    resolve_origins()   # may find no tailnet yet: allowed() looks again on a miss
    t0 = time.time()
    LAST_STATE, LAST_READ = await asyncio.to_thread(desk.state), t0
    add(LAST_STATE)
    async with serve(handler, HOST, port, process_request=page):
        # the page's own origin, not the bind address: 127.0.0.1 is refused at /ws
        print(f"settings web: http://localhost:{port}/  (bound to {HOST}; app {APP}, websocket /ws)", flush=True)
        print(f"  origins allowed: {sorted(ORIGINS)}; passkeys: {webauth.STORE} ({len(webauth.load())})", flush=True)
        await poll()


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 8770))
