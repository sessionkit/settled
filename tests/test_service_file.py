"""settled's service file, for `sessionkit service`.

The kit finds a service from one JSON file its package's own install writes,
`~/.sessionkit/<package>/services/<name>.json`, and keeps no list of its own
(sessionkit docs/service.md). So `settled install` writes
`~/.sessionkit/settled/services/desk.json` with `enabled: true`, and
`settled uninstall` rewrites it with `enabled: false` and keeps it.

The schema is the kit's own: tests/fixtures/sessionkit/service.schema.json is
sessionkit's src/sessionkit/service.schema.json, byte for byte, as the kit's
service group ships it.
SCHEMA_SHA256 pins those bytes, so a hand edit to the fixture is caught.
The kit's reader adds rules the schema cannot say (services.py `check` and
`load`): the label sits under `dev.sessionkit.<the folder>.`, the
name is the label's tail and no group's name, and no other package's file
declares the same name (two such files are BOTH bad). Those are held here
too, and, where sessionkit's reader is importable, by the reader itself.

Every run is under a scratch HOME, with a fake `launchctl` first on PATH that
logs its arguments: nothing is loaded and nothing outside the scratch is read.
"""
import hashlib
import json
import os
import plistlib
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = ROOT / "tests" / "fixtures" / "sessionkit" / "service.schema.json"
SCHEMA_SHA256 = "e35f5f76553446c8e52c66274fea775bf119dab3ae69cb7b4ab5458546f51182"
# sessionkit services.py PACKAGES: a service's name may not be one.
KIT_GROUPS = ("kit", "mail", "phone", "tools", "view", "voice")
# Names other packages' files already declare. `web` is sessiontools' front door,
# tools/services/web.json (src/sessiontools/web/cli.py SERVICE = "web").
TAKEN = ("web", "talk", "pane-state", "sound-engine", "default-model")

sys.path.insert(0, str(ROOT))
from settled import cli  # noqa: E402

FAKE_LAUNCHCTL = """#!/bin/sh
echo "$@" >> "$LAUNCHCTL_LOG"
if [ "$1" = bootstrap ]; then exit "${LAUNCHCTL_BOOTSTRAP_RC:-0}"; fi
exit 0
"""


@pytest.fixture
def mac(tmp_path):
    """A scratch HOME and a fake launchctl. Returns (home, run, log)."""
    home = tmp_path / "home"
    home.mkdir()
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    fake = bin_ / "launchctl"
    fake.write_text(FAKE_LAUNCHCTL)
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "launchctl.log"
    log.touch()

    def run(*args, **env):
        e = {k: v for k, v in os.environ.items() if k != "SETTLED_HOME"}
        e.update(HOME=str(home), PYTHONPATH=str(ROOT), LAUNCHCTL_LOG=str(log),
                 PATH=str(bin_) + os.pathsep + os.environ.get("PATH", ""), **env)
        return subprocess.run([sys.executable, "-m", "settled", *args], cwd="/", env=e,
                              capture_output=True, text=True, timeout=60)

    return home, run, log


def service_file(home: Path) -> Path:
    return home / ".sessionkit" / "settled" / "services" / "desk.json"


def kit_walk(home: Path) -> list[Path]:
    """sessionkit services.py `walk`: .json at the two depths under each package's services/."""
    base = home / ".sessionkit"
    return sorted(p for p in [*base.glob("*/services/*.json"), *base.glob("*/services/*/*.json")]
                  if p.is_file())


# --- the shape --------------------------------------------------------------


def test_the_schema_fixture_is_the_kits_own():
    assert hashlib.sha256(SCHEMA.read_bytes()).hexdigest() == SCHEMA_SHA256


@pytest.mark.parametrize("port,enabled", [(8770, True), (8770, False), (8799, True)])
def test_the_record_validates_against_the_kits_schema(port, enabled):
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads(SCHEMA.read_text())
    jsonschema.Draft202012Validator.check_schema(schema)
    rec = cli.service_record(port, enabled)
    jsonschema.Draft202012Validator(schema).validate(rec)
    assert rec["enabled"] is enabled and rec["port"] == port


def test_the_schema_check_has_teeth():
    """The fixture refuses what the kit refuses, so a pass above means something."""
    jsonschema = pytest.importorskip("jsonschema")
    v = jsonschema.Draft202012Validator(json.loads(SCHEMA.read_text()))
    good = cli.service_record(8770, True)
    for bad in ({**good, "service": 2}, {k: x for k, x in good.items() if k != "install"},
                {**good, "run": ["settled", "serve"]}, {**good, "label": "com.example.settled.desk"}):
        assert not v.is_valid(bad), bad


def test_the_record_meets_the_kit_readers_rules():
    rec = cli.service_record(8770, True)
    assert rec["label"] == cli.LABEL == "dev.sessionkit.settled.desk"
    assert rec["label"].startswith("dev.sessionkit.settled.")       # the folder it sits in
    assert rec["name"] == rec["label"][len("dev.sessionkit.settled."):] == "desk"
    assert rec["name"] not in KIT_GROUPS
    assert rec["name"] not in TAKEN, "two files with one name are both `bad file` in the kit"
    assert rec["install"] == "settled install" and rec["uninstall"] == "settled uninstall"
    assert rec["bind"] == "127.0.0.1"
    assert cli.service_record(8799, True)["install"] == "settled install --port 8799"


# --- the install writes it ---------------------------------------------------


def test_a_fresh_install_writes_the_service_file(mac):
    home, run, log = mac
    r = run("install", "--port", "8799")
    assert r.returncode == 0, r.stdout + r.stderr
    f = service_file(home)
    assert json.loads(f.read_text()) == cli.service_record(8799, True)
    assert stat.S_IMODE(f.stat().st_mode) == 0o644
    assert kit_walk(home) == [f], "the kit's walk finds exactly this file"
    plist = home / "Library" / "LaunchAgents" / "dev.sessionkit.settled.desk.plist"
    assert plistlib.loads(plist.read_bytes())["Label"] == json.loads(f.read_text())["label"]
    calls = log.read_text().splitlines()
    assert any(c.startswith("bootstrap gui/") and c.endswith(str(plist)) for c in calls), calls
    assert str(f) in r.stdout
    assert not list(home.glob("**/*.tmp")) and not list(home.glob("**/.*.tmp"))


def test_the_dotdirs_a_fresh_install_makes(mac):
    """One home: only ~/.sessionkit (beside macOS's own ~/Library, where launchd reads plists)."""
    home, run, _ = mac
    assert run("install").returncode == 0
    assert sorted(p.name for p in home.iterdir()) == [".sessionkit", "Library"]


def test_uninstall_keeps_the_file_and_says_disabled(mac):
    home, run, _ = mac
    assert run("install", "--port", "8799").returncode == 0
    r = run("uninstall")
    assert r.returncode == 0, r.stdout + r.stderr
    assert json.loads(service_file(home).read_text()) == cli.service_record(8799, False)
    assert not (home / "Library" / "LaunchAgents" / "dev.sessionkit.settled.desk.plist").exists()


def test_a_failed_bootstrap_still_declares_the_service(mac):
    """The file is the owner's word that this Mac should run it: `sessionkit service`
    then shows the job down, beside its install command."""
    home, run, _ = mac
    r = run("install", LAUNCHCTL_BOOTSTRAP_RC="5")
    assert r.returncode == 1
    assert "bootstrap failed" in r.stderr
    assert json.loads(service_file(home).read_text())["enabled"] is True


def test_a_failed_write_is_said_and_fails_the_verb(mac):
    home, run, log = mac
    services = service_file(home).parent
    services.parent.mkdir(parents=True)
    services.write_text("a file where the folder goes")
    r = run("install")
    assert r.returncode == 1
    assert "could not write" in r.stderr
    assert any(c.startswith("bootstrap ") for c in log.read_text().splitlines())   # the load still ran


def test_install_retires_the_old_label(mac):
    """A Mac that ran `settled install` before the service file has dev.sessionkit.settled.web:
    it is booted out and its plist removed, so two desks never share a port."""
    home, run, log = mac
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    old = agents / "dev.sessionkit.settled.web.plist"
    old.write_bytes(plistlib.dumps({"Label": "dev.sessionkit.settled.web"}))
    r = run("install")
    assert r.returncode == 0, r.stderr
    assert not old.exists()
    calls = log.read_text().splitlines()
    assert any(c.startswith("bootout gui/") and c.endswith("/dev.sessionkit.settled.web") for c in calls)
    assert "booted out the old dev.sessionkit.settled.web" in r.stdout


def test_dry_run_writes_no_service_file(mac):
    home, run, log = mac
    r = run("install", "--dry-run")
    assert r.returncode == 0
    assert "desk.json" in r.stderr and "nothing written" in r.stderr
    assert not (home / ".sessionkit").exists()
    assert log.read_text() == ""


def test_settled_home_does_not_move_the_service_file(mac, tmp_path):
    """The kit reads ~/.sessionkit, fixed; SETTLED_HOME moves settled's state only."""
    home, run, _ = mac
    assert run("install", SETTLED_HOME=str(tmp_path / "elsewhere")).returncode == 0
    assert service_file(home).is_file()
    assert not (tmp_path / "elsewhere" / "services").exists()


# --- the kit's own reader, where it is installed ----------------------------


def test_the_kits_reader_lists_it(mac):
    """sessionkit's services.py (the service group, after the 0.3.1 wheel) reads the file as a
    usable service, in the `settled` group, with no problem and no note."""
    services = pytest.importorskip("sessionkit.services")
    home, run, _ = mac
    assert run("install").returncode == 0
    files = services.load_all(services.root(home))
    assert [(f.package, f.name, f.problems, f.notes, f.data["enabled"]) for f in files] == \
        [("settled", "desk", [], [], True)]
