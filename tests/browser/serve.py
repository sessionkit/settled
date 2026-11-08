"""The browser harness: the real web.main on a spare port, TLS optional.
usage: serve.py <repo root> <port> (tls <cert> <key> | plain)
Temp stores only (SETTINGS_PASSKEYS / SETTINGS_SESSIONS set by the caller)."""
import asyncio, functools, ssl, sys
sys.path.insert(0, sys.argv[1])
from settled import web
port, tls = int(sys.argv[2]), sys.argv[3] == "tls"
web.tailnet_name = lambda: ""
web.desk.state = lambda: {"kind": "state"}
async def forever(): await asyncio.Event().wait()
web.poll = forever
if tls:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain(sys.argv[4], sys.argv[5])
    web.serve = functools.partial(web.serve, ssl=ctx)
    web.ORIGINS.add(f"https://localhost:{port}")
asyncio.run(web.main(port))
