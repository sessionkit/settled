"""The suite's guards hold, and a test cannot leak a fake.

`make test` runs the suite behind four guards, and tests/conftest.py applies
them again so a bare `pytest tests` gets them too: (1) a scratch HOME; (2) a
stub tmux first on PATH that exits non-zero, loudly; (3) a temp TMUX_TMPDIR;
(4) TMUX and TMUX_PANE unset. The suite uses no tmux today, so (2)-(4) are
defence in depth: these tests fail the day one of them stops holding.

The leak guard: tests/test_voice_browser.py once set `desk.subprocess.run`
at import, so every test collected after it ran against a fake that always
returned 0. pytest imports every collected module before the first test runs,
so by the time `test_no_module_left_a_fake_after_import` runs, any module that
fakes at import has done it, whatever the order. The meta tests at the bottom
run pytest on a throwaway tree to prove the guard catches what it claims to.
Run: make test (or pytest tests).
"""
import ast, os, pwd, re, shutil, subprocess, sys, tempfile, textwrap
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
STUB = TESTS / "stubs" / "tmux"


def real_home() -> Path:
    return Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()


def temp_root() -> Path:
    return Path(tempfile.gettempdir()).resolve()


def under(p, root: Path) -> bool:
    p = Path(p).resolve()
    return p == root or root in p.parents


# --- the four guards ----------------------------------------------------------

def test_home_is_a_temp_dir_not_the_real_home():                      # guard 1
    home = Path(os.environ["HOME"]).resolve()
    assert under(home, temp_root()) and home != temp_root(), home
    assert not under(home, real_home()), home
    assert Path.home().resolve() == home


def test_tmux_resolves_to_the_stub():                                  # guard 2
    assert shutil.which("tmux") is not None
    assert Path(shutil.which("tmux")).resolve() == STUB
    assert os.environ["PATH"].split(os.pathsep)[0] == str(STUB.parent)


def test_the_stub_fails_loudly_whatever_it_is_asked():                 # guard 2
    assert Path(shutil.which("tmux")).resolve() == STUB  # never run a real tmux to find out
    for args in (["ls"], ["new-session", "-d"], ["kill-server"], []):
        r = subprocess.run(["tmux", *args], capture_output=True, text=True, timeout=10)
        assert r.returncode == 97, (args, r)
        assert "STUB tmux called" in r.stderr and "never touch tmux" in r.stderr, r.stderr
        assert r.stdout == ""


def test_tmux_tmpdir_is_a_temp_dir():                                  # guard 3
    d = Path(os.environ["TMUX_TMPDIR"]).resolve()
    assert d.is_dir() and under(d, temp_root()) and d != temp_root(), d
    assert not under(d, real_home()), d
    assert d != Path("/tmp").resolve() / f"tmux-{os.getuid()}"


def test_no_tmux_session_is_inherited():                               # guard 4
    assert "TMUX" not in os.environ
    assert "TMUX_PANE" not in os.environ


def make_test_recipe() -> str:
    if shutil.which("make") is None:
        pytest.skip("no make on this host")
    r = subprocess.run(["make", "-n", "test"], cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_make_test_sets_all_four_guards():
    recipe = make_test_recipe()
    assert re.search(r'scratch=\$\(mktemp -d ', recipe), recipe                     # 1
    assert 'HOME="$scratch/home"' in recipe, recipe                                  # 1
    assert f'PATH="{STUB.parent}:$PATH"' in recipe, recipe                           # 2
    assert 'TMUX_TMPDIR="$scratch/tmux"' in recipe, recipe                           # 3
    assert "-u TMUX " in recipe and "-u TMUX_PANE " in recipe, recipe                # 4


# --- the deps are declared, in the one file make test reads -------------------

def declared(name: str = "requirements-test.txt") -> set:
    lines = (ROOT / name).read_text().splitlines()
    names = (re.split(r"[\s<>=!~;\[#]", ln.strip(), maxsplit=1)[0] for ln in lines)
    return {n.lower() for n in names if n}


def imported_third_party() -> dict:
    """Top-level name -> files, for every import that is neither stdlib nor ours."""
    packages = [p.parent for p in ROOT.glob("*/__init__.py")]          # settled/, app/: ours too
    # private/ (the chat follower) is ours but deliberately no
    # package, so no wheel can carry it: its imports are scanned, and its name is ours.
    packages += [d for d in (ROOT / "private",) if d.is_dir()]
    files = [p for p in [*ROOT.glob("*.py"), *TESTS.rglob("*.py"),
                         *(q for d in packages for q in d.rglob("*.py"))] if "__pycache__" not in p.parts]
    ours = {p.stem for p in files} | {d.name for d in packages}
    out = {}
    for p in files:
        for n in ast.walk(ast.parse(p.read_text())):
            mods = ([a.name for a in n.names] if isinstance(n, ast.Import)
                    else [n.module] if isinstance(n, ast.ImportFrom) and n.level == 0 and n.module
                    else [])
            for m in mods:
                top = m.split(".")[0]
                if top not in sys.stdlib_module_names and top not in ours:
                    out.setdefault(top, set()).add(p.relative_to(ROOT).as_posix())
    return out


def test_the_suite_deps_are_declared():
    assert {"pytest", "cryptography", "websockets"} <= declared()
    missing = {m: sorted(f) for m, f in imported_third_party().items() if m.lower() not in declared()}
    assert missing == {}, f"imported but not in requirements-test.txt: {missing}"


def extras() -> set:
    """The names pyproject's optional-dependencies declare: an extra is a runtime
    dependency a user opts into (`settled[kit]`), imported behind `except
    ImportError`, so it is declared there and not in requirements.txt."""
    import tomllib
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    names = (re.split(r"[\s<>=!~;\[#]", spec.strip(), maxsplit=1)[0]
             for specs in data.get("project", {}).get("optional-dependencies", {}).values() for spec in specs)
    return {n.lower() for n in names if n}


def test_the_runtime_deps_are_declared_apart_and_the_suite_carries_them():
    """requirements.txt is the RUNTIME list (deploy has
    one list): every third-party import of the code that
    runs (not tests/) is in it, or in an extra of pyproject.toml (opt-in at
    runtime, behind `except ImportError`), and the suite's file repeats each one."""
    runtime = declared("requirements.txt") | extras()
    assert {"cryptography", "websockets", "tomlkit"} <= runtime
    shipped = {m: sorted(f) for m, f in imported_third_party().items()
               if any(not x.startswith("tests/") for x in f)}
    missing = {m: f for m, f in shipped.items() if m.lower() not in runtime}
    assert missing == {}, f"imported at runtime but not in requirements.txt: {missing}"
    assert runtime <= declared(), f"runtime deps the suite lacks: {runtime - declared()}"


def test_make_test_reads_the_requirements_file_and_nothing_inline():
    recipe = make_test_recipe()
    assert "--with-requirements requirements-test.txt" in recipe, recipe
    assert not re.search(r"--with(?!-requirements)\b", recipe), recipe   # one place to declare a dep


# --- no fake outlives its test ------------------------------------------------

def test_the_originals_are_the_real_subprocess(subprocess_originals):
    for name, f in subprocess_originals.items():
        assert getattr(f, "__module__", None) == "subprocess", (name, f)


def test_no_module_left_a_fake_after_import(subprocess_originals):
    import conftest
    for name, f in subprocess_originals.items():
        assert getattr(subprocess, name) is f, f"subprocess.{name} is replaced: {getattr(subprocess, name)!r}"
    assert conftest.leaks() == []


def test_apply_runs_the_real_subprocess_outside_a_voice_test():
    sys.path.insert(0, str(ROOT))
    from settled import desk
    assert desk.subprocess is subprocess
    assert desk.subprocess.run is subprocess.run


# The meta tests: pytest on a throwaway tree (copies of conftest.py, this file
# and the stub, a tiny project module, and one module that leaks), proving the
# guard fails a leak and passes a clean run. The child runs only the named tests.

LEAKS = {
    "subprocess_run_at_import": """
        import subprocess
        subprocess.run = lambda *a, **k: None
        def test_x(): pass
    """,
    "project_fn_at_import": """
        import sys; from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        import fakeapp
        fakeapp.say = lambda *a: "faked"
        def test_x(): pass
    """,
    "project_subprocess_at_import": """
        import sys, types; from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        import fakeapp
        fakeapp.subprocess = types.SimpleNamespace(run=lambda *a, **k: None)
        def test_x(): pass
    """,
}
CLEAN = """
    import subprocess, sys, types; from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import fakeapp
    def test_scoped(monkeypatch):
        monkeypatch.setattr(fakeapp, "subprocess", types.SimpleNamespace(run=lambda *a, **k: None))
        monkeypatch.setattr(fakeapp, "say", lambda *a: "faked")
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: None)
        assert fakeapp.say() == "faked"
"""
BODY_LEAK = """
    import subprocess
    def test_leaves_it(): subprocess.run = lambda *a, **k: None
"""


def child_pytest(tmp_path: Path, module: str, *nodes: str) -> subprocess.CompletedProcess:
    proj = tmp_path / "proj"
    (proj / "tests" / "stubs").mkdir(parents=True)
    (proj / "pytest.ini").write_text("[pytest]\n")
    (proj / "fakeapp.py").write_text("import subprocess\n\ndef say(*a):\n    return 'real'\n")
    for f in ("conftest.py", "test_guards.py"):
        shutil.copy(TESTS / f, proj / "tests" / f)
    shutil.copy(STUB, proj / "tests" / "stubs" / "tmux")
    (proj / "tests" / "test_other.py").write_text(textwrap.dedent(module))
    args = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:randomly",
            *(f"tests/{n}" for n in nodes)]
    return subprocess.run(args, cwd=proj, capture_output=True, text=True, timeout=120, env=dict(os.environ))


GUARD = "test_guards.py::test_no_module_left_a_fake_after_import"


@pytest.mark.parametrize("leak", sorted(LEAKS))
@pytest.mark.parametrize("order", ["guard_first", "guard_last"])
def test_meta_an_import_time_leak_fails_the_guard(tmp_path, leak, order):
    nodes = [GUARD, "test_other.py"] if order == "guard_first" else ["test_other.py", GUARD]
    r = child_pytest(tmp_path, LEAKS[leak], *nodes)
    assert r.returncode == 1, r.stdout + r.stderr
    assert f"FAILED tests/{GUARD}" in r.stdout, r.stdout


def test_meta_a_scoped_fake_passes_the_guard(tmp_path):
    r = child_pytest(tmp_path, CLEAN, "test_other.py", GUARD)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "2 passed" in r.stdout, r.stdout


def test_meta_a_test_that_leaves_a_fake_errors_at_teardown(tmp_path):
    r = child_pytest(tmp_path, BODY_LEAK, "test_other.py")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "ERROR at teardown of test_leaves_it" in r.stdout, r.stdout
    assert "subprocess.run" in r.stdout and "left a fake in place" in r.stdout, r.stdout
