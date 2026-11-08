"""Structural test isolation.

desk.py binds HOME, VOICES_DIR and PREFS at IMPORT time. Whichever test
module imports it first fixes them for the whole run, and tests/test_schemas.py
imports it with no env, so a bare `pytest tests` bound the REAL paths and the
voice-browser tests then wrote the live voice-prefs.json and repointed live
voice-lab links. pytest imports this file before
it imports any test module, so HOME and the voice env vars are a temp dir before
desk (or anything that calls Path.home()) is first imported. Unconditional,
not setdefault: a run with no env set, or with a real HOME set, still cannot
reach the real paths.
"""
import os, tempfile
from pathlib import Path

REAL_HOME = os.environ.get("HOME")
SANDBOX = Path(tempfile.mkdtemp(prefix="settings-test-home-")).resolve()
os.environ["HOME"] = str(SANDBOX)
os.environ["SETTINGS_VOICES_DIR"] = str(SANDBOX / "voices")
os.environ["SETTINGS_VOICE_PREFS"] = str(SANDBOX / "voice-prefs.json")
os.environ["SETTINGS_ROOT"] = str(SANDBOX / "settings-root")


# --- guards (2)-(4), and no test leaves a fake behind -----------------------
# `make test` sets all four guards; this file sets them again, so a bare
# `pytest tests` gets them too. (2) a stub tmux first on PATH, which exits 97
# loudly if anything calls it; (3) a temp TMUX_TMPDIR; (4) TMUX and TMUX_PANE
# unset. Unsetting TMUX alone is unsafe: tmux falls back to the default socket.
# tests/test_guards.py proves all four.
import ast, functools, subprocess, sys  # noqa: E402

import pytest  # noqa: E402

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
# The checkout's settled package, for every test module: `from settled import
# desk` must find this tree (not an installed copy) whichever file runs alone.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
STUBS = TESTS / "stubs"
if os.environ.get("PATH", "").split(os.pathsep)[0] != str(STUBS):
    os.environ["PATH"] = str(STUBS) + os.pathsep + os.environ.get("PATH", "")
os.environ["TMUX_TMPDIR"] = str(SANDBOX / "tmux")
(SANDBOX / "tmux").mkdir(exist_ok=True)
for _k in ("TMUX", "TMUX_PANE"):
    os.environ.pop(_k, None)

# The real subprocess entry points, taken before pytest imports any test module.
# A module that replaces one at import (tests/test_voice_browser.py did:
# `desk.subprocess.run = fake_run`) hands its fake to every test after it.
SUBPROCESS = ("run", "Popen", "call", "check_call", "check_output", "getoutput", "getstatusoutput")
ORIGINALS = {name: getattr(subprocess, name) for name in SUBPROCESS}


@functools.lru_cache(maxsize=None)
def _top_level_defs(path: Path) -> tuple:
    tree = ast.parse(path.read_text())
    return tuple(n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)))


def project_modules() -> dict:
    """The project's own modules (desk, web, ...) that are loaded now."""
    out = {}
    for name, mod in list(sys.modules.items()):
        f = getattr(mod, "__file__", None)
        if f and _in_root(f):
            out[name] = mod
    return out


@functools.lru_cache(maxsize=None)
def _under_root(f: str) -> bool:
    return os.path.realpath(f).startswith(str(ROOT) + os.sep)


@functools.lru_cache(maxsize=None)
def _in_root(f: str) -> bool:
    """A module of ours: in the settled package, or flat at the root
    (the meta tests' throwaway tree keeps its one module there)."""
    return f.endswith(".py") and os.path.dirname(os.path.realpath(f)) in (str(ROOT), str(ROOT / "settled"))


def leaks() -> list:
    """Every fake left in place now: a subprocess entry point that is not the
    real one, a module whose `subprocess` is not the real module, or a project
    function (a top-level def in desk.py, web.py, ...) that is no longer its own."""
    bad = [f"subprocess.{n}" for n, f in ORIGINALS.items() if getattr(subprocess, n) is not f]
    for name, mod in list(sys.modules.items()):
        f = getattr(mod, "__file__", None)
        if f and _under_root(f) and getattr(mod, "subprocess", subprocess) is not subprocess:
            bad.append(f"{name}.subprocess")
    for name, mod in project_modules().items():
        bad += [f"{name}.{fn}" for fn in _top_level_defs(Path(mod.__file__)) if not _own(mod, fn)]
    return sorted(set(bad))


def _own(mod, fn: str) -> bool:
    """`mod.fn` is still the def in mod's own file (a reload makes a new one: still its own)."""
    v = getattr(mod, fn, None)
    code = getattr(v, "__code__", None)
    return (code is not None and getattr(v, "__name__", None) == fn
            and _real(code.co_filename) == _real(mod.__file__))


_real = functools.lru_cache(maxsize=None)(os.path.realpath)


def _snapshot() -> dict:
    snap = {f"subprocess.{n}": getattr(subprocess, n) for n in SUBPROCESS}
    for name, mod in project_modules().items():
        snap[f"{name}.subprocess"] = getattr(mod, "subprocess", None)
        for fn in _top_level_defs(Path(mod.__file__)):
            snap[f"{name}.{fn}"] = "own" if _own(mod, fn) else getattr(mod, fn, None)
    return snap


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: builds a real env with uv; `-m 'not slow'` skips it")


@pytest.fixture
def subprocess_originals() -> dict:
    return ORIGINALS


@pytest.fixture(autouse=True)
def _no_fake_left_behind():
    """A test may fake subprocess or a project function while it runs (use
    monkeypatch), never after: this fails the test that leaves one in place."""
    before = _snapshot()
    yield
    after = _snapshot()
    left = sorted(k for k in before if k in after and after[k] is not before[k])
    assert not left, f"this test left a fake in place after it finished: {left}"
