"""Settings runs on its own env, never a borrowed python.

The desk once ran a voicemode task worktree's .venv (gone when that worktree
was cleaned up), and the applier ran /opt/homebrew/bin/python3 (no tomlkit, and
it refuses a pip install). Now `make venv` builds one env from requirements.txt,
with uv, at a fixed path outside every git checkout, and the desk's launch
agent runs its python. The agent ships as a TEMPLATE, launchd/<label>.plist,
naming no home of anyone's: `make agent` fills each @NAME@ and installs it.
These tests hold the template, its rendering, the Makefile default and the env
to that.

The env test is the SLOW one (marked `slow`): it builds a real env with uv into
the suite's scratch HOME, twice, so it needs uv and an index. Without either it
skips, saying which. Nothing here touches ~/Library/LaunchAgents or launchctl.
Run: make test (or pytest tests).
"""
import os, plistlib, re, shutil, subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LAUNCHD = ROOT / "launchd"
DESK = "dev.sessionkit.settled.desk"          # the label `settled install` writes too
PLISTS = (f"{DESK}.plist",)                    # what ships in launchd/
DEPLOY_HOME = "/Users/deploy"                  # a made-up HOME for the dry runs (make -n)
ENV_REL = ".local/share/settings/venv"
VENV_PYTHON = "@VENV@/bin/python"              # the template's python, filled by `make agent`
MAKEFILE_VENV = f"$(HOME)/{ENV_REL}"
WORKTREE = re.compile(r"projects/[^/\s]+/tasks/[^/\s]+/worktree")
BORROWED = "/opt/homebrew/bin/python3"
SLOT = re.compile(r"@([A-Z_]+)@")


def strings(v):
    """Every string in a parsed plist, keys included."""
    if isinstance(v, str):
        yield v
    elif isinstance(v, dict):
        for k, x in v.items():
            yield k
            yield from strings(x)
    elif isinstance(v, list):
        for x in v:
            yield from strings(x)


def load(name: str) -> dict:
    with open(LAUNCHD / name, "rb") as f:
        return plistlib.load(f)


def test_the_launchd_dir_holds_exactly_the_desk():
    assert sorted(p.name for p in LAUNCHD.glob("*.plist")) == sorted(PLISTS)


@pytest.mark.parametrize("name", PLISTS)
def test_each_plist_runs_the_settings_env_python(name):
    pl = load(name)
    assert pl["Label"] == name.removesuffix(".plist")
    assert pl["ProgramArguments"][0] == VENV_PYTHON, pl["ProgramArguments"]
    assert pl["EnvironmentVariables"]["HOME"] == "@HOME@"   # the HOME `make venv` builds under


@pytest.mark.parametrize("name", PLISTS)
def test_the_template_names_no_home(name):
    """Every path in the shipped template is a slot or a system dir: no user's
    home folder, no checkout, no host. `make agent` fills them for whoever runs it."""
    for s in strings(load(name)):
        assert not re.search(r"/Users/|/Code/", s) and not s.startswith("/home"), s


@pytest.mark.parametrize("name", PLISTS)
def test_no_plist_borrows_a_python(name):
    """Neither a parsed string nor the raw file (comments too) names the
    Homebrew python or any task worktree."""
    raw = (LAUNCHD / name).read_text()
    for s in [*strings(load(name)), raw]:
        assert BORROWED not in s, (name, s[:200])
        assert not WORKTREE.search(s), (name, WORKTREE.search(s).group(0))


def test_the_env_path_is_outside_any_checkout():
    p = Path(MAKEFILE_VENV.replace("$(HOME)", DEPLOY_HOME))
    assert ROOT not in p.parents and "worktree" not in p.parts and ".git" not in p.parts
    assert "Code" not in p.parts               # where checkouts usually live


def makefile_venv_default() -> str:
    found = re.findall(r"^VENV\s*\?=\s*(\S.*?)\s*$", (ROOT / "Makefile").read_text(), re.M)
    assert len(found) == 1, found
    return found[0]


def test_the_makefile_default_is_the_plists_env():
    """The Makefile's VENV default is what `make agent` puts in the template's
    @VENV@, so `make venv` builds what launchd runs and the two cannot drift."""
    assert makefile_venv_default() == MAKEFILE_VENV
    recipe = make_n("agent", f"HOME={DEPLOY_HOME}")
    assert f"s|@VENV@|{DEPLOY_HOME}/{ENV_REL}|g" in recipe, recipe


def make_n(*args: str) -> str:
    if shutil.which("make") is None:
        pytest.skip("no make on this host")
    r = subprocess.run(["make", "-n", *args], cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_make_venv_uses_uv_and_the_requirements_file():
    recipe = make_n("venv", f"HOME={DEPLOY_HOME}")    # -n: printed, never run
    env = f"{DEPLOY_HOME}/{ENV_REL}"
    assert f"uv venv --no-project --managed-python --python '>=3.11' \"{env}\"" in recipe, recipe
    assert f'uv pip install --python "{env}/bin/python" -r requirements.txt' in recipe, recipe
    assert "pip install " not in recipe.replace("uv pip install ", ""), recipe    # uv only


def test_every_uv_venv_line_pins_a_uv_managed_python():
    """Both `uv venv` lines (the fresh build and the --clear rebuild of a
    broken env) carry --managed-python, so the env never sits on Homebrew's
    python, which a `brew upgrade` can replace under it."""
    recipe = make_n("venv", f"HOME={DEPLOY_HOME}")
    lines = [l for l in recipe.splitlines() if re.search(r"\buv venv\b", l)]
    assert len(lines) == 2, lines                    # fresh build + --clear rebuild
    assert any("--clear" in l for l in lines), lines
    for l in lines:
        assert re.search(r"\buv venv\b.*\s--managed-python\s", l), l
        assert "--no-managed-python" not in l, l


def test_make_agent_renders_and_installs_the_desk():
    recipe = make_n("agent", f"HOME={DEPLOY_HOME}")
    assert f"launchd/{DESK}.plist" in recipe, recipe
    assert "$p > ~/Library/LaunchAgents/$a.plist" in recipe, recipe
    assert "launchctl bootout gui/$(id -u)/$a" in recipe, recipe
    assert "launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/$a.plist" in recipe, recipe
    assert f"{DEPLOY_HOME}/.sessionkit/settled/log" in recipe, recipe   # launchd makes no log dir
    status = make_n("status", f"HOME={DEPLOY_HOME}")
    assert DESK in status, status


@pytest.mark.parametrize("name", PLISTS)
def test_make_agent_fills_every_slot_and_the_result_is_a_plist(name):
    """Every @NAME@ in the template has a sed in `make agent`; filled the same
    way, the template parses, names no slot, and runs the env's python."""
    raw = (LAUNCHD / name).read_text()
    recipe = make_n("agent", f"HOME={DEPLOY_HOME}")
    fills = dict(re.findall(r"s\|@([A-Z_]+)@\|([^|]*)\|g", recipe))
    assert set(SLOT.findall(raw)) <= set(fills), (set(SLOT.findall(raw)) - set(fills), recipe)
    out = SLOT.sub(lambda m: fills[m.group(1)], raw)
    pl = plistlib.loads(out.encode())
    assert not SLOT.search(out)
    assert pl["ProgramArguments"][0] == f"{DEPLOY_HOME}/{ENV_REL}/bin/python"
    assert pl["EnvironmentVariables"]["HOME"] == DEPLOY_HOME
    assert pl["StandardOutPath"].startswith(f"{DEPLOY_HOME}/.sessionkit/")   # one home


def test_make_agent_refuses_a_task_worktree():
    """The template's WorkingDirectory is the checkout `make agent` runs in; a
    worktree's cleanup would take the agent's code, so it refuses one."""
    recipe = make_n("agent", f"HOME={DEPLOY_HOME}")
    assert "*/worktree|*/worktree/*" in recipe and "exit 1" in recipe, recipe


# --- the slow one: build the env for real, into the scratch HOME --------------

OFFLINE = re.compile(r"(?i)failed to fetch|dns|could not connect|connection|network|offline|"
                     r"timed out|no solution found|failed to download")


def build(venv: Path, home: Path) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("UV_", "XDG_", "VIRTUAL_ENV"))}
    env.update(HOME=str(home), UV_CACHE_DIR=str(home / ".cache" / "uv"),
               UV_PYTHON_INSTALL_DIR=str(home / ".local" / "share" / "uv" / "python"))
    return subprocess.run(["make", "venv", f"VENV={venv}"], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=600)


@pytest.mark.slow
def test_make_venv_builds_an_env_that_imports_the_runtime_deps():
    if shutil.which("uv") is None:
        pytest.skip("uv is not on PATH: cannot build the settings env here")
    if shutil.which("make") is None:
        pytest.skip("no make on this host")
    home = Path(os.environ["HOME"]).resolve()
    assert str(home) != DEPLOY_HOME and not str(home).startswith(DEPLOY_HOME + "/.local")
    venv = home / ENV_REL                         # the same layout, under the scratch HOME
    r = build(venv, home)
    if r.returncode != 0 and OFFLINE.search(r.stdout + r.stderr):
        pytest.skip(f"uv could not reach an index (offline?): {(r.stdout + r.stderr)[-300:]}")
    assert r.returncode == 0, r.stdout + r.stderr
    py = venv / "bin" / "python"
    ok = subprocess.run([str(py), "-c", "import cryptography, websockets, tomlkit, sys; "
                         "assert sys.version_info >= (3, 11); print(sys.prefix)"],
                        capture_output=True, text=True, timeout=60)
    assert ok.returncode == 0, ok.stderr
    assert Path(ok.stdout.strip()).resolve() == venv.resolve()
    # --managed-python: the base interpreter is one uv installed (here, under the
    # scratch HOME's UV_PYTHON_INSTALL_DIR), never Homebrew's.
    base = Path(os.path.realpath(venv / "bin" / "python"))
    assert (home / ".local" / "share" / "uv" / "python").resolve() in base.parents, base

    # Idempotent: a second run succeeds, keeps the env, and changes nothing material.
    before = snapshot(venv)
    again = build(venv, home)
    assert again.returncode == 0, again.stdout + again.stderr
    assert "(exists, kept)" in again.stdout, again.stdout
    assert snapshot(venv) == before


def snapshot(venv: Path) -> dict:
    """What would matter if it changed: the interpreter, the config, and every
    installed distribution (name and version)."""
    sp = sorted(p.name for p in (venv / "lib").glob("python3.*/site-packages/*.dist-info"))
    return {"python": os.path.realpath(venv / "bin" / "python"),
            "cfg": (venv / "pyvenv.cfg").read_text(), "dists": sp}


def test_make_venv_refuses_a_dir_that_is_not_a_venv(tmp_path):
    """--clear is only for a broken venv: a plain directory at VENV is kept,
    and the build fails rather than wiping it."""
    if shutil.which("uv") is None:
        pytest.skip("uv is not on PATH")
    d = tmp_path / "not-a-venv"
    d.mkdir()
    (d / "keep").write_text("mine")
    r = build(d, Path(os.environ["HOME"]).resolve())
    assert r.returncode != 0, r.stdout
    assert (d / "keep").read_text() == "mine"
