"""`settled`: the settings desk as one command, from any directory.

    settled enrol               a one-time code: an address with no passkey of its own enrols one
    settled serve [--port N]    the desk, on 127.0.0.1 (default 8770)
    settled install [--port N] [--dry-run]   the launch agent that keeps `serve` running, and its service file
    settled uninstall           boot that agent out, remove its plist; the service file says enabled: false
    settled status              is the agent loaded, which port, how many passkeys
    settled web-room            the front door's room descriptor: refuses until the splice

`make enrol` only works from inside a checkout, in one particular directory, so
enrol belongs to the kit's command instead. So every verb here is `web.py`'s,
reached by name, with no checkout and no cwd.

The launch agent is `dev.sessionkit.settled.desk`, the kit's naming
(`dev.sessionkit.voice.talk` is the precedent). It runs THIS python
(`sys.executable -m settled serve`), never the first python on PATH, so the
env that has websockets and cryptography is the one that serves.

THE SERVICE FILE. `sessionkit service` finds a kit
service from one JSON file its package's own install writes, at
`~/.sessionkit/<package>/services/<name>.json`; the kit writes none and keeps
no list (sessionkit docs/service.md, service.schema.json). So `install` writes
`~/.sessionkit/settled/services/desk.json` with `enabled: true`, and
`uninstall` rewrites it with `enabled: false` and keeps it. The path is fixed
at `~/.sessionkit`, as the kit's reader is: `$SETTLED_HOME` moves settled's
own state, never this file, since the kit would not find it there. A pip or
uv install runs no code, and the kit's setup runs no package's service
install, so `settled install` (the README's step) is what puts it there.

The name is `desk`, not `web`: sessiontools declares its front door as
`tools/services/web.json`, name `web`, and the kit marks two files
with one name as both bad. The label was `dev.sessionkit.settled.web` before
the service file; `install` and `uninstall` boot that one out too and remove its plist,
so a Mac that installed it before does not run two desks on one port.
"""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import subprocess
import sys
from pathlib import Path

LABEL = "dev.sessionkit.settled.desk"
LEGACY_LABELS = ("dev.sessionkit.settled.web",)   # the label before the service file
SERVICE = LABEL.rsplit(".", 1)[1]                 # the service's name: the label's tail
DEFAULT_PORT = 8770


def home() -> Path:
    return Path(os.environ.get("SETTLED_HOME") or Path.home() / ".sessionkit" / "settled")


def plist_path(label: str = LABEL) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"


def service_path() -> Path:
    """`~/.sessionkit/settled/services/desk.json`: where `sessionkit service` reads it.
    Fixed at HOME, like the kit's reader; `$SETTLED_HOME` does not move it."""
    return Path.home() / ".sessionkit" / "settled" / "services" / f"{SERVICE}.json"


def service_record(port: int, enabled: bool) -> dict:
    """The service file, in the kit's shape (sessionkit's service.schema.json,
    schema 1): the marker, the name (the label's tail), the label, whether this
    Mac should run it, and the commands that install and remove it."""
    install = "settled install" if port == DEFAULT_PORT else f"settled install --port {port}"
    return {"service": 1, "name": SERVICE, "label": LABEL, "enabled": enabled,
            "install": install, "uninstall": "settled uninstall",
            "description": "the settings desk, in a browser",
            "port": port, "bind": "127.0.0.1"}


def write_service(rec: dict) -> str | None:
    """Write REC whole to service_path(), atomically: a temp file beside it, then a
    rename, so the kit reads the old file or the new one. None when written, else
    the reason; a failed write leaves the old file as it was."""
    target = service_path()
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(rec, indent=2) + "\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, target)
    except OSError as x:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:      # its folder is the fault (missing, or not a folder)
            pass
        return f"{type(x).__name__}: {x.strerror or x}"
    return None


def installed_port() -> int:
    """The port the service file records, else DEFAULT_PORT: uninstall keeps it."""
    try:
        port = json.loads(service_path().read_text()).get("port")
    except (OSError, ValueError, AttributeError):
        return DEFAULT_PORT
    return port if isinstance(port, int) and not isinstance(port, bool) else DEFAULT_PORT


def boot_out_legacy(uid: int) -> list[str]:
    """Boot out each LEGACY_LABELS job and remove its plist. The labels it removed."""
    gone = []
    for label in LEGACY_LABELS:
        subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"], capture_output=True)
        p = plist_path(label)
        if p.exists():
            p.unlink()
            gone.append(label)
    return gone


def say_service(enabled: bool, why: str | None) -> None:
    if why:
        print(f"settled: could not write {service_path()}: {why}; the previous file is unchanged",
              file=sys.stderr)
    else:
        print(f"settled: {service_path()} says enabled: {'true' if enabled else 'false'}"
              " (`sessionkit service` reads it)")


def plist(port: int) -> dict:
    """The launch agent, as a dict: this python, `-m settled serve --port N`,
    the log under settled's own home, a PATH that finds `mouth` and `uv` tools."""
    log = home() / "log"
    path = os.pathsep.join(p for p in (str(Path.home() / ".local/bin"), "/opt/homebrew/bin",
                                       "/usr/local/bin", "/usr/bin", "/bin") if p)
    return {
        "Label": LABEL,
        "ProgramArguments": [sys.executable, "-m", "settled", "serve", "--port", str(port)],
        "RunAtLoad": True,
        "KeepAlive": True,
        "EnvironmentVariables": {"HOME": str(Path.home()), "PATH": path},
        "StandardOutPath": str(log / "web.log"),
        "StandardErrorPath": str(log / "web.log"),
    }


def cmd_enrol(_: argparse.Namespace) -> int:
    from settled import web
    web.enrol()
    return 0


def cmd_serve(a: argparse.Namespace) -> int:
    import asyncio

    from settled import web
    asyncio.run(web.main(a.port))
    return 0


def cmd_install(a: argparse.Namespace) -> int:
    data = plist(a.port)
    text = plistlib.dumps(data, sort_keys=False).decode()
    if a.dry_run:
        print(text, end="")
        print(f"# would write {plist_path()} and load it, and write {service_path()}; nothing written",
              file=sys.stderr)
        return 0
    (home() / "log").mkdir(parents=True, exist_ok=True)
    p = plist_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    uid = os.getuid()
    for label in boot_out_legacy(uid):
        print(f"settled: booted out the old {label} and removed its plist")
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"], capture_output=True)
    p.write_text(text)
    # The file is the owner's word that this Mac should run it, so it is written
    # before the load: a failed bootstrap then shows in `sessionkit service` as down,
    # with this install command beside it.
    why = write_service(service_record(a.port, True))
    say_service(True, why)
    r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(p)], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"settled install: launchctl bootstrap failed ({r.returncode}): {r.stderr.strip()}",
              file=sys.stderr)
        return 1
    if why:
        return 1
    # localhost, never 127.0.0.1: the desk allows only http://localhost:<port> as its
    # origin, and an IP address cannot hold a passkey (found walking the README).
    print(f"settled: loaded {LABEL} on http://localhost:{a.port}/ (log {home() / 'log' / 'web.log'})")
    print(f"Next: run `settled enrol` here, open http://localhost:{a.port}/, choose Enrol this device "
          f"and type the code.")
    return 0


def cmd_uninstall(_: argparse.Namespace) -> int:
    uid = os.getuid()
    for label in boot_out_legacy(uid):
        print(f"settled: booted out the old {label} and removed its plist")
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"], capture_output=True)
    p = plist_path()
    gone = p.exists()
    if gone:
        p.unlink()
    print(f"settled: {LABEL} booted out; plist {'removed' if gone else 'was not there'}")
    # The kit's rule: an uninstall keeps the file and says enabled: false.
    why = write_service(service_record(installed_port(), False))
    say_service(False, why)
    return 1 if why else 0


def cmd_status(_: argparse.Namespace) -> int:
    r = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
    row = next((ln for ln in r.stdout.splitlines() if ln.endswith(LABEL)), None)
    p = plist_path()
    port = None
    if p.exists():
        args = plistlib.loads(p.read_bytes()).get("ProgramArguments", [])
        if "--port" in args:
            port = args[args.index("--port") + 1]
    try:
        from settled import webauth
        keys = len(webauth.load())
    except Exception as x:   # a missing dependency must not hide the agent's state
        keys = f"? ({type(x).__name__})"
    print(f"agent: {'loaded (' + row.split()[0] + ')' if row else 'not loaded'}; "
          f"plist: {'present' if p.exists() else 'absent'}; port: {port or '-'}; passkeys: {keys}")
    return 0


def cmd_web_room(_: argparse.Namespace) -> int:
    print("settled web-room: not a room yet. The kit's front door cannot proxy a WebSocket "
          "(sessiontools web/proxy.py answers the upgrade 501), and the desk is one; the room "
          "comes with that splice. Until then: `settled serve` on its own port.", file=sys.stderr)
    return 2


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="settled", description="the settings desk as one command")
    sub = ap.add_subparsers(dest="cmd", metavar="COMMAND", required=True)
    sub.add_parser("enrol", help="print a one-time code: an address with no passkey of its own enrols one"
                   ).set_defaults(f=cmd_enrol)
    sp = sub.add_parser("serve", help="serve the desk on 127.0.0.1")
    sp.add_argument("--port", type=int, default=DEFAULT_PORT)
    sp.set_defaults(f=cmd_serve)
    ip = sub.add_parser("install", help=f"install the launch agent {LABEL} that keeps `serve` running, "
                        f"and its service file for `sessionkit service`")
    ip.add_argument("--port", type=int, default=DEFAULT_PORT)
    ip.add_argument("--dry-run", action="store_true", help="print the plist; write and load nothing")
    ip.set_defaults(f=cmd_install)
    sub.add_parser("uninstall", help="boot the launch agent out and remove its plist; its service file "
                   "says enabled: false").set_defaults(f=cmd_uninstall)
    sub.add_parser("status", help="the agent, its port and the passkey count").set_defaults(f=cmd_status)
    sub.add_parser("web-room", help="the front door's room descriptor (refuses until the splice)"
                   ).set_defaults(f=cmd_web_room)
    return ap


def main(argv: list[str] | None = None) -> None:
    a = build_parser().parse_args(argv)
    sys.exit(a.f(a))


if __name__ == "__main__":
    main()
