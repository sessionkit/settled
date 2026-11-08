"""The shell: desk.css owns the tokens
and the seven classes; the page links it; the ear strip, the tabs and the two rooms are
in the page; the .xdc and the wheel carry the sheet. Static; offline."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGE = (ROOT / "app/index.html").read_text()
CSS = (ROOT / "app/desk.css").read_text()


def test_desk_css_owns_the_tokens_and_the_seven_classes():
    for tok in ("--bg", "--card", "--fg", "--muted", "--line", "--on", "--off", "--knob", "--tint",
                "--danger", "--warn", "--heart"):
        assert re.search(tok + r":", CSS), tok
    for cls in ("row", "toggle", "chooser", "status", "cap", "foot", "bar"):
        assert re.search(r"\." + cls + r"\b", CSS), cls
    assert "--bg:#f2f2f7" not in PAGE and "--bg:#000" not in PAGE   # no second copy of the tokens


def test_page_links_the_sheet_and_has_the_shell():
    assert '<link rel="stylesheet" href="desk.css">' in PAGE
    assert 'class="bar top ear off" id="ear"' in PAGE          # the ear strip, on top, never hidden
    assert 'id="tabs"' in PAGE and 'data-room="talk"' in PAGE and 'data-room="settings"' in PAGE
    assert 'id="room-talk"' in PAGE and 'id="room-settings"' in PAGE
    assert "@media (min-width: 900px)" in PAGE
    assert 'data-settings="hidden"' in PAGE                      # Settings hides on wide; the strip never


def test_the_xdc_and_the_wheel_carry_the_sheet():
    assert "index.html desk.css manifest.toml icon.png" in (ROOT / "Makefile").read_text()
    assert '"*.css"' in (ROOT / "pyproject.toml").read_text()


def test_no_empty_bar_no_doubled_titles_no_grey_paragraph_no_lock_over_talk():
    """An empty bar mid-page, Settings and Connecting twice,
    the grey paragraph, the lock over Talk."""
    assert "container-type:" not in PAGE       # it made the body the containing block of the fixed lock
    assert ".bar.bottom:has(#bar:empty) { display:none }" in PAGE and ".bar:empty" in CSS
    room = PAGE.index('id="room-settings"')
    assert room < PAGE.index('<div class="bar bottom">') < PAGE.index('<script>')   # the bar is Settings'
    assert '<h1 id="title"></h1>' in PAGE and '<div class="foot" id="sub"></div>' in PAGE   # the as-of line is a foot
    assert PAGE.count("Connecting") == 1         # the strip's, once
    assert "never on this phone" not in PAGE        # the consent page replaced that foot
    wheel = (ROOT / "app/webxdc.js").read_text()
    assert 'getElementById("room-settings")' in wheel and "(room || document.body).appendChild(lock)" in wheel
    assert 'id="side"' in PAGE and "settled.settings" in PAGE   # Settings ‹ / › in the strip, wide


def test_the_home_opens_at_the_first_caps_own_margin():
    """The empty nav must not keep its 34 px on the home."""
    assert ".nav:empty { display:none }" in PAGE
