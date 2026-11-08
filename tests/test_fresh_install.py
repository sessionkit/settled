"""The fresh-install gate: what a stranger's Mac gets from `uv tool install settled`.

A fresh-install failure made this test. `make enrol` only
worked inside a checkout; the app wrote a dot folder of its own, which the kit never
reads, joined on a laptop by two hand-made symlinks; the deployed venv had no
tomlkit. Everything here runs under a scratch HOME of its own, seeded from
the fixtures beside this file (`tests/fixtures/sessionkit/`, the kit's own
voice and mail schemas); nothing from the Mac that runs it. It is RED until
the package exists, then it is the merge gate for
every settled branch.

The steps, in file order:

1. `pyproject.toml` names the package `settled` and a `settled` script.
2. The scratch HOME holds the kit's registry and no other dot folder.
3. `uv build` the wheel; `uv tool install` it into a scratch tool dir with
   the extra that brings sessiontools (`kit`; env SETTLED_INSTALL_EXTRA).
4. `settled enrol` from a foreign cwd: exit 0, prints the code, writes its
   state under `~/.sessionkit/settled/`, and no dot folder but `~/.sessionkit`.
5. `settled serve --port N` answers 200 at `/`.
6. The registry draws ears, mouth and mail from the seeded folders by the
   kit's rule (`.<tool>.schema.json` first); a host-layer write to
   `mouth.voice` is read back by sessiontools' own reader (`host_files`,
   `read_json_object`, `find_schema`: sessiontools 0.3.3's, the release the
   [kit] extra requires).
7. No symlink exists anywhere under the scratch HOME.
8. A desk set through the core (desk.py, the path web.py's tap takes):
   lookup, apply, event, state, said, log and keep, each "ok".
9. One home, proven: after the install (run under the scratch
   HOME, uv's own cache and tool dirs outside it) and every run above, the
   scratch HOME's only top-level entry is `.sessionkit`. Not just dot
   folders: a `Library/` or a stray file fails it too.

Subprocesses go by Popen, as test_enrol.py's do: test_voice_browser.py swaps
the shared subprocess.run for a fake at import.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "sessionkit"
STUBS = Path(__file__).resolve().parent / "stubs"
TOOLS = ("ears", "mouth", "mail")
EXTRA = os.environ.get("SETTLED_INSTALL_EXTRA", "kit")


def run(args, *, cwd=None, env=None, timeout=120):
    """(rc, stdout, stderr); a hang is a failure with both streams shown."""
    with subprocess.Popen(args, cwd=cwd, env=env, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True) as p:
        try:
            out, err = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            p.kill()
            out, err = p.communicate()
            pytest.fail(f"{args[0]} did not finish in {timeout}s\n--- stdout\n{out}\n--- stderr\n{err}")
    return p.returncode, out, err


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_http(url: str, seconds: float) -> int:
    """The status `url` answers within SECONDS, else -1."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code
        except (urllib.error.URLError, ConnectionError, socket.timeout):
            time.sleep(0.2)
    return -1


def env_for(install: dict, home: Path, scratch: Path) -> dict:
    """A stranger's environment: the scratch HOME, the scratch tool bin first on
    PATH, the suite's tmux stub, the system's bin dirs, nothing of this Mac's."""
    (scratch / "tmux").mkdir(exist_ok=True)
    return {
        "HOME": str(home),
        "PATH": os.pathsep.join([str(install["bin"]), str(STUBS), "/usr/bin", "/bin"]),
        "TMUX_TMPDIR": str(scratch / "tmux"),
        "LANG": "C.UTF-8",
    }


# --- 1. the package -----------------------------------------------------------

@pytest.fixture(scope="module")
def pyproject() -> dict:
    p = REPO / "pyproject.toml"
    if not p.is_file():
        pytest.fail("RED: no pyproject.toml at the repo root, so there is no `settled` to "
                    "install. Every step below waits on it.")
    data = tomllib.loads(p.read_text())
    proj = data.get("project", {})
    assert proj.get("name") == "settled", f"[project] name is {proj.get('name')!r}, not 'settled'"
    scripts = proj.get("scripts", {})
    assert "settled" in scripts, f"[project.scripts] has no `settled` (has {sorted(scripts)})"
    return data


def test_1_pyproject_names_settled_and_its_command(pyproject):
    assert pyproject["project"]["name"] == "settled"


# --- 2. the scratch HOME ------------------------------------------------------

@pytest.fixture(scope="module")
def home(tmp_path_factory) -> Path:
    """A fresh HOME holding only the kit's registry, copied from the fixtures."""
    h = tmp_path_factory.mktemp("home")
    shutil.copytree(FIXTURES, h / ".sessionkit")
    return h


def stray_dot_folders(home: Path) -> list:
    """Dot folders under HOME other than the kit's: a fresh install makes only ~/.sessionkit."""
    return sorted(p.name for p in home.iterdir() if p.name.startswith(".") and p.name != ".sessionkit")


def test_2_scratch_home_holds_the_kit_registry_and_no_other_dot_folder(home):
    voice = home / ".sessionkit" / "voice" / "settings"
    mail = home / ".sessionkit" / "mail" / "settings"
    for f in (voice / ".ears.schema.json", voice / ".mouth.schema.json", voice / "ears.json",
              voice / "mouth.json", mail / "mail.schema.json", mail / "mail.json"):
        assert f.is_file(), f"fixture missing: {f.relative_to(home)}"
    assert not stray_dot_folders(home)


# --- 3. build and install -----------------------------------------------------

def uv_cache(uv: str) -> str:
    """The cache uv uses for THIS run's environment, read before HOME moves: the
    install reuses it (no network trip for a wheel already fetched), and it sits
    outside the scratch HOME, so it is never counted as the install's write."""
    rc, out, err = run([uv, "cache", "dir"], timeout=30)
    assert rc == 0 and out.strip(), f"uv cache dir failed:\n{out}\n{err}"
    return out.strip()


@pytest.fixture(scope="module")
def install(pyproject, home, tmp_path_factory) -> dict:
    """Build the wheel and `uv tool install` it with HOME set to the scratch HOME,
    so a write the install makes there is caught by step 9. uv's own folders
    (cache, tools, their bin, any python it fetches) are pointed outside it: they
    are uv's, a stranger's uv already has them, and they are not settled's."""
    uv = shutil.which("uv")
    if not uv:
        pytest.fail("the gate needs `uv` on PATH to build and install the wheel (make venv uses it too)")
    root = tmp_path_factory.mktemp("install")
    dist = root / "dist"
    env = {**os.environ, "HOME": str(home), "UV_CACHE_DIR": uv_cache(uv),
           "UV_TOOL_DIR": str(root / "tools"), "UV_TOOL_BIN_DIR": str(root / "bin"),
           "UV_PYTHON_INSTALL_DIR": str(root / "python")}
    rc, out, err = run([uv, "build", "--wheel", "--out-dir", str(dist), str(REPO)], env=env, timeout=300)
    assert rc == 0, f"uv build failed:\n{out}\n{err}"
    wheels = sorted(dist.glob("settled-*.whl"))
    assert wheels, f"no settled wheel in {dist}: {sorted(p.name for p in dist.iterdir())}"
    spec = f"{wheels[-1]}[{EXTRA}]" if EXTRA else str(wheels[-1])
    rc, out, err = run([uv, "tool", "install", "--python", sys.executable, spec], env=env, timeout=600)
    assert rc == 0, f"uv tool install {spec!r} failed:\n{out}\n{err}"
    cmd = root / "bin" / "settled"
    assert cmd.is_file(), f"no `settled` in {root / 'bin'}: {sorted(p.name for p in (root / 'bin').iterdir())}"
    python = root / "tools" / "settled" / "bin" / "python"
    assert python.exists(), f"no interpreter at {python}"
    return {"bin": root / "bin", "settled": cmd, "python": python, "root": root}


def test_3_wheel_installs_as_a_tool_with_settled_on_its_bin(install):
    assert install["settled"].is_file()


# --- 4. enrol from anywhere ---------------------------------------------------

def test_4_enrol_from_a_foreign_cwd_writes_state_under_sessionkit(install, home, tmp_path):
    foreign = tmp_path / "elsewhere"
    foreign.mkdir()
    rc, out, err = run([str(install["settled"]), "enrol"], cwd=foreign, env=env_for(install, home, tmp_path))
    assert rc == 0, f"settled enrol exited {rc} from {foreign}\n{out}\n{err}"
    assert "enrol code:" in out, f"no enrol code printed:\n{out}\n{err}"
    state = home / ".sessionkit" / "settled"
    assert state.is_dir(), (f"settled wrote no state under {state}; "
                            f"under HOME: {sorted(p.name for p in home.iterdir())}")
    assert (state / "enrol.json").is_file(), f"no enrol.json in {state}: {sorted(p.name for p in state.iterdir())}"
    assert not stray_dot_folders(home), f"settled wrote a dot folder the kit never reads: {stray_dot_folders(home)}"


# --- 5. serve -----------------------------------------------------------------

def test_5_serve_answers_200_at_root(install, home, tmp_path):
    port = free_port()
    env = env_for(install, home, tmp_path)
    p = subprocess.Popen([str(install["settled"]), "serve", "--port", str(port)], cwd=tmp_path, env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        status = wait_http(f"http://127.0.0.1:{port}/", 20)
    finally:
        p.terminate()
        try:
            out, err = p.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
            out, err = p.communicate()
    assert status == 200, f"settled serve --port {port}: / answered {status}\n{out}\n{err}"
    assert "Traceback" not in err, err


# --- 6. the registry, and a write read back by the kit ------------------------

DRAW = r"""
import json, sys
from settled import schemas
reg = schemas.registry()
out = {"registry": list(reg),
       "schema": {t: str(schemas.schema_path(t)) for t in reg},
       "file": {t: str(schemas.file_path(t)) for t in reg}}
out["write"] = schemas.write("mouth", ["voice"], "bm_daniel")
print(json.dumps(out))
"""

READ_BACK = r"""
import json
from sessiontools.settings import find_schema, host_files, package_dir, read_json_object
d = package_dir("voice")
schema = find_schema("mouth")[0]
voice = None
for p in host_files("mouth").host:   # the host layer in read order, as the kit reads it
    voice = read_json_object(p).get("voice", voice)
print(json.dumps({"dir": str(d), "schema": str(schema), "voice": voice}))
"""


def test_6_registry_draws_the_kit_and_a_host_write_is_read_back(install, home, tmp_path):
    env = env_for(install, home, tmp_path)
    rc, out, err = run([str(install["python"]), "-I", "-c", DRAW], cwd=tmp_path, env=env)
    assert rc == 0, f"the registry did not draw:\n{out}\n{err}"
    drawn = json.loads(out.strip().splitlines()[-1])
    missing = [t for t in TOOLS if t not in drawn["registry"]]
    assert not missing, f"not drawn from the kit's registry: {missing}; drawn: {drawn['registry']}"
    voice = home / ".sessionkit" / "voice" / "settings"
    assert drawn["schema"]["mouth"] == str(voice / ".mouth.schema.json"), drawn["schema"]
    assert drawn["file"]["mouth"] == str(voice / "mouth.json"), drawn["file"]
    assert drawn["write"] == "ok", f"the host-layer write was refused: {drawn['write']}"
    assert json.loads((voice / "mouth.json").read_text())["voice"] == "bm_daniel"
    rc, out, err = run([str(install["python"]), "-I", "-c", READ_BACK], cwd=tmp_path, env=env)
    assert rc == 0, f"sessiontools could not read it back (is the `{EXTRA}` extra installed?):\n{out}\n{err}"
    back = json.loads(out.strip().splitlines()[-1])
    assert back["dir"] == str(voice), back
    assert back["schema"] == str(voice / ".mouth.schema.json"), back
    assert back["voice"] == "bm_daniel", back


# --- 7. nothing joined by a symlink ------------------------------------------

def test_7_no_symlink_under_the_scratch_home(home):
    links = [str(p.relative_to(home)) for p in home.rglob("*") if p.is_symlink()]
    assert not links, f"symlinks under the scratch HOME, the laptop shortcut this gate forbids: {links}"


# --- 8. a desk set through the core ------------------------------------------

DESK_SET = r"""
import json, time
from settled import desk
sets = [(["global", "allow"], False),       # notifications/global.json, settled's own
        (["ears", "listen"], False),         # the ears' settings, the kit's voice folder
        (["ears", "sounds", "allow"], True), # the keeper's sounds, the same folder
        (["tool", "mouth", "voice"], "bm_lewis")]  # drawn from the mouth's schema
out = []
last = desk.state()
for path, value in sets:                     # as web.py's tap() takes each one
    old = desk.lookup(last, path)
    result = desk.apply(path, value)
    ev = desk.event(path, old, value, "fresh-install", source="web") if result == "ok" else None
    t0 = time.time()
    last = desk.said(desk.state(), path, result)
    desk.log({"via": "web", "path": path, "value": value, "result": result, "event_rc": ev})
    desk.keep(last, t0)
    out.append({"path": path, "result": result})
print(json.dumps({"sets": out, "voice": desk.lookup(last, ["tool", "mouth", "voice"]),
                  "snapshot": desk.snapshot() is not None}))
"""


def test_8_a_desk_set_through_the_core_applies_and_logs(install, home, tmp_path):
    env = env_for(install, home, tmp_path)
    rc, out, err = run([str(install["python"]), "-I", "-c", DESK_SET], cwd=tmp_path, env=env)
    assert rc == 0, f"the desk core did not run:\n{out}\n{err}"
    got = json.loads(out.strip().splitlines()[-1])
    refused = [s for s in got["sets"] if s["result"] != "ok"]
    assert not refused, f"the desk refused a set: {refused}\n{err}"
    assert got["voice"] == "bm_lewis", got
    state = home / ".sessionkit" / "settled"
    assert (state / "log" / "apply.jsonl").is_file(), sorted(p.name for p in state.iterdir())
    assert (state / "state.json").is_file(), sorted(p.name for p in state.iterdir())


# --- 9. one home, proven ------------------------------------------------------

def test_9_install_and_run_leave_nothing_in_home_but_sessionkit(install, home):
    """The one-home gate: a fresh install and a run make only ~/.sessionkit.
    Runs last: by now the install, enrol, serve, the registry write and the desk
    set have all run with HOME set to this folder. Every top-level entry counts,
    not just dot folders. A failure names each stray and what is under it."""
    stray = sorted(p for p in home.iterdir() if p.name != ".sessionkit")
    shown = {str(p.relative_to(home)): sorted(str(q.relative_to(home)) for q in p.rglob("*"))[:20]
             if p.is_dir() else "(file)" for p in stray}
    assert not stray, f"the install or a run wrote outside ~/.sessionkit: {shown}"
