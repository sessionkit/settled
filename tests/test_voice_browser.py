"""Offline tests for the voice browser's paths in desk.py (voices.*).

Never touches the real voices folder or the real prefs: both come from env vars
pointed at a temp dir BEFORE desk is imported. `mouth voices`, `mouth say`
and `mouth play` are stubbed. Run: python3 tests/test_voice_browser.py
(or pytest).

The fakes are installed per test and undone after it. This module once
set `desk.subprocess.run = fake_run` at IMPORT: `desk.subprocess` is the shared
subprocess module, so every test collected after it ran against the fake, and a
red could pass. Now `install()` swaps desk's own `subprocess` for a namespace
whose `run` is the fake, by monkeypatch, so nothing outside desk ever sees it
and nothing outlives the test. tests/conftest.py fails any test that leaves a
fake behind; tests/test_guards.py fails if a module leaves one at import.
"""
import hashlib, json, os, subprocess, sys, tempfile, threading
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="settings-voices-"))
VOICES = TMP / "voices"
os.environ["SETTINGS_VOICES_DIR"] = str(VOICES)
os.environ["SETTINGS_VOICE_PREFS"] = str(TMP / "voice-prefs.json")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from settled import desk  # noqa: E402

CALLS = []
SYNTH = []    # every payload the stubbed clone server got
GATE = None   # a threading.Event the stub waits on, when a test holds the worker


def fake_post(payload):
    SYNTH.append(payload)
    if GATE is not None:
        GATE.wait(5)
    return b"ID3 fake mp3: " + payload["input"].encode()


class Ran:
    returncode = 0
    stdout = stderr = ""


def fake_run(cmd, **kw):
    CALLS.append([str(c) for c in cmd])
    return Ran()


class FakeSubprocess:
    """desk's view of subprocess while a test runs: `run` is the fake, the rest
    (TimeoutExpired, ...) the real module's. The shared module is never touched."""
    run = staticmethod(fake_run)

    def __getattr__(self, name):
        return getattr(subprocess, name)


def fake_mouth_voices():
    return {"kokoro": ["af_sky", "am_santa"],
            "clone": ["monkey/pigsy", "monkey/sandy", "the-big-bang-theory/penny", "odawg"]}


def install(set_):
    """Point desk at this module's temp dir and fakes. `set_` is
    monkeypatch.setattr under pytest (undone after each test), plain setattr
    when this file runs as a script. desk binds these paths at import, and
    another module may have imported it first."""
    set_(desk, "VOICES_DIR", VOICES)
    set_(desk, "PREFS", TMP / "voice-prefs.json")
    set_(desk, "MOUTH", TMP / "mouth.json")    # never the live mouth.json
    set_(desk, "LOG", TMP / "apply.jsonl")     # nor the live apply log (the audition worker logs each clip)
    set_(desk, "tts_post", fake_post)          # the audition never reaches the network
    set_(desk, "subprocess", FakeSubprocess())  # `mouth say` and `mouth play` never run
    set_(desk, "mouth_voices", fake_mouth_voices)


try:
    import pytest
except ImportError:  # run as a script without pytest: __main__ installs the fakes
    pytest = None

if pytest is not None:
    @pytest.fixture(autouse=True)
    def fakes(monkeypatch):
        install(monkeypatch.setattr)
        yield
        desk.audition_drain()  # the audition worker never outlives the fakes


def voice(name, samples, default, real=False, txt=True, default_txt="link"):
    d = VOICES / name
    d.mkdir(parents=True, exist_ok=True)
    for s in samples:
        (d / f"{s}.wav").write_bytes(b"RIFF")
        if txt:
            (d / f"{s}.txt").write_text(f"words of {s}\n")
    if real:
        (d / "default.wav").write_bytes(b"RIFF real")
    else:
        os.symlink(f"{default}.wav", d / "default.wav")
    if default_txt == "link" and txt:
        os.symlink(f"{default}.txt", d / "default.txt")
    elif default_txt == "real":
        (d / "default.txt").write_text("a real transcript\n")
    (d / ".cast").mkdir()
    (d / ".cast" / "candidate.wav").write_bytes(b"RIFF")
    return d


voice("monkey/pigsy", ["rude-to-a-king", "true-greatness"], "true-greatness")
voice("monkey/sandy", ["one", "two"], "one", default_txt="real")
voice("the-big-bang-theory/penny", ["a", "b"], "a", real=True)
voice("odawg", ["odawg", "bonus"], "odawg", txt=False, default_txt=None)
(VOICES / "odawg" / "bonus.txt").write_text("bonus words\n")  # a transcript for bonus only


def prefs():
    return json.loads((TMP / "voice-prefs.json").read_text())


def test_state_lists_samples_not_dot_dirs():
    s = desk.samples(desk.mouth_voices())
    assert s["monkey/sandy"] == {"default": "one", "samples": ["one", "two"]}  # sandy never moves
    assert s["monkey/pigsy"]["samples"] == ["rude-to-a-king", "true-greatness"]
    assert s["the-big-bang-theory/penny"]["default"] is None  # a real file
    assert "candidate" not in json.dumps(s)


def test_families():
    f = desk.families(desk.mouth_voices())
    assert f["clone"]["monkey"] == ["monkey/pigsy", "monkey/sandy"]
    assert f["kokoro"]["af"] == ["af_sky"] and "odawg" not in json.dumps(f["clone"])


def test_heart_on_off_and_unknown():
    assert desk.apply(["voices", "heart"], {"voice": "monkey/pigsy", "on": True}) == "ok"
    assert desk.apply(["voices", "heart"], {"voice": "af_sky", "on": True}) == "ok"
    assert prefs()["hearts"] == ["monkey/pigsy", "af_sky"]
    assert desk.apply(["voices", "heart"], {"voice": "monkey/pigsy", "on": False}) == "ok"
    assert prefs()["hearts"] == ["af_sky"]
    assert desk.apply(["voices", "heart"], {"voice": "nobody", "on": True}).startswith("refused")


def test_star_one_per_family():
    assert desk.apply(["voices", "star"], {"family": "monkey", "voice": "monkey/pigsy"}) == "ok"
    assert desk.apply(["voices", "star"], {"family": "monkey", "voice": "monkey/sandy"}) == "ok"
    assert prefs()["stars"] == {"monkey": "monkey/sandy"}
    assert desk.apply(["voices", "star"], {"family": "af", "voice": "monkey/pigsy"}).startswith("refused")
    assert desk.apply(["voices", "star"], {"family": "monkey", "voice": None}) == "ok"
    assert prefs()["stars"] == {}


def test_phrase_and_opt_in_add():
    assert desk.apply(["voices", "phrase"], {"x": 1}).startswith("refused")
    assert desk.apply(["voices", "phrase"], "  Ho   ho  ho ") == "ok"
    assert prefs()["phrase"] == "Ho ho ho"
    assert "Ho ho ho" not in desk.prefs()["phrases"]  # trying is not keeping
    assert desk.apply(["voices", "phrases", "add"], "Ho ho ho") == "ok"
    assert desk.apply(["voices", "phrases", "add"], "Ho ho ho") == "ok"
    assert desk.prefs()["phrases"].count("Ho ho ho") == 1
    assert desk.apply(["voices", "phrase"], "x" * 500) == "ok" and len(prefs()["phrase"]) == desk.PHRASE_MAX


def test_say_is_an_action_with_the_phrase():
    desk.apply(["voices", "phrase"], "Hi there")
    CALLS.clear()
    assert desk.apply(["voices", "say"], {"voice": "am_santa"}) == "ok"
    cmd = CALLS[-1]
    assert cmd[1] == "say" and cmd[cmd.index("--voice") + 1] == "am_santa" and cmd[-1] == "Hi there"
    assert "--hold" not in cmd and "--interest" in cmd   # a tap plays now
    assert desk.apply(["voices", "say"], {"voice": "../etc"}).startswith("refused")


def test_play_validates_and_blocks_traversal():
    CALLS.clear()
    assert desk.apply(["voices", "play"], {"voice": "monkey/pigsy", "sample": "rude-to-a-king"}) == "ok"
    assert CALLS[-1][1] == "play" and CALLS[-1][-1].endswith("monkey/pigsy/rude-to-a-king.wav")
    n = len(CALLS)
    for bad in ("../sandy/one", "default", "candidate", ".cast/candidate", "nope", None):
        assert desk.apply(["voices", "play"], {"voice": "monkey/pigsy", "sample": bad}).startswith("refused")
    assert desk.apply(["voices", "play"], {"voice": "af_sky", "sample": "x"}).startswith("refused")  # kokoro: no folder
    assert desk.apply(["voices", "play"], {"voice": "../x", "sample": "a"}).startswith("refused")
    assert len(CALLS) == n  # nothing refused ever ran


def test_sample_repoints_both_links_relative():
    d = VOICES / "monkey/pigsy"
    assert desk.apply(["voices", "sample"], {"voice": "monkey/pigsy", "sample": "rude-to-a-king"}) == "ok"
    assert os.readlink(d / "default.wav") == "rude-to-a-king.wav"
    assert os.readlink(d / "default.txt") == "rude-to-a-king.txt"
    assert not list(d.glob(".default*"))  # no tmp link left behind
    assert desk.samples(desk.mouth_voices())["monkey/pigsy"]["default"] == "rude-to-a-king"


def test_sample_refusals_move_nothing():
    penny = VOICES / "the-big-bang-theory/penny"
    r = desk.apply(["voices", "sample"], {"voice": "the-big-bang-theory/penny", "sample": "b"})
    assert r.startswith("refused") and "real file" in r and not (penny / "default.wav").is_symlink()
    sandy = VOICES / "monkey/sandy"
    r = desk.apply(["voices", "sample"], {"voice": "monkey/sandy", "sample": "two"})
    assert r.startswith("refused") and os.readlink(sandy / "default.wav") == "one.wav"  # default.txt is real
    odawg = VOICES / "odawg"
    assert desk.apply(["voices", "sample"], {"voice": "odawg", "sample": "bonus"}) == "ok"  # no default.txt: fine
    assert os.readlink(odawg / "default.wav") == "bonus.wav" and os.readlink(odawg / "default.txt") == "bonus.txt"
    (odawg / "bonus.txt").unlink()
    r = desk.apply(["voices", "sample"], {"voice": "odawg", "sample": "odawg"})  # odawg has no .txt
    assert r.startswith("refused") and "transcript" in r and os.readlink(odawg / "default.wav") == "bonus.wav"
    (odawg / "bonus.txt").write_text("bonus words\n")  # put the shared fixture back: the audition tests below need it
    assert desk.apply(["voices", "sample"], {"voice": "monkey/pigsy", "sample": "../sandy/one"}).startswith("refused")


def test_refheart_several_on_off():
    rh = lambda v: desk.apply(["voices", "refheart"], v)  # noqa: E731
    link = os.readlink(VOICES / "monkey/pigsy" / "default.wav")
    assert rh({"voice": "monkey/pigsy", "ref": "rude-to-a-king", "on": True}) == "ok"
    assert rh({"voice": "monkey/pigsy", "ref": "true-greatness", "on": True}) == "ok"
    assert rh({"voice": "monkey/pigsy", "ref": "true-greatness", "on": True}) == "ok"  # twice: still once
    assert rh({"voice": "odawg", "ref": "bonus", "on": True}) == "ok"
    assert prefs()["refhearts"] == {"monkey/pigsy": ["rude-to-a-king", "true-greatness"], "odawg": ["bonus"]}
    assert desk.prefs()["refhearts"] == prefs()["refhearts"]  # the state the app reads
    assert rh({"voice": "monkey/pigsy", "ref": "rude-to-a-king", "on": False}) == "ok"
    assert rh({"voice": "odawg", "ref": "bonus", "on": False}) == "ok"
    assert prefs()["refhearts"] == {"monkey/pigsy": ["true-greatness"]}  # an empty voice is dropped
    assert os.readlink(VOICES / "monkey/pigsy" / "default.wav") == link  # a heart never moves the star


def test_refheart_refusals_leave_prefs_untouched():
    rh = lambda v: desk.apply(["voices", "refheart"], v)  # noqa: E731
    before = (TMP / "voice-prefs.json").read_bytes() if (TMP / "voice-prefs.json").exists() else None
    for bad in ({"voice": "nobody", "ref": "a", "on": True},              # not a voice
                {"voice": "af_sky", "ref": "a", "on": True},              # kokoro: no folder
                {"voice": "../x", "ref": "a", "on": True},
                {"voice": "monkey/pigsy", "ref": "nope", "on": True},     # not a reference
                {"voice": "monkey/pigsy", "ref": "default", "on": True},  # the pointer, not a clip
                {"voice": "monkey/pigsy", "ref": "candidate", "on": True},  # dot-folder
                {"voice": "monkey/pigsy", "ref": "../sandy/one", "on": True},
                {"voice": "monkey/pigsy", "ref": None, "on": True}):
        assert rh(bad).startswith("refused"), bad
    assert rh({"voice": "monkey/pigsy", "ref": "true-greatness", "on": "yes"}) == "refused: path not allowed"
    assert rh("monkey/pigsy") == "refused: path not allowed"
    after = (TMP / "voice-prefs.json").read_bytes() if (TMP / "voice-prefs.json").exists() else None
    assert after == before


def test_hide_on_off_and_state():
    hd = lambda v: desk.apply(["voices", "hide"], v)  # noqa: E731
    hearts = prefs().get("hearts") if (TMP / "voice-prefs.json").exists() else None
    assert hd({"voice": "monkey/sandy", "on": True}) == "ok"
    assert hd({"voice": "am_santa", "on": True}) == "ok"        # a Kokoro voice can hide too
    assert hd({"voice": "am_santa", "on": True}) == "ok"        # twice: still once
    assert prefs()["hidden"] == ["monkey/sandy", "am_santa"]
    assert desk.prefs()["hidden"] == prefs()["hidden"]          # the state the app reads
    assert hd({"voice": "monkey/sandy", "on": False}) == "ok"
    assert hd({"voice": "odawg", "on": False}) == "ok"          # unhiding a shown voice: a no-op
    assert prefs()["hidden"] == ["am_santa"]
    assert prefs().get("hearts") == hearts                      # hiding never touches a heart
    assert hd({"voice": "am_santa", "on": False}) == "ok"
    assert prefs()["hidden"] == []


def test_hide_refusals_leave_prefs_untouched():
    hd = lambda v: desk.apply(["voices", "hide"], v)  # noqa: E731
    before = (TMP / "voice-prefs.json").read_bytes() if (TMP / "voice-prefs.json").exists() else None
    for bad in ({"voice": "nobody", "on": True}, {"voice": "../x", "on": True},
                {"voice": "monkey", "on": True},                 # a family, not a voice
                {"voice": None, "on": True}, {"voice": ["af_sky"], "on": True}):
        assert hd(bad).startswith("refused"), bad
    assert hd({"voice": "af_sky", "on": "yes"}) == "refused: path not allowed"
    assert hd({"voice": "af_sky"}) == "refused: path not allowed"
    assert hd("af_sky") == "refused: path not allowed"
    after = (TMP / "voice-prefs.json").read_bytes() if (TMP / "voice-prefs.json").exists() else None
    assert after == before


def pick(v, text):
    assert desk.apply(["voices", "line"], {"voice": v, "text": text}) == "ok"


def aud(v, ref):
    return desk.apply(["voices", "audition"], {"voice": v, "ref": ref})


def test_audition_makes_once_then_plays_cached():
    pick("monkey/pigsy", "Oink oink")
    SYNTH.clear(); CALLS.clear()
    assert aud("monkey/pigsy", "rude-to-a-king") == "ok: making it"
    desk.audition_drain()
    out = desk.clip_path("monkey/pigsy", "rude-to-a-king", "Oink oink")
    assert out.parent == VOICES / "monkey/pigsy" / ".auditions" and out.is_file()
    assert out.name == f"rude-to-a-king.{desk.line_hash('Oink oink')}.mp3"
    assert out.with_suffix(".txt").read_text() == "Oink oink\n"      # the sidecar holds the line
    assert len(SYNTH) == 1
    p = SYNTH[0]
    assert p["input"] == "Oink oink" and p["ref_text"] == "words of rude-to-a-king"
    assert p["ref_audio"] == str(VOICES / "monkey/pigsy" / "rude-to-a-king.wav")    # that ONE clip
    assert p["model"] == desk.CLONE_MODEL and p["response_format"] == "mp3" and p["lang_code"] == "auto"
    assert CALLS[-1][1] == "play" and CALLS[-1][-1] == str(out)     # played once it landed
    assert not [f for f in out.parent.iterdir() if "tmp" in f.name]  # atomic: no tmp left behind
    assert '"audition": "monkey/pigsy/rude-to-a-king"' in desk.LOG.read_text()
    CALLS.clear()
    assert aud("monkey/pigsy", "rude-to-a-king") == "ok"            # made: just play it
    assert len(SYNTH) == 1 and CALLS[-1][-1] == str(out)
    s = desk.samples(desk.mouth_voices())["monkey/pigsy"]
    assert s["made"] == ["rude-to-a-king"] and "making" not in s and "batch" not in s


def test_audition_hash_follows_the_line_and_model():
    h = desk.line_hash("Oink oink")
    assert len(h) == 10 and h == hashlib.sha256((desk.CLONE_MODEL + "\nOink oink").encode()).hexdigest()[:10]
    assert desk.line_hash("Oink oink!") != h
    old = desk.CLONE_MODEL
    desk.CLONE_MODEL = "another/model"
    try:
        assert desk.line_hash("Oink oink") != h
    finally:
        desk.CLONE_MODEL = old
    SYNTH.clear()
    for line in ("First hash line", "Second hash line"):
        pick("monkey/pigsy", line)
        assert "true-greatness" not in desk.samples(desk.mouth_voices())["monkey/pigsy"].get("made", [])
        assert aud("monkey/pigsy", "true-greatness") == "ok: making it"
        desk.audition_drain()
    a, b = (desk.clip_path("monkey/pigsy", "true-greatness", t) for t in ("First hash line", "Second hash line"))
    assert a != b and a.is_file() and b.is_file() and len(SYNTH) == 2  # a new line: a new clip, the old kept


def test_audition_refusals_never_synthesise():
    SYNTH.clear(); CALLS.clear()
    r = aud("odawg", "odawg")                                        # no odawg.txt
    assert r.startswith("refused") and "transcript" in r
    for v, ref in (("af_sky", "x"), ("nobody", "a"), ("../x", "a"), ("monkey/pigsy", "default"),
                   ("monkey/pigsy", "../sandy/one"), ("monkey/pigsy", "candidate"), ("monkey/pigsy", None)):
        assert aud(v, ref).startswith("refused"), (v, ref)
    assert desk.apply(["voices", "audition", "all"], {"voice": "af_sky"}).startswith("refused")
    assert desk.apply(["voices", "audition"], "monkey/pigsy") == "refused: path not allowed"
    desk.audition_drain()
    assert SYNTH == [] and CALLS == []                               # never another voice instead
    assert desk.samples(desk.mouth_voices())["odawg"]["notext"] == ["odawg"]


def test_audition_server_failure_leaves_nothing_and_can_retry():
    def boom(payload):
        raise OSError("connection refused")
    pick("monkey/pigsy", "Broken line")
    desk.tts_post = boom
    try:
        assert aud("monkey/pigsy", "rude-to-a-king") == "ok: making it"
        desk.audition_drain()
    finally:
        desk.tts_post = fake_post
    out = desk.clip_path("monkey/pigsy", "rude-to-a-king", "Broken line")
    assert not out.exists() and not out.with_suffix(".txt").exists()
    s = desk.samples(desk.mouth_voices())["monkey/pigsy"]
    assert "rude-to-a-king" not in s.get("made", []) and "making" not in s
    assert "connection refused" in desk.LOG.read_text()
    assert aud("monkey/pigsy", "rude-to-a-king") == "ok: making it"  # not stuck: a tap retries
    desk.audition_drain()
    assert out.is_file()


def test_audition_all_one_batch_at_a_time_single_tap_first():
    global GATE
    pick("monkey/pigsy", "All of them")
    SYNTH.clear(); CALLS.clear()
    GATE = threading.Event()
    try:
        assert desk.apply(["voices", "audition", "all"], {"voice": "monkey/pigsy"}) == "ok: making 2"
        assert desk.apply(["voices", "audition", "all"], {"voice": "monkey/pigsy"}) == "ok: already making them"
        assert aud("monkey/pigsy", "true-greatness") == "ok: making it"         # ahead of the batch's copy
        assert aud("monkey/pigsy", "true-greatness") == "ok: already making it"
        s = desk.samples(desk.mouth_voices())["monkey/pigsy"]
        assert s["batch"] is True and sorted(s["making"]) == ["rude-to-a-king", "true-greatness"]
    finally:
        GATE.set()
    desk.audition_drain()
    GATE = None
    assert sorted(p["ref_audio"].rsplit("/", 1)[1] for p in SYNTH) == ["rude-to-a-king.wav", "true-greatness.wav"]
    assert [c[-1] for c in CALLS if c[1] == "play"] == [str(desk.clip_path("monkey/pigsy", "true-greatness", "All of them"))]
    s = desk.samples(desk.mouth_voices())["monkey/pigsy"]
    assert s["made"] == ["rude-to-a-king", "true-greatness"] and "batch" not in s and "making" not in s
    assert desk.apply(["voices", "audition", "all"], {"voice": "monkey/pigsy"}) == "ok: all made"
    assert desk.apply(["voices", "audition", "all"], {"voice": "odawg"}) == "ok: making 1"  # bonus; odawg has no .txt
    desk.audition_drain()
    assert len(SYNTH) == 3


def test_lines_three_levels_sticky_and_lock():
    lf, v = desk.line_for, "monkey/sandy"
    desk.apply(["voices", "phrase"], "Global hello")
    assert lf(v) == "Global hello" and lf(v, False) == "Global hello"
    pick(v, "  Sandy   here ")
    assert lf(v) == "Sandy here" and lf(v, False) == "Global hello"   # the list says the phrase till she's locked
    assert desk.apply(["voices", "lock"], {"voice": v, "on": True}) == "ok"
    assert prefs()["locked"][v] == "Sandy here" and lf(v, False) == "Sandy here"
    pick(v, "Just trying")
    assert lf(v) == "Just trying" and lf(v, False) == "Sandy here"   # trying never moves the lock
    pick("monkey/pigsy", "Pigsy's own")
    assert lf(v) == "Just trying"                                    # sticky per voice
    assert desk.apply(["voices", "lock"], {"voice": v, "on": False}) == "ok"
    assert v not in prefs()["locked"] and lf(v, False) == "Global hello" and lf(v) == "Just trying"
    p = {"phrase": "G", "locked": {v: "L"}, "current": {}}
    assert lf(v, True, p) == "L" and lf("odawg", True, p) == "G"     # no current: locked, else global
    add = lambda lvl, t, who=v: desk.apply(["voices", "lines", "add"], {"voice": who, "level": lvl, "text": t})  # noqa: E731
    assert add("voice", "Mine") == "ok" and add("voice", "Mine") == "ok"
    assert add("family", "Ours") == "ok" and add("global", "Everyone's") == "ok"
    pr = desk.prefs()
    assert pr["voice_lines"][v] == ["Mine"] and pr["family_lines"]["monkey"] == ["Ours"]
    assert pr["phrases"].count("Everyone's") == 1 and pr["current"][v] == "Everyone's"  # a save also picks it
    before = (TMP / "voice-prefs.json").read_bytes()
    r = add("family", "x", "odawg")
    assert r.startswith("refused") and "no family" in r
    for bad in (add("planet", "x"), add("voice", ""), add("voice", None), add("voice", "x", "nobody"),
                desk.apply(["voices", "line"], {"voice": v, "text": "   "}),
                desk.apply(["voices", "line"], {"voice": "../x", "text": "x"}),
                desk.apply(["voices", "lock"], {"voice": v, "on": "yes"}),
                desk.apply(["voices", "lock"], {"voice": "nobody", "on": True}),
                desk.apply(["voices", "line"], "Sandy")):
        assert bad.startswith("refused"), bad
    assert (TMP / "voice-prefs.json").read_bytes() == before


def test_say_page_says_her_line_the_list_her_lock():
    desk.apply(["voices", "phrase"], "List line")
    pick("monkey/pigsy", "Page line")
    CALLS.clear()
    assert desk.apply(["voices", "say"], {"voice": "monkey/pigsy", "page": True}) == "ok"
    assert CALLS[-1][-1] == "Page line"
    assert desk.apply(["voices", "say"], {"voice": "monkey/pigsy"}) == "ok"
    assert CALLS[-1][-1] == "List line"
    desk.apply(["voices", "lock"], {"voice": "monkey/pigsy", "on": True})
    assert desk.apply(["voices", "say"], {"voice": "monkey/pigsy"}) == "ok" and CALLS[-1][-1] == "Page line"
    desk.apply(["voices", "lock"], {"voice": "monkey/pigsy", "on": False})


def test_try_it_says_the_text_and_keeps_nothing():
    before = json.loads(desk.PREFS.read_text()) if desk.PREFS.exists() else {}
    CALLS.clear()
    assert desk.apply(["voices", "say"], {"voice": "monkey/pigsy", "text": "  Just   trying  "}) == "ok"
    assert CALLS[-1][-1] == "Just trying"
    after = json.loads(desk.PREFS.read_text()) if desk.PREFS.exists() else {}
    assert after == before   # said once, saved nowhere
    assert desk.apply(["voices", "say"], {"voice": "monkey/pigsy", "text": "   "}) == "refused: no line"


def test_other_voices_paths_refused():
    assert desk.apply(["voices", "nope"], 1) == "refused: path not allowed"
    assert desk.apply(["voices"], 1) == "refused: path not allowed"


# The pencil, the outputs' hearts and test.

def test_edit_line_changes_it_where_kept_and_where_used():
    desk.apply(["voices", "lines", "add"], {"voice": "monkey/pigsy", "level": "voice", "text": "Old words"})
    desk.apply(["voices", "lock"], {"voice": "monkey/pigsy", "on": True})
    assert desk.apply(["voices", "lines", "edit"],
                       {"voice": "monkey/pigsy", "level": "voice", "old": "Old words", "text": "New  words"}) == "ok"
    p = json.loads((TMP / "voice-prefs.json").read_text())
    assert "New words" in p["voice_lines"]["monkey/pigsy"] and "Old words" not in p["voice_lines"]["monkey/pigsy"]
    assert p["current"]["monkey/pigsy"] == "New words" and p["locked"]["monkey/pigsy"] == "New words"
    desk.apply(["voices", "lock"], {"voice": "monkey/pigsy", "on": False})


def test_edit_line_refusals_touch_nothing():
    before = (TMP / "voice-prefs.json").read_bytes() if (TMP / "voice-prefs.json").exists() else None
    e = lambda v: desk.apply(["voices", "lines", "edit"], v)
    assert e({"voice": "monkey/pigsy", "level": "voice", "old": "Never said", "text": "x"}).startswith("refused: no line")
    assert e({"voice": "monkey/pigsy", "level": "nope", "old": "a", "text": "b"}).startswith("refused: no level")
    assert e({"voice": "odawg", "level": "family", "old": "a", "text": "b"}) == "refused: odawg has no family"
    assert e({"voice": "../etc", "level": "voice", "old": "a", "text": "b"}).startswith("refused: no voice")
    assert e({"voice": "monkey/pigsy", "level": "voice", "old": "a", "text": "  "}) == "refused: no line"
    after = (TMP / "voice-prefs.json").read_bytes() if (TMP / "voice-prefs.json").exists() else None
    assert after == before


def test_edit_global_line_moves_the_phrase_too():
    desk.apply(["voices", "phrase"], "Testing, testing, one two three.")
    assert desk.apply(["voices", "lines", "edit"], {"voice": "af_sky", "level": "global",
                       "old": "Testing, testing, one two three.", "text": "Testing, one two."}) == "ok"
    p = json.loads((TMP / "voice-prefs.json").read_text())
    assert "Testing, one two." in p["phrases"] and "Testing, testing, one two three." not in p["phrases"]
    assert p["phrase"] == "Testing, one two."


def test_output_heart_and_test():
    real = desk.devices
    desk.devices = lambda kind: ["AirPods", "MacBook Pro Speakers"] if kind == "output" else []
    try:
        assert desk.apply(["mouth", "heart"], {"device": "AirPods", "on": True}) == "ok"
        assert desk.prefs()["out_hearts"] == ["AirPods"]
        assert desk.apply(["mouth", "heart"], {"device": "AirPods", "on": False}) == "ok"
        assert desk.prefs()["out_hearts"] == []
        assert desk.apply(["mouth", "heart"], {"device": "Nope", "on": True}).startswith("refused: no output")
        CALLS.clear()
        assert desk.apply(["mouth", "test"], "AirPods") == "ok"
        assert CALLS[-1][CALLS[-1].index("--device") + 1] == "AirPods"
        assert desk.apply(["mouth", "test"], "default") == "ok"
        assert desk.apply(["mouth", "test"], "Nope").startswith("refused: no output")
        assert ["mouth", "test"] in desk.ACTIONS   # a test is an action: no config-change event
    finally:
        desk.devices = real


if __name__ == "__main__":
    install(setattr)
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_")]  # file order: the tests share state
    for name, f in tests:
        f()
        print("ok  ", name)
    print(f"{len(tests)} passed ({TMP})")
