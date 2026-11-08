"""desk.py, the set/read core the desk calls, split from the private chat
follower.

What ships: settled/desk.py, which settled/web.py imports. What does not:
private/apply.py (the follower) and tests/test_follower.py, kept in the repo
but out of the sdist (MANIFEST.in) and out of the public tree (.gitattributes
export-ignore). The follower sits outside the settled package, so no
wheel carries it however it is built: from the sdist, or straight from this
private checkout. Every module is inside the settled package, none at the
wheel's top level, so nothing collides in the kit's shared venv. The core's
own writes go under ~/.sessionkit only (one home).
"""
import json
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

from settled import desk
from settled import web

REPO = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "sessionkit"
STUBS = Path(__file__).resolve().parent / "stubs"
PRIVATE = ("private/apply.py", "tests/test_follower.py")


def child(code: str, home: Path, **extra) -> subprocess.CompletedProcess:
    """`python -c CODE` from the repo root: HOME at `home`, every settled and
    voice switch unset, and a PATH of the stubs and the system's bin dirs only,
    so nothing of this Mac's (its audio tools, its mail) runs."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("SETTINGS_", "SETTLED_", "VOICEMODE_"))}
    env.update({"HOME": str(home), "PATH": os.pathsep.join([str(STUBS), "/usr/bin", "/bin"]), **extra})
    return subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env,
                          capture_output=True, text=True, timeout=60)


# --- the split ------------------------------------------------------------------

def test_web_reads_and_sets_through_desk():
    assert web.desk is desk
    assert not hasattr(web, "apply"), "web.py still imports the follower"


def test_desk_imports_without_the_follower(tmp_path):
    r = child("import sys; from settled import desk, web; "
              "print(sorted(m for m in sys.modules if m.endswith('apply') or m.startswith('private')))",
              tmp_path)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "[]"


def test_desk_carries_the_set_read_core_and_no_follower():
    for name in ("apply", "state", "lookup", "said", "same", "host", "log", "snapshot", "keep",
                 "event", "ACTIONS", "POLL"):
        assert hasattr(desk, name), name
    for name in ("main", "push", "summary", "XDC_IN", "UPDATE", "MSGS", "EARS_DIR"):
        assert not hasattr(desk, name), f"desk.{name}: the follower's, not the core's"


def test_event_with_no_writer_named_is_127_said_once(monkeypatch, capsys):
    monkeypatch.setattr(desk, "HEARD", None)
    desk._SAID_ABSENT.clear()
    assert desk.event(["mouth", "output"], "a", "b", "test") == 127
    assert desk.event(["mouth", "output"], "a", "b", "test", source="web") == 127
    assert capsys.readouterr().err.count("no ears event writer") == 1


def test_event_runs_the_named_writer_with_its_source(monkeypatch, tmp_path):
    out = tmp_path / "argv.json"
    writer = tmp_path / "heard.py"
    writer.write_text(f"import json, sys; open({str(out)!r}, 'w').write(json.dumps(sys.argv[1:]))\n")
    monkeypatch.setattr(desk, "HEARD", writer)
    assert desk.event(["mouth", "output"], "a", "b", "me", source="web") == 0
    argv = json.loads(out.read_text())
    assert argv[:2] == ["write", "event"] and argv[argv.index("--source") + 1] == "web"


# --- one home: the core writes only under ~/.sessionkit -------------------------

def test_desk_own_paths_default_under_settled_home(tmp_path):
    r = child("import json; from settled import desk, paths; print(json.dumps({'home': str(paths.settled_home()), "
              "'paths': [str(p) for p in (desk.LOG, desk.PREFS, desk.SNAPSHOT, desk.VOICES_DIR)], "
              "'heard': desk.HEARD and str(desk.HEARD)}))", tmp_path)
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout)
    assert got["home"] == str(tmp_path / ".sessionkit" / "settled")
    for p in got["paths"]:
        assert Path(p).is_relative_to(got["home"]), p
    assert got["heard"] is None, "no ears event writer is assumed at a private path"


def test_the_voices_dir_follows_the_mouths_own_setting(tmp_path):
    r = child("from settled import desk; print(desk.VOICES_DIR)", tmp_path, VOICEMODE_VOICES_DIR=str(tmp_path / "v"))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == str(tmp_path / "v")


RUN = """
import json, time
from settled import desk
s = desk.state()
desk.log({"probe": True})
desk.keep(s, time.time())
print(json.dumps([
    desk.apply(["voices", "phrases", "add"], "Hello from a fresh home."),
    desk.apply(["tool", "notifications/global", "allow"], False),
    desk.apply(["tool", "desk", "card_max_width"], "900px"),
    desk.snapshot(0.0) is not None,
]))
"""


def test_a_desk_run_writes_nothing_outside_sessionkit(tmp_path):
    """Kit mode (the kit's registry in ~/.sessionkit, sessiontools from the test
    requirements): read the state, log, keep the snapshot, set a voice pref and
    two of settled's own tools. HOME then holds ~/.sessionkit and nothing else."""
    home = tmp_path / "home"
    shutil.copytree(FIXTURES, home / ".sessionkit")
    before = {p for p in home.rglob("*")}
    r = child(RUN, home)
    assert r.returncode == 0, r.stderr
    results = json.loads(r.stdout.strip().splitlines()[-1])
    assert results[0] == "ok", results
    assert sorted(p.name for p in home.iterdir()) == [".sessionkit"]
    made = sorted(str(p.relative_to(home)) for p in set(home.rglob("*")) - before)
    assert made, "the run wrote nothing at all"
    for p in made:
        assert p.startswith(".sessionkit/"), p
    assert (home / ".sessionkit" / "settled" / "log" / "apply.jsonl").is_file()
    assert (home / ".sessionkit" / "settled" / "state.json").is_file()



def test_a_set_on_a_fresh_home_makes_its_folder(tmp_path):
    """global.allow on a fresh install: notifications/ under settled's folder does
    not exist yet, and the set still lands (the fresh-install gate's step 8 caught
    save() writing into a folder nobody had made)."""
    home = tmp_path / "home"
    shutil.copytree(FIXTURES, home / ".sessionkit")
    r = child("from settled import desk; print(desk.apply(['global', 'allow'], False)); print(desk.GLOBAL)", home)
    assert r.returncode == 0, r.stderr
    result, where = r.stdout.strip().splitlines()[-2:]
    assert result == "ok", r.stdout
    assert Path(where).is_relative_to(home / ".sessionkit"), where
    assert json.loads(Path(where).read_text()) == {"allow": False}

# --- out of the wheel, the sdist and the public tree ----------------------------

def test_the_private_files_are_export_ignored():
    if not (REPO / ".git").exists():
        pytest.skip("not a git checkout (an export has no .git)")
    r = subprocess.run(["git", "check-attr", "export-ignore", "--", *PRIVATE, "settled/desk.py"],
                       cwd=REPO, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    got = dict(line.split(": export-ignore: ") for line in r.stdout.splitlines())
    assert got == {"private/apply.py": "set", "tests/test_follower.py": "set", "settled/desk.py": "unspecified"}


# --- fail-closed: nothing private inside a package -------------------------------
# setuptools cannot leave one module out of a listed package: a direct
# `uv build --wheel` or `pip install .` from the private checkout shipped
# settled/apply.py while it sat there. So nothing export-ignored may sit inside
# a package directory, and the follower's directory is no package.

def test_nothing_export_ignored_sits_inside_a_package():
    if not (REPO / ".git").exists():
        pytest.skip("not a git checkout (an export has no .git)")
    tracked = subprocess.run(["git", "ls-files", "--", "settled", "app"], cwd=REPO,
                             capture_output=True, text=True, check=True).stdout.split()
    assert "settled/desk.py" in tracked
    r = subprocess.run(["git", "check-attr", "export-ignore", "--", *tracked],
                       cwd=REPO, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    ignored = [ln.split(": export-ignore: ")[0] for ln in r.stdout.splitlines()
               if not ln.endswith(": unspecified")]
    assert ignored == [], ignored
    assert not (REPO / "settled" / "apply.py").exists()


def test_the_followers_directory_is_no_package():
    import tomllib
    cfg = tomllib.loads((REPO / "pyproject.toml").read_text())["tool"]["setuptools"]
    assert not any(p.split(".")[0] == "private" for p in cfg["packages"]), cfg["packages"]
    assert "private" not in cfg.get("package-dir", {}).values()
    assert not (REPO / "private" / "__init__.py").exists()
    if not (REPO / ".git").exists():
        pytest.skip("not a git checkout (an export has no .git)")
    assert (REPO / "private" / "apply.py").is_file()


@pytest.fixture(scope="module")
def direct_wheel(tmp_path_factory) -> Path:
    """`uv build --wheel` straight from the private checkout, not via the sdist:
    the tracked tree, private/ and all, copied (so no stale build/ of this
    checkout's can leak into it, and none is left behind)."""
    if not (REPO / ".git").exists():
        pytest.skip("not a git checkout (an export has no .git)")
    uv = shutil.which("uv")
    if not uv:
        pytest.fail("needs `uv` on PATH to build the wheel")
    src = tmp_path_factory.mktemp("checkout")
    tracked = subprocess.run(["git", "ls-files", "-z"], cwd=REPO, capture_output=True, check=True).stdout
    for rel in filter(None, tracked.decode().split("\0")):
        if (REPO / rel).is_file():
            (src / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO / rel, src / rel)
    assert (src / "private" / "apply.py").is_file(), "the copy is not the private checkout"
    out = tmp_path_factory.mktemp("direct")
    r = subprocess.run([uv, "build", "--wheel", "--out-dir", str(out), str(src)],
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    return next(out.glob("settled-*.whl"))


def test_a_direct_wheel_from_the_private_checkout_has_no_follower(direct_wheel):
    names = zipfile.ZipFile(direct_wheel).namelist()
    assert "settled/desk.py" in names and "settled/web.py" in names
    followers = sorted(n for n in names if n.rsplit("/", 1)[-1] in ("apply.py", "test_follower.py")
                       or n.startswith("private/"))
    assert followers == [], followers


@pytest.fixture(scope="module")
def dist(tmp_path_factory) -> Path:
    uv = shutil.which("uv")
    if not uv:
        pytest.fail("needs `uv` on PATH to build the wheel and sdist")
    out = tmp_path_factory.mktemp("dist")
    r = subprocess.run([uv, "build", "--out-dir", str(out), str(REPO)], capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    return out


def test_the_wheel_ships_desk_and_not_the_follower(dist):
    names = zipfile.ZipFile(next(dist.glob("settled-*.whl"))).namelist()
    assert "settled/desk.py" in names and "settled/web.py" in names
    for p in PRIVATE:
        assert p not in names, p


def test_the_sdist_ships_desk_and_not_the_follower(dist):
    with tarfile.open(next(dist.glob("settled-*.tar.gz"))) as t:
        names = {m.name.split("/", 1)[1] for m in t.getmembers() if "/" in m.name}
    assert "settled/desk.py" in names
    for p in PRIVATE:
        assert p not in names, p
    assert not any(n.startswith("private/") or n.endswith("/apply.py") for n in names), sorted(names)


# --- one package, no top-level modules ------------------------------------------
# `web` is PyPI's web.py framework's top-level module, and paths/sessions are
# generic names; the kit installs settled into a shared venv, so nothing of
# settled's may sit at the top level beside them.

def test_the_wheel_holds_only_the_settled_package(dist):
    names = zipfile.ZipFile(next(dist.glob("settled-*.whl"))).namelist()
    tops = {n.split("/", 1)[0] for n in names}
    assert tops == {"settled", "settled-0.1.0.dist-info"}, sorted(tops)
    top_level = zipfile.ZipFile(next(dist.glob("settled-*.whl"))).read("settled-0.1.0.dist-info/top_level.txt")
    assert top_level.decode().split() == ["settled"]
    for m in ("web", "desk", "schemas", "webauth", "sessions", "vm_check", "paths"):
        assert f"settled/{m}.py" in names, m


def test_no_module_sits_at_the_repo_top_level():
    """The checkout matches the wheel: a bare `import web` from the repo root
    finds nothing of ours, so no test or module can lean on the flat layout."""
    assert sorted(p.name for p in REPO.glob("*.py")) == []


def test_the_public_tree_has_no_follower():
    """git archive HEAD honours export-ignore: the follower is not in it."""
    if not (REPO / ".git").exists():
        pytest.skip("not a git checkout (an export has no .git)")
    r = subprocess.run(["git", "archive", "--format=tar", "HEAD"], cwd=REPO, capture_output=True, timeout=60)
    assert r.returncode == 0, r.stderr
    import io
    with tarfile.open(fileobj=io.BytesIO(r.stdout)) as t:
        names = set(t.getnames())
    assert "pyproject.toml" in names and "tests/test_desk_split.py" in names
    followers = sorted(n for n in names if n.rsplit("/", 1)[-1] in ("apply.py", "test_follower.py")
                       or n.startswith("private/"))
    assert followers == [], followers   # HEAD as committed: the follower under any path
