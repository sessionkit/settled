"""Offline tests for the screens drawn from a JSON Schema (schemas.py, desk's tool path).

Never touches a live settings folder: SETTINGS_ROOT points at a temp dir BEFORE schemas
is imported, and the ears schema there is a copy of the one in tests/. Device
queries are stubbed. Run: python3 tests/test_schemas.py (or pytest).

The settings app draws its screens
from each tool's schema; the ears first.
"""
import json, os, shutil, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
TMP = Path(tempfile.mkdtemp(prefix="settings-schemas-"))
os.environ["SETTINGS_ROOT"] = str(TMP)
os.environ["SETTINGS_DRAWN"] = "ears"
sys.path.insert(0, str(HERE.parent))
from settled import schemas  # noqa: E402
from settled import desk  # noqa: E402

EARS = TMP / "ears"
LIVE = {"_about": "a note for people", "listen": True, "input": "default",
        "stt": {"url": "http://127.0.0.1:8890/v1", "model": "mlx-community/parakeet-tdt-0.6b-v3"}}
APPLE = {"url": "http://127.0.0.1:2023/v1"}


def fake_devices(kind):
    return ["airpods", "MacBook Pro Microphone"] if kind == "input" else ["airpods"]


def install(set_):
    """Point schemas at THIS module's temp dir and stub the device query. schemas
    binds ROOT / DRAWN at import, and another module (tests/test_isolation.py,
    collected first) may have imported it already. `set_` is
    monkeypatch.setattr under pytest, so each is undone after every test: this
    module once set `desk.devices` at IMPORT, and every test after it saw the
    fake. Plain setattr when this file runs as a script."""
    set_(schemas, "ROOT", TMP)
    set_(schemas, "DRAWN", ("ears",))
    set_(desk, "devices", fake_devices)


try:
    import pytest
except ImportError:  # run as a script without pytest: __main__ installs the fakes
    pytest = None

if pytest is not None:
    @pytest.fixture(autouse=True)
    def fakes(monkeypatch):
        install(monkeypatch.setattr)


def fresh(doc=None):
    shutil.rmtree(EARS, ignore_errors=True)
    EARS.mkdir(parents=True)
    shutil.copy(HERE / "ears.schema.json", EARS / "ears.schema.json")
    (EARS / "ears.json").write_text(json.dumps(LIVE if doc is None else doc, indent=2) + "\n")


def ears() -> dict:
    return json.loads((EARS / "ears.json").read_text())


def test_registry_is_the_drawn_tools_that_publish_a_schema():
    fresh()
    (TMP / "mail").mkdir(exist_ok=True)
    (TMP / "mail" / "mail.schema.json").write_text("{}")
    assert schemas.registry() == ["ears"]          # mail publishes one, but is not drawn yet
    (EARS / "ears.schema.json").unlink()
    assert schemas.registry() == []


def test_the_live_file_validates_and_bad_values_name_their_key():
    fresh()
    s = json.loads((EARS / "ears.schema.json").read_text())
    assert schemas.unchecked(s) == []
    assert schemas.errors(s, LIVE) == []
    assert schemas.errors(s, dict(LIVE, listen="yes")) == ["/listen: wants boolean, got string"]
    assert schemas.errors(s, dict(LIVE, bogus=1)) == ["/bogus: not a setting here"]
    assert schemas.errors(s, dict(LIVE, stt={"url": "ftp://x"}))[0].startswith("/stt: is none of Parakeet")
    assert schemas.errors(s, dict(LIVE, stt=APPLE)) == []
    assert schemas.errors(s, dict(LIVE, stt={"url": "http://h:1/v1", "model": "m"})) == []  # Other


def test_true_is_not_one():
    assert schemas.errors({"const": 1}, True) == ["/: must be 1"]
    assert schemas.errors({"type": "integer"}, True) == ["/: wants integer, got boolean"]
    assert schemas.errors({"enum": [True]}, 1) != []


def test_a_write_keeps_every_other_key_and_the_notes():
    fresh()
    assert schemas.write("ears", ["stt"], APPLE) == "ok"
    d = ears()
    assert d["stt"] == APPLE
    assert d["_about"] == "a note for people" and d["listen"] is True and d["input"] == "default"
    assert list(d) == list(LIVE)                    # the order is kept too
    assert not list(EARS.glob(".*.tmp"))            # nothing left behind by the one-step write


def test_a_write_that_breaks_the_schema_is_refused_and_nothing_changes():
    fresh()
    before = (EARS / "ears.json").read_text()
    assert schemas.write("ears", ["stt"], {"url": "ftp://x"}).startswith("refused: /stt: is none of")
    assert schemas.write("ears", ["listen"], "yes") == "refused: /listen: wants boolean, got string"
    assert schemas.write("ears", ["bogus"], 1) == "refused: the schema has no bogus"
    assert schemas.write("ears", ["_about"], "x").startswith("refused: not a setting")
    assert schemas.write("ears", ["$schema"], "x").startswith("refused: not a setting")
    assert schemas.write("mail", ["hooks"], True) == "refused: no tool 'mail'"
    assert (EARS / "ears.json").read_text() == before


def test_one_bad_key_elsewhere_never_locks_the_screen_out():
    fresh(dict(LIVE, input=7))                       # broken by hand, not by the screen
    assert schemas.write("ears", ["listen"], False) == "ok"
    assert ears()["listen"] is False and ears()["input"] == 7


def test_a_schema_with_a_keyword_it_cant_check_refuses_every_write():
    fresh()
    s = json.loads((EARS / "ears.schema.json").read_text())
    s["properties"]["stt"]["anyOf"][3]["properties"]["url"]["format"] = "uri"
    (EARS / "ears.schema.json").write_text(json.dumps(s))
    r = schemas.write("ears", ["listen"], False)
    assert r == "refused: the schema uses what settings can't check yet: /stt/anyOf/3/url: format", r
    assert ears()["listen"] is True


def test_a_missing_file_is_written_from_nothing():
    fresh()
    (EARS / "ears.json").unlink()
    assert schemas.write("ears", ["listen"], False) == "ok"
    assert ears() == {"listen": False}


def test_tool_state_draws_from_schema_and_values_without_the_notes():
    fresh()
    t = schemas.tool_state("ears")
    assert t["schema"]["title"] == "Ears" and t["schema_file"] == "ears.schema.json"
    assert t["value"] == {k: v for k, v in LIVE.items() if k != "_about"}
    (EARS / "ears.json").write_text("{not json")
    assert schemas.tool_state("ears")["error"].startswith("its settings can't be read: JSONDecodeError")


def test_apply_routes_tool_paths_and_checks_a_live_input():
    fresh()
    assert desk.apply(["tool", "ears", "stt"], APPLE) == "ok"
    assert ears()["stt"] == APPLE
    assert desk.apply(["tool", "ears", "input"], "airpods") == "ok"
    assert desk.apply(["tool", "ears", "input"], "Nope") == "refused: no input 'Nope'"
    assert desk.apply(["tool", "ears", "input"], "default") == "ok"
    assert desk.apply(["tool", "ears"], True) == "refused: path not allowed"
    assert desk.apply(["tool", "mail", "hooks"], True) == "refused: no tool 'mail'"


def test_lookup_reads_a_tool_path_from_the_state():
    st = {"tools": {"ears": {"value": {"stt": APPLE}}}}
    assert desk.lookup(st, ["tool", "ears", "stt"]) == APPLE
    assert desk.lookup(st, ["tool", "ears", "listen"]) is None


def test_the_state_carries_each_drawn_tool_and_the_ears_status():
    fresh()
    t = desk.tools({"listen": True}, {"up": True, "device": "airpods"})
    assert set(t) == {"ears"} and t["ears"]["status"] == {"up": True, "text": "Capturing on airpods"}
    assert desk.tools({"listen": False}, {"up": True, "device": "x"})["ears"]["status"]["text"] == "Stopped"
    assert desk.tools({}, {"up": False, "device": ""})["ears"]["status"] == {"up": False, "text": "Starting…"}


def test_a_refused_tap_rides_back_in_the_state_and_an_ok_one_does_not():
    assert desk.said({"x": 1}, ["tool", "ears", "listen"], "refused: /listen: wants boolean, got string") == \
        {"x": 1, "said": {"path": ["tool", "ears", "listen"], "result": "refused: /listen: wants boolean, got string"}}
    assert desk.said({"x": 1}, ["tool", "ears", "listen"], "ok") == {"x": 1}


def test_a_dot_schema_wins_and_a_dir_slash_name_id_pairs_by_name():
    fresh()
    shutil.copy(EARS / "ears.schema.json", EARS / ".ears.schema.json")
    assert schemas.schema_path("ears").name == ".ears.schema.json"
    assert schemas.tool_state("ears")["schema_file"] == "ears.schema.json"
    (EARS / ".ears.schema.json").unlink()
    assert schemas.schema_path("ears").name == "ears.schema.json"   # the old name, until the install moves
    (TMP / "notifications").mkdir(exist_ok=True)
    (TMP / "notifications" / ".global.schema.json").write_text(json.dumps(
        {"type": "object", "properties": {"allow": {"type": "boolean"}}, "additionalProperties": False}))
    (TMP / "notifications" / "global.json").write_text('{"allow": true}')
    old = schemas.DRAWN
    schemas.DRAWN = ("ears", "notifications/global", "../etc")
    try:
        assert schemas.registry() == ["ears", "notifications/global"]
        assert schemas.file_path("notifications/global") == TMP / "notifications" / "global.json"
        assert desk.apply(["tool", "notifications/global", "allow"], False) == "ok"
        assert json.loads((TMP / "notifications" / "global.json").read_text()) == {"allow": False}
        assert desk.apply(["tool", "../etc", "x"], 1) == "refused: no tool '../etc'"
    finally:
        schemas.DRAWN = old


MAP = {"type": "object", "properties": {"sounds": {"type": "object",
       "properties": {"turn": {"type": "object", "properties": {"on": {"type": "boolean"}}}},
       "additionalProperties": {"type": "object", "properties": {"on": {"type": "boolean"}},
                                "additionalProperties": False}},
       "out": {"type": "string", "x-widget": "audio-output"},
       "to": {"type": "array", "items": {"type": "string"}, "x-widget": "audio-output"}}}


def test_a_maps_entries_are_settings_through_additional_properties():
    assert schemas.prop(MAP, ["sounds", "turn", "on"]) == {"type": "boolean"}
    assert schemas.prop(MAP, ["sounds", "event:x", "on"]) == {"type": "boolean"}   # a kind not named
    assert schemas.prop(MAP, ["sounds", "event:x", "loud"]) is None
    assert schemas.prop(MAP, ["nope"]) is None


def test_apply_checks_a_live_output_but_not_a_preference_list():
    shutil.rmtree(TMP / "snd", ignore_errors=True)
    (TMP / "snd").mkdir()
    (TMP / "snd" / "snd.schema.json").write_text(json.dumps(MAP))
    (TMP / "snd" / "snd.json").write_text(json.dumps({"sounds": {"x": {"on": True}}}))
    old = schemas.DRAWN
    schemas.DRAWN = ("ears", "snd")
    try:
        assert desk.apply(["tool", "snd", "out"], "airpods") == "ok"
        assert desk.apply(["tool", "snd", "out"], "Nope") == "refused: no output 'Nope'"
        assert desk.apply(["tool", "snd", "to"], ["Gone", "airpods"]) == "ok"   # first present wins
        assert desk.apply(["tool", "snd", "sounds", "x", "on"], False) == "ok"
        assert json.loads((TMP / "snd" / "snd.json").read_text())["sounds"]["x"] == {"on": False}
        assert desk.apply(["tool", "snd", "sounds", "x", "on"], "no").startswith("refused: /sounds/x/on")
    finally:
        schemas.DRAWN = old


if __name__ == "__main__":
    install(setattr)
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_")]
    for name, f in tests:
        f()
        print("ok  ", name)
    print(f"{len(tests)} passed ({TMP})")
