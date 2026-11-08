"""The first passkey, from the README alone.

A fresh-HOME walk of the README, the built wheel under `uv tool install`, found
two ways a stranger would be stopped:

- every address the commands printed was `http://127.0.0.1:<port>/`, but the
  desk allows only `http://localhost:<port>` as its origin, and an IP address
  cannot hold a passkey: that page says "can't hold a passkey" and offers no
  button;
- the README and the page disagreed on whether the first passkey takes a code.

Since there is no trust on first use, it always does: a fresh, empty store
offers "Enrol this device", and the README says to run `settled enrol` on the Mac,
open http://localhost:8770/, choose Enrol this device and type the code. No
first-one-is-free wording is left anywhere a user reads.

These tests hold the README, the printed lines and the page to one story. The
walk itself (each command, what it printed, the passkey count after) is done by
hand, on a fresh HOME; these tests keep what it found from coming back.

Offline. `install` runs against a stub launchctl in a scratch HOME; nothing is loaded."""
import os, re, socket, stat, subprocess, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from settled import webauth  # noqa: E402

README = (ROOT / "README.md").read_text()
PAGE = (ROOT / "app" / "webxdc.js").read_text()
FAKE_LAUNCHCTL = "#!/bin/sh\nexit 0\n"


def env_for(home: Path, bin_: Path | None = None) -> dict:
    e = {k: v for k, v in os.environ.items() if k not in ("SETTLED_HOME", "SETTINGS_PASSKEYS")}
    e.update(HOME=str(home), PYTHONPATH=str(ROOT))
    if bin_:
        e["PATH"] = str(bin_) + os.pathsep + e.get("PATH", "")
    return e


def first_passkey_section() -> str:
    """The README's first-passkey paragraph: from its bold lead to the next blank line."""
    m = re.search(r"\*\*The first passkey\.\*\*(.*?)\n\n", README, re.S)
    assert m, "the README has no **The first passkey.** paragraph"
    return m.group(1)


def test_install_points_at_the_address_that_can_hold_a_passkey(tmp_path):
    home, bin_ = tmp_path / "home", tmp_path / "bin"
    home.mkdir(), bin_.mkdir()
    fake = bin_ / "launchctl"
    fake.write_text(FAKE_LAUNCHCTL)
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    r = subprocess.run([sys.executable, "-m", "settled", "install", "--port", "8799"], cwd="/",
                       env=env_for(home, bin_), capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "http://localhost:8799/" in r.stdout, r.stdout
    assert "127.0.0.1" not in r.stdout, "an IP address cannot hold a passkey: never send the user there"
    assert "settled enrol" in r.stdout and "Enrol this device" in r.stdout, r.stdout
    assert "Set up" not in r.stdout and "no code" not in r.stdout, r.stdout


def test_enrol_points_a_fresh_install_at_localhost(tmp_path):
    r = subprocess.run([sys.executable, "-m", "settled", "enrol"], cwd="/",
                       env=env_for(tmp_path), capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "enrol code:" in r.stdout
    assert "http://localhost:8770/" in r.stdout and "Enrol this device" in r.stdout, r.stdout
    assert "no code" not in r.stdout and "Set up" not in r.stdout, r.stdout


def spare() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_serve_prints_the_pages_own_origin(tmp_path):
    port = spare()
    p = subprocess.Popen([sys.executable, "-u", "-m", "settled", "serve", "--port", str(port)], cwd="/",
                         env=env_for(tmp_path), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        lines, t0 = [], time.time()
        while time.time() - t0 < 20:
            line = p.stdout.readline()
            if not line:
                break
            lines.append(line)
            if line.startswith("settings web:"):
                break
        banner = next((ln for ln in lines if ln.startswith("settings web:")), "")
        assert banner.startswith(f"settings web: http://localhost:{port}/"), "".join(lines)
    finally:
        p.kill()
        p.wait(timeout=10)


def test_readme_first_passkey_is_localhost_and_the_enrol_code():
    s = first_passkey_section()
    assert "`settled enrol`" in s and "on the Mac" in s
    assert "http://localhost:8770/" in s
    assert "not `127.0.0.1`" in s                     # said, so nobody opens the dead end
    assert "**Enrol this device**" in s and "type the code" in s
    assert s.index("settled enrol") < s.index("http://localhost:8770/") < s.index("Enrol this device")
    assert "passkeys: 1" in s                         # what `settled status` prints after


def test_readme_lock_bullet_is_the_enrol_code_path():
    m = re.search(r"- \*\*The lock:\*\*(.*?)\n- ", README, re.S)
    assert m, "the README has no **The lock:** bullet"
    s = " ".join(m.group(1).split())
    for step in ("`settled enrol`", "`http://localhost:8770/`", "Enrol this device", "type the code"):
        assert step in s, (step, s)


FREE_FIRST = re.compile(r"no code is needed|needs no code|first one is free|one tap on|"
                        r"Set up a passkey|(?<!no )trust on first use|\bchoose Set up\b", re.I)


def test_no_first_one_is_free_wording_anywhere_a_user_reads():
    """No trust on first use: nothing shipped says the first passkey is free."""
    shipped = [ROOT / "README.md", ROOT / "app" / "webxdc.js", ROOT / "app" / "index.html",
               *sorted((ROOT / "settled").glob("*.py"))]
    hits = [f"{f.relative_to(ROOT)}:{n}: {ln.strip()}" for f in shipped
            for n, ln in enumerate(f.read_text().splitlines(), 1) if FREE_FIRST.search(ln)]
    assert not hits, "\n".join(hits)


def test_readme_install_block_does_not_send_a_fresh_user_to_enrol():
    block = re.search(r"```sh\n(uv tool install settled.*?)```", README, re.S).group(1)
    assert "--from dist/settled-*.whl settled" in block   # uv needs the package name after --from


def test_an_empty_store_offers_enrol():
    """What the README promises, from the code: a locked page never passes may_register,
    so no passkey anywhere => enrol, the code path (no trust on first use)."""
    assert webauth.offer([], "localhost", may_register=False)[0]["mode"] == "enrol"
    other = [{"id": "k", "rp_id": "elsewhere.example"}]
    assert webauth.offer(other, "localhost", may_register=False)[0]["mode"] == "enrol"


def test_every_label_the_readme_quotes_is_on_the_page():
    for label in ("Enrol this device", "Make the passkey", "can't hold a passkey"):
        assert label in README, f"README no longer quotes {label!r}"
        assert label in PAGE, f"the page has no {label!r} for the README to point at"
    for gone in ("Set up a passkey", '"Set up"'):   # the free first passkey's lock is gone
        assert gone not in PAGE, gone


def test_the_enrol_sheet_names_the_installed_command():
    assert "<code>settled enrol</code>" in PAGE
    assert "make enrol" not in PAGE, "a wheel install has no Makefile"


def test_enrol_code_errors_name_the_installed_command(tmp_path, monkeypatch):
    """A wheel install has no Makefile: every enrol-code error the page shows names
    `settled enrol`, the command the README and the enrol sheet send the user to."""
    monkeypatch.setattr(webauth, "ENROL", tmp_path / "enrol.json")
    seen = [webauth.check_enrol_code("X", consume=False)]                     # none pending
    webauth.new_enrol_code(now=0)
    seen.append(webauth.check_enrol_code("X", now=webauth.ENROL_TTL + 1))     # expired
    webauth.new_enrol_code()
    seen += [webauth.check_enrol_code("WRONG") for _ in range(webauth.ENROL_TRIES)]  # to burnt
    assert len(seen) == webauth.ENROL_TRIES + 2 and all(seen), seen
    assert not any("make enrol" in s for s in seen), seen
    for s in (seen[0], seen[1], seen[-1]):
        assert "settled enrol" in s, s
