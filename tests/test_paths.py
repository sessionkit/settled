"""paths.py and the path move: settled's state under one folder, the tools'
files where their readers read them, the old store carried once, and the
desk serving with nothing of the old deploy present.

Kit mode cannot be tested in this process: conftest sets SETTINGS_ROOT (the
standalone switch) before any module loads, and schemas.py reads the switch
at import. So the kit-mode tests run a child python from the repo root with
a HOME of their own and the switch unset; sessiontools comes from
requirements-test.txt (the `settled[kit]` extra at runtime).
"""
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from settled import desk
from settled import paths

REPO = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "sessionkit"


def child(code: str, home: Path, **extra) -> subprocess.CompletedProcess:
    """`python -c CODE` from the repo root, HOME at `home`, every settled switch unset."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("SETTINGS_", "SETTLED_"))}
    env.update({"HOME": str(home), **extra})
    with subprocess.Popen([sys.executable, "-c", code], cwd=REPO, env=env, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True) as p:
        out, err = p.communicate(timeout=60)
    return subprocess.CompletedProcess(p.args, p.returncode, out, err)


# --- settled's own folder ------------------------------------------------------

def test_settled_home_is_under_sessionkit_or_the_env(tmp_path, monkeypatch):
    monkeypatch.delenv("SETTLED_HOME", raising=False)
    assert paths.settled_home() == Path.home() / ".sessionkit" / "settled"
    monkeypatch.setenv("SETTLED_HOME", str(tmp_path / "elsewhere"))
    assert paths.settled_home() == tmp_path / "elsewhere"


def test_carry_copies_once_private_and_leaves_the_old_file(tmp_path, capsys):
    old = tmp_path / "old" / "passkeys.json"
    old.parent.mkdir()
    old.write_text('{"credentials": [1, 2, 3]}\n')
    new = tmp_path / "new" / "deep" / "passkeys.json"
    assert paths.carry(new, old) is True
    assert new.read_text() == old.read_text()
    assert old.is_file(), "the old file is left in place"
    assert stat.S_IMODE(new.stat().st_mode) == 0o600
    assert stat.S_IMODE(new.parent.stat().st_mode) == 0o700
    assert "carried" in capsys.readouterr().err
    new.write_text("changed\n")
    assert paths.carry(new, old) is False, "never twice"
    assert new.read_text() == "changed\n"
    assert paths.carry(tmp_path / "x.json", tmp_path / "absent.json") is False


# --- the kit's registry, read the kit's own way (a child process) ---------------

KIT_PROBE = r"""
import json, os
from settled import schemas, paths
home = os.environ["HOME"]
print(json.dumps({
    "kit": schemas.KIT is not None,
    "registry": schemas.registry(),
    "mouth_schema": str(schemas.schema_path("mouth")),
    "mouth_file": str(schemas.file_path("mouth")),
    "mail_schema": str(schemas.schema_path("mail")),
    "sounds_file": str(schemas.file_path("ears-sounds")),
    "desk_file": str(schemas.file_path("desk")),
    "write": schemas.write("mouth", ["voice"], "bm_daniel"),
    "home": home,
}))
"""


def test_kit_mode_draws_the_kit_folders_and_writes_the_host_file(tmp_path):
    pytest.importorskip("sessiontools", reason="the kit's registry needs sessiontools (requirements-test.txt)")
    home = tmp_path / "home"
    shutil.copytree(FIXTURES, home / ".sessionkit")
    r = child(KIT_PROBE, home)
    assert r.returncode == 0, r.stderr
    got = json.loads(r.stdout.strip().splitlines()[-1])
    voice = home / ".sessionkit" / "voice" / "settings"
    assert got["kit"] is True
    assert {"ears", "mouth", "mail"} <= set(got["registry"]), got["registry"]
    assert "desk" not in got["registry"], "no schema under settled's folder, no form"
    assert got["mouth_schema"] == str(voice / ".mouth.schema.json")
    assert got["mouth_file"] == str(voice / "mouth.json")
    assert got["mail_schema"] == str(home / ".sessionkit" / "mail" / "settings" / "mail.schema.json")
    assert got["sounds_file"] == str(voice / "ears-sounds.json"), "the keeper's sounds live in the flat voice folder"
    assert got["desk_file"] == str(home / ".sessionkit" / "settled" / "desk.json")
    assert got["write"] == "ok"
    assert json.loads((voice / "mouth.json").read_text())["voice"] == "bm_daniel"
    assert [p.name for p in home.iterdir() if p.name.startswith(".")] == [".sessionkit"]   # one home


def test_settings_root_keeps_the_standalone_layout_even_with_the_kit(tmp_path):
    pytest.importorskip("sessiontools")
    root = tmp_path / "root"
    (root / "mouth").mkdir(parents=True)
    shutil.copy(FIXTURES / "voice" / "settings" / ".mouth.schema.json", root / "mouth" / ".mouth.schema.json")
    r = child("import json; from settled import schemas; print(json.dumps([schemas.KIT is None, schemas.registry(), str(schemas.file_path('mouth'))]))",
              tmp_path / "home", SETTINGS_ROOT=str(root))
    assert r.returncode == 0, r.stderr
    standalone, reg, mouth = json.loads(r.stdout.strip().splitlines()[-1])
    assert standalone is True
    assert reg == ["mouth"]
    assert mouth == str(root / "mouth" / "mouth.json")


# --- the old passkey store, carried once --------------------------------------

CARRY_PROBE = "import json; from settled import webauth; print(json.dumps(len(webauth.load())))"


def old_store(home: Path, n: int = 3) -> Path:
    """A pre-package passkey store, in a folder of the test's own naming: the
    shipped code names no old folder, the host says it in $SETTLED_CARRY_FROM."""
    old = home / "old-state" / "passkeys.json"
    old.parent.mkdir(parents=True)
    old.write_text(json.dumps({"credentials": [{"id": str(i)} for i in range(n)]}) + "\n")
    return old


def test_webauth_carries_the_pre_package_store_once(tmp_path):
    home = tmp_path / "home"
    old = old_store(home)
    r = child(CARRY_PROBE, home, SETTLED_CARRY_FROM=str(old.parent))
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout.strip()) == 3
    new = home / ".sessionkit" / "settled" / "passkeys.json"
    assert new.is_file() and stat.S_IMODE(new.stat().st_mode) == 0o600
    assert "carried" in r.stderr
    assert old.is_file()
    r2 = child(CARRY_PROBE, home, SETTLED_CARRY_FROM=str(old.parent))
    assert json.loads(r2.stdout.strip()) == 3 and "carried" not in r2.stderr
    r3 = child(CARRY_PROBE, home)                      # later runs need no variable
    assert json.loads(r3.stdout.strip()) == 3


def test_without_the_variable_nothing_is_carried(tmp_path):
    home = tmp_path / "home"
    old_store(home)
    r = child(CARRY_PROBE, home)
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout.strip()) == 0 and "carried" not in r.stderr
    assert not (home / ".sessionkit" / "settled" / "passkeys.json").exists()


def test_webauth_store_env_wins_and_carries_nothing(tmp_path):
    home = tmp_path / "home"
    old = old_store(home, 1)
    r = child(CARRY_PROBE, home, SETTINGS_PASSKEYS=str(tmp_path / "mine.json"),
              SETTLED_CARRY_FROM=str(old.parent))
    assert r.returncode == 0, r.stderr
    assert json.loads(r.stdout.strip()) == 0
    assert not (home / ".sessionkit").exists()


def test_the_standalone_root_is_under_settled_home(tmp_path):
    """No kit and no SETTINGS_ROOT: the tools' settings sit in settled's own
    folder, so a standalone desk writes nothing outside ~/.sessionkit."""
    r = child("from settled import schemas; print(schemas.ROOT)", tmp_path / "home")
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == str(tmp_path / "home" / ".sessionkit" / "settled" / "settings")


def test_shipped_code_names_no_dot_folder_but_the_kits():
    """Every `Path.home() / ".x"` in settled/ is ~/.sessionkit (or ~/.local/bin,
    the PATH a launch agent needs): no other home of anyone's is hard-coded in
    what ships."""
    import re
    hits = []
    for f in sorted((REPO / "settled").glob("*.py")):
        for n, line in enumerate(f.read_text().splitlines(), 1):
            for m in re.finditer(r'home\(\)\s*/\s*"(\.[^"/]+)', line):
                if m.group(1) not in (".sessionkit", ".local"):
                    hits.append(f"{f.name}:{n}: {m.group(1)}")
    assert not hits, hits


# --- nothing of the old deploy is required ------------------------------------------------

# The follower's own (push, main) are in tests/test_follower.py, private.

def test_event_without_the_heard_writer_is_127(monkeypatch):
    monkeypatch.setattr(desk, "HEARD", Path("/nonexistent/heard.py"))
    desk._SAID_ABSENT.clear()
    assert desk.event(["mouth", "output"], "a", "b", "test") == 127


def test_log_makes_its_folder(monkeypatch, tmp_path):
    monkeypatch.setattr(desk, "LOG", tmp_path / "log" / "apply.jsonl")
    desk.log({"ok": True})
    assert desk.LOG.read_text() == '{"ok": true}\n'
