"""TOML beside JSON, and Desk drawn from its dot schema.

Ruled:
Settings reads and writes TOML, keeping every comment and the key order, and
draws Desk's screen from a schema written from desk's own desk.toml.example.

LIVE SAFETY: a deployed desk.toml is LIVE (desk reads it on every redraw).
Nothing here reads it. Every desk.toml below is built from the
LITERAL `LIVE_SHAPED` in this file, in a temp dir that schemas.ROOT is pointed
at; tests/conftest.py has already moved HOME and SETTINGS_ROOT to a sandbox.

Fixtures: tool-schemas/desk.schema.json is the schema this repo ships (not
installed). tests/desk.toml.example is a byte-for-byte copy of
desk's own desk.toml.example, git blob
c5fc68e863ef35f5263aa5b322062e0d9f84c911; a test
below proves the copy is unedited.
"""
import hashlib, json, os, shutil, subprocess, sys, tempfile, tomllib
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))
from settled import desk  # noqa: E402
from settled import schemas  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="settings-toml-"))
DESK = TMP / "desk"
SCHEMA = REPO / "tool-schemas" / "desk.schema.json"
EXAMPLE = HERE / "desk.toml.example"
EXAMPLE_BLOB = "c5fc68e863ef35f5263aa5b322062e0d9f84c911"

# Shaped like the live desk.toml: three keys, each with a comment, plus a head
# comment, a trailing comment and a blank line. A literal, never the live file.
LIVE_SHAPED = '''# desk.toml: Desk's settings (a test copy, shaped like the live one)

# packs: the folder of kind packs
packs = "/Users/you/deskpacks"
# fallback_area: where a card with no area goes
fallback_area = "general-area"   # the catch-all
# card_max_width: the widest the card column grows
card_max_width = "720px"
'''

LOOSE = {"type": "object", "properties": {"packs": {}, "card_max_width": {}, "t": {}}}


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    monkeypatch.setattr(schemas, "ROOT", TMP)
    monkeypatch.setattr(schemas, "DRAWN", ("ears", "desk"))


def fresh(text=LIVE_SHAPED, schema=None):
    shutil.rmtree(DESK, ignore_errors=True)
    DESK.mkdir(parents=True)
    if schema is None:
        shutil.copy(SCHEMA, DESK / ".desk.schema.json")
    else:
        (DESK / ".desk.schema.json").write_text(json.dumps(schema))
    if text is not None:
        (DESK / "desk.toml").write_text(text)
    return DESK / "desk.toml"


def snap(p: Path) -> tuple:
    return p.read_bytes(), p.stat().st_mtime_ns


# --- the pair: .NAME.schema.json with NAME.toml, as with NAME.json ------------

def test_a_dot_schema_pairs_with_name_toml_as_with_name_json():
    p = fresh()
    assert schemas.file_path("desk") == p
    assert schemas.registry() == ["desk"]          # no ears here; desk has its pair
    t = schemas.tool_state("desk")
    assert t["schema_file"] == "desk.schema.json" and t["file"].endswith("desk/desk.toml")
    assert t["value"] == {"packs": "/Users/you/deskpacks", "fallback_area": "general-area",
                          "card_max_width": "720px"}
    assert "error" not in t


def test_with_no_file_a_tool_still_means_name_json():
    fresh(text=None)
    assert schemas.file_path("desk") == DESK / "desk.json"


def test_when_both_exist_settings_picks_neither():
    toml = fresh()
    (DESK / "desk.json").write_text('{"card_max_width": "640px"}\n')
    before_t, before_j = snap(toml), snap(DESK / "desk.json")
    why = "both desk.json and desk.toml exist; settings won't pick one, so remove one"
    assert schemas.ambiguous("desk") == why
    t = schemas.tool_state("desk")
    assert t["error"] == why and "value" not in t
    assert schemas.write("desk", ["card_max_width"], "900px") == f"refused: {why}"
    assert snap(toml) == before_t and snap(DESK / "desk.json") == before_j


def test_a_toml_file_that_wont_parse_is_an_error_not_a_crash():
    p = fresh('card_max_width = "720px\n')
    assert schemas.tool_state("desk")["error"].startswith("its settings can't be read: TOMLDecodeError")
    before = snap(p)
    assert schemas.write("desk", ["card_max_width"], "900px") == "refused: desk.toml can't be read (TOMLDecodeError)"
    assert snap(p) == before


# --- the write: tomlkit round-trip, comments and order kept -----------------

def test_a_write_round_trips_byte_for_byte_apart_from_the_value():
    p = fresh()
    assert desk.apply(["tool", "desk", "card_max_width"], "900px") == "ok"
    assert p.read_text() == LIVE_SHAPED.replace('"720px"', '"900px"')
    assert list(tomllib.loads(p.read_text())) == ["packs", "fallback_area", "card_max_width"]
    assert not list(DESK.glob(".*.tmp"))            # the one-step replace left nothing


def test_a_value_with_a_trailing_comment_keeps_it():
    p = fresh()
    assert schemas.write("desk", ["fallback_area"], "inbox-area") == "ok"
    assert p.read_text() == LIVE_SHAPED.replace('"general-area"', '"inbox-area"')


def test_a_new_key_joins_at_the_end_and_nothing_else_moves():
    p = fresh()
    assert schemas.write("desk", ["decider"], "ana") == "ok"
    assert p.read_text() == LIVE_SHAPED + 'decider = "ana"\n'


def test_a_write_that_would_lose_a_comment_refuses_and_the_file_is_unchanged():
    # tomlkit turns a commented scalar into a [packs] table at the end: its
    # trailing comment and its place would both go. The schema allows it (LOOSE),
    # so only the comment proof stands in the way.
    p = fresh('# head\npacks = "/x"   # where the packs are\ncard_max_width = "720px"\n', LOOSE)
    before = snap(p)
    r = schemas.write("desk", ["packs"], {"dir": "/x"})
    assert r == "refused: the write would lose a comment in desk.toml: # where the packs are", r
    assert snap(p) == before
    assert not list(DESK.glob(".*.tmp"))


def test_a_null_write_to_toml_refuses_toml_has_no_null():
    p = fresh(schema=LOOSE)
    before = snap(p)
    assert schemas.write("desk", ["card_max_width"], None) == "refused: TOML has no null, so desk.toml can't hold one"
    assert schemas.write("desk", ["t"], {"a": [1, None]}) == "refused: TOML has no null, so desk.toml can't hold one"
    assert snap(p) == before


def test_without_tomlkit_a_toml_write_refuses_and_a_read_still_works(monkeypatch):
    p = fresh()
    before = snap(p)
    monkeypatch.setattr(schemas, "tomlkit", None)
    r = schemas.write("desk", ["card_max_width"], "900px")
    assert r.startswith("refused: writing desk.toml needs tomlkit, which this python (") and \
        r.endswith(") does not have"), r
    assert snap(p) == before
    assert schemas.tool_state("desk")["value"]["card_max_width"] == "720px"   # the screen still draws


def test_when_tomlkit_cannot_be_imported_a_toml_write_refuses_naming_it(tmp_path):
    """The import itself fails (monkeypatch the import, not
    the name, so the real failure path runs): a fresh python with sys.modules["tomlkit"] = None,
    so `import tomlkit` raises ImportError, as on Homebrew's python3. The write
    refuses naming tomlkit, the file is byte-identical, and the screen draws."""
    desk = tmp_path / "root" / "desk"
    desk.mkdir(parents=True)
    shutil.copy(SCHEMA, desk / ".desk.schema.json")
    p = desk / "desk.toml"
    p.write_text(LIVE_SHAPED)
    before = snap(p)
    env = {k: v for k, v in os.environ.items() if k not in ("SETTINGS_DRAWN", "SETTINGS_ROOT")}
    env["HOME"] = str(tmp_path)
    env["SETTINGS_ROOT"] = str(tmp_path / "root")
    code = ("import json, sys; sys.modules['tomlkit'] = None\n"
            "try:\n    import tomlkit\nexcept ImportError:\n    blocked = True\n"
            "else:\n    blocked = False\n"
            "from settled import schemas\n"
            "print(json.dumps([blocked, schemas.tomlkit is None, "
            "schemas.write('desk', ['card_max_width'], '900px'), "
            "schemas.tool_state('desk')['value']['card_max_width']]))")
    r = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    blocked, absent, said, drawn = json.loads(r.stdout)
    assert blocked and absent
    assert said == f"refused: writing desk.toml needs tomlkit, which this python ({sys.executable}) does not have", said
    assert snap(p) == before
    assert drawn == "720px"                         # reads use stdlib tomllib


def test_a_json_tool_still_writes_without_tomlkit(monkeypatch):
    monkeypatch.setattr(schemas, "tomlkit", None)
    fresh(text=None, schema=LOOSE)
    assert schemas.write("desk", ["card_max_width"], "900px") == "ok"
    assert json.loads((DESK / "desk.json").read_text()) == {"card_max_width": "900px"}


def test_a_write_the_schema_refuses_leaves_the_toml_unchanged():
    p = fresh()
    before = snap(p)
    assert schemas.write("desk", ["card_max_width"], "1601px").startswith("refused: /card_max_width: does not match")
    assert schemas.write("desk", ["bogus"], "x") == "refused: the schema has no bogus"
    assert snap(p) == before


def test_the_comment_scanner_skips_a_hash_inside_any_string():
    text = ('a = "x # not" # one\n'
            "b = 'y # not' #two  \n"
            'c = """\nline # not\n""" # three\n'
            "d = '''\n# not\n''' # four\n"
            'e = "q\\" # not" # five\n'
            "# six\n")
    assert schemas.comments(text) == ["# one", "#two", "# three", "# four", "# five", "# six"]
    assert schemas.lost_comments(text, text) == []
    assert schemas.lost_comments("# a\n# a\n", "# a\n") == ["# a"]   # a repeat counts


def test_order_kept_sees_a_move_at_any_level():
    assert schemas._order_kept({"a": 1, "b": 2}, {"a": 1, "b": 3, "c": 4})
    assert not schemas._order_kept({"a": 1, "b": 2}, {"b": 2, "a": 1})
    assert not schemas._order_kept({"t": {"x": 1, "y": 2}}, {"t": {"y": 2, "x": 1}})


# --- Desk's schema, written from desk's own desk.toml.example --------------------

def test_the_example_fixture_is_jims_file_unedited():
    data = EXAMPLE.read_bytes()
    assert hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest() == EXAMPLE_BLOB


def test_the_desk_schema_covers_every_key_the_example_documents_and_accepts_it():
    s = json.loads(SCHEMA.read_text())
    ex = tomllib.loads(EXAMPLE.read_text())
    assert set(s["properties"]) == set(ex) and len(ex) == 13
    assert s["additionalProperties"] is False
    assert schemas.unchecked(s) == []
    assert schemas.errors(s, ex) == []


def test_the_desk_schema_rejects_an_unknown_key():
    s = json.loads(SCHEMA.read_text())
    assert schemas.errors(s, {"card_width": "720px"}) == ["/card_width: not a setting here"]


@pytest.mark.parametrize("width", ["319px", "1601px", "720", "720em", "0720px", "72.5px", 720, "", "3200px"])
def test_the_desk_schema_rejects_a_card_width_outside_320_to_1600px(width):
    s = json.loads(SCHEMA.read_text())
    assert schemas.errors(s, {"card_max_width": width}), width


@pytest.mark.parametrize("width", ["320px", "640px", "999px", "1000px", "1599px", "1600px"])
def test_the_desk_schema_accepts_a_card_width_from_320_to_1600px(width):
    assert schemas.errors(json.loads(SCHEMA.read_text()), {"card_max_width": width}) == []


def test_the_desk_schema_refuses_blank_names_and_quotes():
    s = json.loads(SCHEMA.read_text())
    assert schemas.errors(s, {"decider": "  "}) and schemas.errors(s, {"trusted_chats": [""]})
    assert schemas.errors(s, {"channel": 'de"sk'}) and schemas.errors(s, {"packs": "a\\b"})
    assert schemas.errors(s, {"ask_senders": []}) == []          # [] is a choice: no ask draws


def test_card_width_and_packs_say_a_change_needs_a_new_app_in_chat_12():
    p = json.loads(SCHEMA.read_text())["properties"]
    for k in ("card_max_width", "packs"):
        assert "new app sent to chat 12" in p[k]["x-hint"], k


# --- Desk is drawn ----------------------------------------------------------

def test_desk_is_in_drawns_default_and_drawn_from_a_temp_home(tmp_path):
    """A fresh python, with no SETTINGS_DRAWN, so DRAWN is its default; ROOT is
    a temp folder (the standalone root's own default is test_paths.py's)."""
    (tmp_path / "root" / "desk").mkdir(parents=True)
    shutil.copy(SCHEMA, tmp_path / "root" / "desk" / ".desk.schema.json")
    (tmp_path / "root" / "desk" / "desk.toml").write_text(LIVE_SHAPED)
    env = {k: v for k, v in os.environ.items() if k not in ("SETTINGS_DRAWN", "SETTINGS_ROOT")}
    env["HOME"] = str(tmp_path)
    env["SETTINGS_ROOT"] = str(tmp_path / "root")
    code = ("import json; from settled import schemas; print(json.dumps([schemas.DRAWN, schemas.registry(), "
            "schemas.tool_state('desk')['value']]))")
    r = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    drawn, registry, value = json.loads(r.stdout)
    assert "desk" in drawn
    assert registry == ["desk"]
    assert value["card_max_width"] == "720px"


def test_the_web_state_carries_desk_when_the_pair_exists():
    fresh()
    t = desk.tools({}, {"up": False, "device": ""})
    assert set(t) == {"desk"}
    assert t["desk"]["schema"]["title"] == "Desk" and t["desk"]["value"]["packs"] == "/Users/you/deskpacks"
    json.dumps(t)                                   # the state is JSON: TOML's data must be too
    (DESK / "desk.toml").unlink()
    (DESK / ".desk.schema.json").unlink()
    assert desk.tools({}, {"up": False, "device": ""}) == {}


def test_a_toml_date_draws_as_its_iso_text():
    fresh('when = 2030-06-09T14:16:00Z  # a date\n', {"type": "object", "properties": {"when": {}}})
    v = schemas.tool_state("desk")["value"]
    assert v == {"when": "2030-06-09T14:16:00+00:00"} and json.dumps(v)
