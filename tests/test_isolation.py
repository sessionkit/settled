"""The suite can never reach the real HOME.

tests/conftest.py puts HOME and the voice env vars in a temp sandbox before
any test module imports desk. These tests fail if that stops being true, e.g.
conftest.py deleted, or a module imported desk before it ran, so desk bound
the real voice-prefs.json and voices folder.
Run: pytest tests (or this file alone).
"""
import os, pwd, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from settled import desk  # noqa: E402
from settled import schemas  # noqa: E402


def real_home() -> Path:
    return Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()


def under(p, root) -> bool:
    p = Path(p).expanduser().resolve()
    return p == root or root in p.parents


def sandbox_paths() -> dict:
    return {k: v for k, v in vars(desk).items() if isinstance(v, Path)}


def test_home_is_the_conftest_sandbox_not_the_real_home():
    assert Path.home().name.startswith("settings-test-home-"), Path.home()
    assert desk.HOME == Path.home()
    assert not under(desk.HOME, real_home())


def test_voice_data_paths_sit_in_the_sandbox():
    sandbox = Path.home().resolve()
    assert under(desk.VOICES_DIR, sandbox) or "settings-voices-" in str(desk.VOICES_DIR)
    assert under(desk.PREFS, sandbox) or "settings-voices-" in str(desk.PREFS)
    for p in (desk.VOICES_DIR, desk.PREFS):
        assert not under(p, real_home()), p


def test_no_path_apply_holds_is_in_the_real_home():
    bad = {k: str(v) for k, v in sandbox_paths().items() if under(v, real_home())}
    assert bad == {}


def test_schemas_root_is_not_the_real_home():
    for name in ("ROOT", "SETTINGS_ROOT"):
        v = getattr(schemas, name, None)
        if isinstance(v, Path):
            assert not under(v, real_home()), (name, v)
