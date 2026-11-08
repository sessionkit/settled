"""The `settled` command (settled/cli.py): every verb reaches web.py
by name, from any directory, with no checkout (enrol
belongs to the kit's command, not a checkout's `make enrol`).

Offline. `install` is exercised only with --dry-run; nothing is loaded."""
import os, plistlib, subprocess, sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from settled import cli  # noqa: E402


def run(*args: str, cwd: Path | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    e = dict(os.environ, PYTHONPATH=str(ROOT))
    if env:
        e.update(env)
    return subprocess.run([sys.executable, "-m", "settled", *args], cwd=cwd or Path("/"),
                          capture_output=True, text=True, env=e, timeout=60)


def test_help_names_every_verb():
    out = run("--help").stdout
    for verb in ("enrol", "serve", "install", "uninstall", "status", "web-room"):
        assert verb in out, verb


def test_the_phone_applier_is_not_a_verb():
    """The chat follower (private/apply.py) is private and out of the wheel, so the
    shipped command has no `apply` verb that would import it."""
    r = run("apply")
    assert r.returncode == 2, r.stdout + r.stderr
    assert "invalid choice" in r.stderr
    assert "apply" not in cli.build_parser()._subparsers._group_actions[0].choices


def test_enrol_runs_from_any_directory(tmp_path, monkeypatch):
    """The code is printed from `/`, with no checkout on the cwd: what `make enrol` could not do."""
    store = tmp_path / "passkeys.json"
    r = run("enrol", cwd=tmp_path, env={"SETTINGS_PASSKEYS": str(store)})
    assert r.returncode == 0, r.stderr
    assert "enrol code:" in r.stdout
    assert (tmp_path / "enrol.json").exists()      # the code's hash, beside the store


def test_install_dry_run_prints_this_python_and_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("SETTLED_HOME", str(tmp_path / "settled"))
    r = run("install", "--dry-run", "--port", "8799", env={"SETTLED_HOME": str(tmp_path / "settled")})
    assert r.returncode == 0, r.stderr
    d = plistlib.loads(r.stdout.encode())
    assert d["Label"] == cli.LABEL == "dev.sessionkit.settled.desk"
    assert d["ProgramArguments"][:4] == [sys.executable, "-m", "settled", "serve"]
    assert d["ProgramArguments"][-2:] == ["--port", "8799"]
    assert d["StandardOutPath"].startswith(str(tmp_path / "settled" / "log"))
    assert "nothing written" in r.stderr
    assert not (tmp_path / "settled").exists()
    assert not (Path(os.environ["HOME"]) / ".sessionkit" / "settled" / "services").exists()


def test_plist_runs_the_serving_python_never_path():
    d = cli.plist(8770)
    assert d["ProgramArguments"][0] == sys.executable
    assert d["KeepAlive"] is True and d["RunAtLoad"] is True


def test_web_room_refuses_until_the_splice():
    r = run("web-room")
    assert r.returncode == 2
    assert "501" in r.stderr and "not a room yet" in r.stderr


def test_pyproject_declares_the_runtime_list_and_the_script():
    """One list: pyproject's dependencies name what requirements.txt names (deploy has one
    list, carried into the package), and the script is `settled`."""
    import tomllib
    p = tomllib.loads((ROOT / "pyproject.toml").read_text())
    deps = {d.split(">")[0].split("<")[0].split("=")[0].strip() for d in p["project"]["dependencies"]}
    req = {ln.split("#")[0].strip() for ln in (ROOT / "requirements.txt").read_text().splitlines()
           if ln.split("#")[0].strip()}
    assert deps == req, (deps, req)
    assert p["project"]["scripts"]["settled"] == "settled.cli:main"
    assert "settled.app" in p["tool"]["setuptools"]["packages"]
    assert p["tool"]["setuptools"]["package-dir"]["settled.app"] == "app"
