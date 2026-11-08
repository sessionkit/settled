"""The consent page's live values:
who can hear the room comes from the heard box's subscriptions, named by
session list; the ear-sounds row is a toggle only when the sounds service is loaded; the
page carries the measured true sentences and none of the false ones."""
import json
import pytest
from settled import desk

ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent
PAGE = (ROOT / "app/index.html").read_text()

SUBS = """example/heard      project box
└── +airpods       claimed: only these get +airpods, not the subscribers above
    ├── 0c51b525   session subscription 00:00 Sun, listening
    ├── 85df9f17   session subscription 00:00 Sun, listening
    └── f2c76ef3   session subscription 00:00 Sun, listening
-- 0 hear it; 3 more on 1 claimed tag; no live listener on it
"""
SESSIONS = json.dumps([
    {"short": "0c51b525", "name": "home-b7", "seat": "kim"},
    {"short": "85df9f17", "name": "ada-6e", "seat": "ada"},
    {"short": "9c007d8e", "name": "bo-f8", "seat": "bo"},           # not subscribed
    {"short": "f2c76ef3", "name": "alex-ec", "seat": ""},         # no seat: the name's stem
])


def canned(answers):
    def run(*cmd):
        return answers.get(cmd[0], "")
    return run


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(desk, "_HEARS", (0.0, []))


def test_hears_names_the_seats_whose_sessions_are_subscribed(monkeypatch):
    monkeypatch.setattr(desk, "run", canned({"message": SUBS, "session": SESSIONS}))
    assert desk.hears() == {"names": ["kim", "ada", "alex"], "more": 0, "by": {"+airpods": ["kim", "ada", "alex"]}}


def test_hears_tells_the_rooms_subscribers_from_a_tags(monkeypatch):
    """The plain document: who gets every device's turns (the box) and who gets one device's (a tag)."""
    tree = SUBS.replace("└── +airpods", "├── bo         seat subscription 00:00 Sun, listening\n└── +airpods")
    monkeypatch.setattr(desk, "run", canned({"message": tree, "session": SESSIONS}))
    h = desk.hears()
    assert h["by"] == {"room": ["bo"], "+airpods": ["kim", "ada", "alex"]}
    assert h["names"] == ["bo", "kim", "ada", "alex"]


def test_hears_counts_the_sessions_this_host_cannot_name(monkeypatch):
    """A subscribed session session list can't name (another
    host's, or ended) is counted, never dropped: the page never says fewer than the truth."""
    other = SUBS.replace("└── f2c76ef3", "├── f2c76ef3") + "    └── deadbeef   session subscription 00:00 Sun, listening\n"
    monkeypatch.setattr(desk, "run", canned({"message": other, "session": SESSIONS}))
    h = desk.hears()
    assert (h["names"], h["more"]) == (["kim", "ada", "alex"], 1)
    assert h["by"]["+airpods"][-1] == "deadbeef"   # unnamed: its short id, in the document only


def test_hears_is_empty_on_a_mac_with_no_heard_box(monkeypatch):
    monkeypatch.setattr(desk, "run", canned({}))
    assert desk.hears() == {"names": [], "more": 0, "by": {}}


def test_hears_is_kept_between_polls(monkeypatch):
    monkeypatch.setattr(desk, "run", canned({"message": SUBS, "session": SESSIONS}))
    assert desk.hears()["names"] == ["kim", "ada", "alex"]
    monkeypatch.setattr(desk, "run", canned({}))
    assert desk.hears()["names"] == ["kim", "ada", "alex"]   # the 30 s answer, not a third command


def test_sounds_service_is_what_launchctl_says(monkeypatch):
    monkeypatch.setattr(desk, "run", canned({}))
    assert desk.sounds_service() is False
    monkeypatch.setattr(desk, "run", canned({"launchctl": "gui/501/dev.sessionkit.voice.ears-sounds = {\n\tstate = running"}))
    assert desk.sounds_service() is True


def test_sounds_service_knows_the_hand_rendered_labels_too(monkeypatch):
    """A host still on dev.sessionvoice.* or dev.voicemode.*
    plays sounds; "Not installed" there would be false."""
    for label in ("dev.sessionvoice.ears-sounds", "dev.voicemode.ears-sounds"):
        monkeypatch.setattr(desk, "run", lambda *cmd, l=label: "state = running" if cmd[-1].endswith(l) else "")
        assert desk.sounds_service() is True, label
    monkeypatch.setattr(desk, "run", lambda *cmd: "state = running" if cmd[-1].endswith("dev.other.ears-sounds") else "")
    assert desk.sounds_service() is False


def test_state_carries_who_hears_and_the_sounds_service(monkeypatch):
    monkeypatch.setattr(desk, "capture", lambda: {"up": False, "device": ""})
    monkeypatch.setattr(desk, "devices", lambda kind: [])
    monkeypatch.setattr(desk, "mouth_voices", lambda: {})
    monkeypatch.setattr(desk, "tools", lambda ears, cap: {})
    monkeypatch.setattr(desk, "save", lambda p, d: None)
    monkeypatch.setattr(desk, "event", lambda *a, **k: None)
    monkeypatch.setattr(desk, "run", canned({"message": SUBS, "session": SESSIONS, "launchctl": "state = running"}))
    s = desk.state()
    assert s["hears"]["names"] == ["kim", "ada", "alex"] and s["hears"]["more"] == 0
    assert s["ears"]["sounds_service"] is True


def test_the_page_says_the_true_sentences_and_none_of_the_false():
    for true in ("Off until you turn it on.",
                 "On a phone or FaceTime call the ears keep listening. Turn them off.",
                 "Your words are kept, in the log and in mail.",
                 "Each caller is tied to a key.", "Only people you approve",
                 "On: a tick each time your words are caught.", "Not installed",
                 "Turn the ears on", "the plain document",
                 "Your voice stays here; your words go to the agents who can hear.",
                 "Your voice goes there; your words go to the agents who can hear."):
        assert true in PAGE, true
    for false in ("until you delete them", "a sound plays whenever", "asks to join",
                  "the room can't see", "never on this phone"):
        assert false not in PAGE, false
    assert "127\\.0\\.0\\.1|localhost|\\[::1\\]|::1" in PAGE   # local = a loopback host only
    assert 'names.slice(0, 4)' in PAGE and '" more"' in PAGE          # up to four names, then "and N more"
    assert "No agent is listening yet" in PAGE


def test_the_plain_document_is_a_screen_the_consent_foot_opens():
    """The screen renders the kit's ONE file (docs/what-listens.md) with the live values in
    it, then the live rows; a Mac without the file says where it would be."""
    assert 'data-go="doc">What listens, who can hear, and when' in PAGE
    assert "    doc: function () {" in PAGE and 'fetch("docs/what-listens.md"' in PAGE
    for tok in ("microphone", "engine", "names", "kept"):
        assert tok + ":" in PAGE.split("var live = {")[1].split("};")[0], tok   # the four live values
    assert "Not installed on " in PAGE and "docs/what-listens.md in the sessionkit repo" in PAGE
    for line in ("ears recording off", "message subscription remove heard+"):
        assert line in PAGE, line
    assert "var h = esc(text)" in PAGE   # the file is escaped before it is rendered
    assert "ev.record_notice !== false" in PAGE and "ev.record_spoken_notice !== false" in PAGE   # missing = on
    assert "unless the capture was started with EARS_RECORD set" in PAGE
