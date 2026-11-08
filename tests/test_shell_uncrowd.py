"""The shell, uncrowded.

The two rooms are one switch (a segmented control), at the top in Safari and as the app's tab
bar at the bottom on the home screen (standalone, which needs the manifest). The strip's button
says what it does: Turn off / Turn on (it sets ears.listen). Talk's one setting, Show Debug,
is a row drawn from Talk's own schema, and Talk's frame reloads when it changes.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGE = (ROOT / "app/index.html").read_text()
DESK = (ROOT / "app/desk.css").read_text()


def test_the_rooms_are_one_switch():
    assert '<div class="tabs" id="tabs"><div class="seg">' in PAGE
    assert "background:var(--track)" in PAGE and "background:var(--thumb)" in PAGE
    assert DESK.count("--track:") == 2 and DESK.count("--thumb:") == 2      # light and dark


def test_on_the_home_screen_the_switch_is_the_tab_bar_at_the_bottom():
    i = PAGE.index("@media (display-mode: standalone)")
    block = PAGE[i:PAGE.index("\n  }\n", i)]
    assert ".rooms { order:2; }" in block and ".tabs { order:3;" in block
    assert "env(safe-area-inset-bottom)" in block and ".tab svg { display:block;" in block


def test_the_manifest_makes_it_a_home_screen_app():
    m = json.loads((ROOT / "app/manifest.json").read_text())
    assert m["display"] == "standalone" and m["start_url"] == "./" and m["scope"] == "./"
    assert (ROOT / "app" / m["icons"][0]["src"]).is_file()
    assert '<link rel="manifest" href="manifest.json" crossorigin="use-credentials">' in PAGE
    assert 'name="apple-mobile-web-app-capable" content="yes"' in PAGE
    assert "viewport-fit=cover" in PAGE


def test_the_strip_says_turn_off_and_turn_on():
    assert '"Mute"' not in PAGE and "data-mute" not in PAGE and "dataset.mute" not in PAGE
    assert 'btn.textContent = on ? "Turn off" : "Turn on"' in PAGE
    assert 'send(["ears", "listen"], e.target.dataset.listen === "on")' in PAGE


def test_show_debug_is_a_row_from_talks_schema_only_when_talk_publishes_one():
    assert '(T[talkKey(T)] ? talkDebug(T[talkKey(T)], talkKey(T)) : "")' in PAGE
    assert 'sw(["tool", key, "show_debug"], on)' in PAGE
    assert 'return T && T["ears/talk"] ? "ears/talk" : "talk";' in PAGE   # drawn list, else the kit's
    assert '(t.value || {}).show_debug === true' in PAGE                 # off unless exactly true


def test_talks_frame_reloads_when_its_setting_changes():
    assert 'get(S, ["tools", talkKey(S.tools), "value", "show_debug"]) === true' in PAGE
    assert 'tf.src = tf.getAttribute("src")' in PAGE



def test_talk_is_drawn_when_its_schema_sits_in_the_voice_folder(tmp_path):
    """A fresh python, no SETTINGS_DRAWN, standalone (SETTINGS_ROOT): the default list
    draws ears/talk from <root>/ears/.talk.schema.json (on a laptop, the voice folder)."""
    import os, subprocess, sys
    ears = tmp_path / "root" / "ears"; ears.mkdir(parents=True)
    (ears / ".talk.schema.json").write_text(json.dumps({"type": "object", "properties": {
        "show_debug": {"title": "Show Debug in Talk", "type": "boolean", "default": False}}}))
    env = {k: v for k, v in os.environ.items() if k not in ("SETTINGS_DRAWN", "SETTINGS_ROOT")}
    env["HOME"] = str(tmp_path)
    env["SETTINGS_ROOT"] = str(tmp_path / "root")
    code = "import json; from settled import schemas; print(json.dumps([schemas.KIT is None, schemas.registry()]))"
    r = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    no_kit, registry = json.loads(r.stdout)
    if no_kit:
        assert registry == ["ears/talk"]
    else:
        assert "talk" in registry
