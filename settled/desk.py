"""desk.py: the settings desk's core, the one set/read path every front end calls.

web.py (the browser desk) imports it: `apply()` applies one "set" (only to the
paths below), `state()` reads the whole settings back, and `lookup`, `said`,
`same`, `host`, `log` and `snapshot` serve the poll. The host stays the truth.
No chat transport lives here; a private follower may call the same functions.

Paths it accepts, and nothing else:
  global.allow                    bool  -> notifications/global.json
  ears.listen                     bool  -> ears.json   (ears-keeper reads it)
  ears.input                      "default" | an input's exact name -> ears.json
  ears.sounds.allow               bool  -> ears-sounds.json
  ears.sounds.pair                bool  -> ears-sounds.json
  ears.sounds.sounds.KIND.on      bool  (KIND must already exist there)
  ears.sounds.to                  [] | [an output's exact name]
  mouth.output                    "default" | an output's exact name -> mouth.json
  mouth.voice                     one of `mouth voices` (kokoro or clone) -> mouth.json;
                                  the mouth's default for a line that names no voice.
                                  A pick is previewed: one line in the new voice.
  The voice browser:
  voices.heart                    {voice, on}      -> voice-prefs.json hearts
  voices.star                     {family, voice}  -> voice-prefs.json stars, one per
                                  family; voice null clears it
  voices.hide                     {voice, on}      -> voice-prefs.json hidden: out of the
                                  list until "Show hidden". Any voice.
  voices.phrase                   str              -> the phrase the speaker button says
  voices.phrases.add              str              -> kept in the phrase list (opt-in)
  voices.sample                   {voice, sample}  -> repoint the clone's default.wav
                                  (and default.txt) symlinks. Refused on a real file.
                                  The star on a reference row: the default reference.
  voices.refheart                 {voice, ref, on} -> voice-prefs.json refhearts: the
                                  references to use, several per clone (star is the
                                  default). ref is checked as voices.sample checks.
  Lines at three levels, the audition:
  voices.line                     {voice, text}    -> voice-prefs.json current: the line
                                  that voice says on its own page, sticky per voice
  voices.lines.add                {voice, level, text} -> kept in that level's list, and
                                  picked as voices.line does. level "voice" (voice_lines),
                                  "family" (family_lines; refused for a voice with none) or
                                  "global" (phrases, the list voices.phrases.add keeps)
  voices.lock                     {voice, on}      -> voice-prefs.json locked: her default
                                  line = the line her page says now; off clears it
  voices.say                      {voice, page?}   ACTION: say her line. page true = the
                                  line her page shows (current, else locked, else the
                                  phrase); else the list's (locked, else the phrase)
  voices.play                     {voice, sample}  ACTION: play that sample's wav
  voices.audition                 {voice, ref}     ACTION: her page's line, cloned from
                                  that ONE reference, made once into <voice>/.auditions/
                                  <ref>.<hash>.mp3 (+ .txt: the line) and then played. The
                                  hash is the model and the line. Refused when the ref has
                                  no transcript; never another voice instead
  voices.audition.all             {voice}          ACTION: make every missing one for her
                                  page's line, in the background, one at a time; a second
                                  tap while it runs starts nothing
  Actions are not stored and write no config-change event.
  Drawn from a schema (schemas.py):
  tool.TOOL.KEY...                any value -> TOOL's settings file (where schemas.py
                                  says), when TOOL is drawn and the file still validates
                                  against its schema afterwards; every other key kept.
                                  An x-widget "audio-input" also wants a live input.
                                  The state carries tools.TOOL = {schema, value, ...}.

Where it writes (one home: a fresh install makes only ~/.sessionkit): its own
files under paths.settled_home() (the log, voice-prefs.json, the state
snapshot, and the clone voices unless SETTINGS_VOICES_DIR or
VOICEMODE_VOICES_DIR names theirs); each tool's settings file where
schemas.py says that tool reads it. Every tap is logged: apply.jsonl, one
line per tap or push, with the result.
"""
import hashlib, itertools, json, os, queue, re, socket, subprocess, sys, threading, time
import urllib.request
from pathlib import Path

from settled import paths    # settled's own folder (state, logs), the one-time carry, the kit switch
from settled import schemas  # the tools drawn from a JSON Schema, and the check on each write

HOME = Path.home()
# Each tool's settings file is where ITS reader reads it; schemas.py says where: the kit's
# registry through sessiontools when the kit is installed (one flat ~/.sessionkit/voice/settings/
# for ears, mouth and the keeper's sounds), else the standalone root, SETTINGS_ROOT/<thing>/
# (schemas.ROOT).
GLOBAL = schemas.file_path("notifications/global")
SOUNDS = schemas.file_path("ears-sounds" if schemas.KIT else "ears/ears-sounds")  # the keeper's sounds
EARS = schemas.file_path("ears")    # the keeper reads it every loop
MOUTH = schemas.file_path("mouth")  # the mouth reads it on the next line
# settled's own files: one folder, paths.settled_home() (settled/cli.py logs there too).
LOG = paths.settled_home() / "log" / "apply.jsonl"
MOUTH_BIN = HOME / ".local/bin/mouth"  # the live mouth; `mouth voices` lists what it speaks
VOICES_TTL = 300.0
# The voice browser's data. The clone voices are the mouth's: SETTINGS_VOICES_DIR, else the
# mouth's own VOICEMODE_VOICES_DIR, else settled's folder, so a fresh install writes nothing
# outside ~/.sessionkit (one home). A Mac whose clones sit elsewhere names them by either env
# var. Env vars too so a test never touches the real ones.
VOICES_DIR = Path(os.environ.get("SETTINGS_VOICES_DIR") or os.environ.get("VOICEMODE_VOICES_DIR")
                  or paths.settled_home() / "voices").expanduser()
PREFS = Path(os.environ.get("SETTINGS_VOICE_PREFS") or paths.settled_home() / "voice-prefs.json").expanduser()
PHRASES = [
    "Hello! This is how I sound.",
    "I have a cunning plan.",
    "Right, who's putting the kettle on?",
    "The magpie is back, and he wants meat.",
    "Testing, testing, one two three.",
]
PHRASE_MAX = 200
ACTIONS = (["voices", "say"], ["voices", "play"], ["voices", "audition"], ["voices", "audition", "all"],
           ["mouth", "test"])
# The audition: the audition tool's contract with the mlx-audio server (skillbox
# voice-cloning/scripts/audition), and the model the live mouth clones with.
# .auditions/ is voice-lab's git-ignored home for generated clips.
TTS_URL = os.environ.get("SETTINGS_TTS_URL") or "http://127.0.0.1:8890/v1/audio/speech"
CLONE_MODEL = os.environ.get("SETTINGS_CLONE_MODEL") or "mlx-community/Qwen3-TTS-12Hz-1.7B-Base-bf16"
AUDITIONS = ".auditions"
KOKORO_WHO = {"af": "American woman", "am": "American man", "bf": "British woman", "bm": "British man"}
# The ears' event writer (`heard.py write event ...`), where a config change joins the
# timeline of what was heard: SETTINGS_HEARD names it. None: no event is written (127).
HEARD = Path(os.environ["SETTINGS_HEARD"]).expanduser() if os.environ.get("SETTINGS_HEARD") else None
POLL = 10.0
# The applier's last state() read, for web.py's poll to take instead of reading the devices
# again: every SwitchAudioSource is a new Core Audio client and coreaudiod pays for each
# (measured: 13 spawns per 10 s, coreaudiod at 20%).
SNAPSHOT = paths.settled_home() / "state.json"


def load(p: Path) -> dict:
    try:
        return json.loads(p.read_text())
    except FileNotFoundError:
        return {}


def save(p: Path, d: dict) -> None:
    """Write D to P whole. Its folder is made first: on a fresh install no tool has
    made it yet (notifications/, under settled's own folder, the gate's step 8)."""
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(d, indent=2) + "\n")
    tmp.replace(p)  # one step: readers never see half a file


def run(*cmd: str) -> str:
    """One command's stdout, or "" when it fails or is not installed: a Mac
    with no SwitchAudioSource (a fresh install) lists no devices, and the
    desk still serves (the fresh-install gate, step 5)."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout.strip() if r.returncode == 0 else ""


def devices(kind: str) -> list:
    """Device names of one kind ('input'|'output'), in order, duplicates dropped."""
    seen = []
    for n in run("SwitchAudioSource", "-a", "-t", kind).splitlines():
        if n and n not in seen:
            seen.append(n)
    return seen


def capture() -> dict:
    """The running listen capture: up, and on which device (from its argv)."""
    # ears.listen since the ears left voice_mode (the old name kept for an old install)
    pid = run("pgrep", "-f", r"(ears|voice_mode)\.listen capture").split("\n")[0]
    if not pid:
        return {"up": False, "device": ""}
    args = run("ps", "-ww", "-o", "args=", "-p", pid)
    m = re.search(r"--device (.+?) --", args)
    return {"up": True, "device": m.group(1) if m else ""}


_VOICES = {"t": -VOICES_TTL, "v": {}}


def mouth_voices() -> dict:
    """{kokoro: [...], clone: [...]}: what the mouth can speak, from `mouth voices`.
    Asked at most every VOICES_TTL s; a failed ask keeps the last good answer."""
    if time.monotonic() - _VOICES["t"] < VOICES_TTL and _VOICES["v"]:
        return _VOICES["v"]
    try:
        r = subprocess.run([str(MOUTH_BIN), "voices"], capture_output=True, text=True, timeout=20)
        v = json.loads(r.stdout) if r.returncode == 0 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        v = None
    if isinstance(v, dict):
        _VOICES["v"] = {"kokoro": list(v.get("kokoro") or []), "clone": list(v.get("clone") or [])}
        _VOICES["t"] = time.monotonic()
    return _VOICES["v"]


def preview(voice: str, device: str) -> int:
    """Say one line in the voice just picked, so it is heard at once, while the
    choice is fresh. Interest
    zero, so no agent wakes on his reply. It used to wait for his turn end; not now: a tap is his ask, so it plays now, over his talking if need
    be."""
    name = voice.rsplit("/", 1)[-1]
    name = (name[3:] if len(name) > 3 and name[2] == "_" else name).replace("-", " ").title()
    env = dict(os.environ, VOICEMODE_AGENT="settings")
    try:
        r = subprocess.run([str(MOUTH_BIN), "say", "--device", device, "--voice", voice,
                            "--expires", "60", "--interest", "zero",
                            f"Hi, I'm {name}. This is how the mouth sounds now."],
                           capture_output=True, text=True, timeout=15, env=env)
        return r.returncode
    except (OSError, subprocess.TimeoutExpired):
        return -1


def family_of(voice: str) -> str:
    """A clone's family is its folder (monkey/pigsy -> monkey); Kokoro's is its
    prefix (af_sky -> af). '' = on its own."""
    if "/" in voice:
        return voice.rsplit("/", 1)[0]
    k = voice[:2]
    return k if voice[2:3] == "_" and k in KOKORO_WHO else ""


def families(v: dict) -> dict:
    """{clone: {family: [voice...]}, kokoro: {prefix: [voice...]}, who: labels}."""
    out = {"clone": {}, "kokoro": {}, "who": KOKORO_WHO}
    for kind in ("clone", "kokoro"):
        for voice in v.get(kind, []):
            fam = family_of(voice)
            if fam:
                out[kind].setdefault(fam, []).append(voice)
    return out


def list_samples(d: Path) -> list:
    """The stems of the WAVs directly in a voice's folder, default.wav excluded
    (it is a pointer). Dot-folders (.cast, .samples, .notes) are never read."""
    try:
        return sorted(p.stem for p in d.glob("*.wav")
                      if p.name != "default.wav" and not p.name.startswith(".") and p.is_file())
    except OSError:
        return []


def samples(v: dict) -> dict:
    """{voice: {default: stem default.wav points at (None: a real file), samples: [...]}}
    for every clone. A scan of ~150 folders takes ~20 ms, so no cache.
    The audition adds, only when not empty: made (refs with a clip for the line
    her page says now), making (queued or being made), batch (Make them all is
    running), notext (refs with no transcript, which can't be cloned from)."""
    out, p = {}, load(PREFS)
    with _AUD["lock"]:
        making = {k for k, n in _AUD["making"].items() if n > 0}
        batch = set(_AUD["batch"])
    for voice in v.get("clone", []):
        d = VOICES_DIR / voice
        if not d.is_dir():
            continue
        dw, default = d / "default.wav", None
        if dw.is_symlink():
            default = Path(os.readlink(dw)).stem
        refs = list_samples(d)
        rec = {"default": default, "samples": refs}
        h = line_hash(line_for(voice, True, p))
        try:
            names = set(os.listdir(d / AUDITIONS))
        except OSError:
            names = set()
        made = [r for r in refs if f"{r}.{h}.mp3" in names]
        mk = [r for r in refs if (voice, r, h) in making and r not in made]
        notext = [r for r in refs if not (d / f"{r}.txt").is_file()]
        for k, val in (("made", made), ("making", mk), ("batch", voice in batch), ("notext", notext)):
            if val:
                rec[k] = val
        out[voice] = rec
    return out


def prefs() -> dict:
    p = load(PREFS)
    return {"hearts": list(p.get("hearts") or []), "stars": dict(p.get("stars") or {}),
            "refhearts": dict(p.get("refhearts") or {}), "hidden": list(p.get("hidden") or []),
            "phrase": p.get("phrase") or PHRASES[0], "phrases": list(p.get("phrases") or PHRASES),
            "voice_lines": dict(p.get("voice_lines") or {}), "family_lines": dict(p.get("family_lines") or {}),
            "current": dict(p.get("current") or {}), "locked": dict(p.get("locked") or {}),
            "out_hearts": list(p.get("out_hearts") or [])}


def clean_line(value) -> str:
    """A line as the phrase paths keep one: spaces folded, PHRASE_MAX at most; '' = none."""
    return " ".join(value.split())[:PHRASE_MAX] if isinstance(value, str) and value.strip() else ""


def line_for(voice: str, page: bool = True, p: dict | None = None) -> str:
    """The line a voice says. On her own page (page=True): the one she was last
    saying there, else her locked default, else the global phrase. From the
    list's 🔈: locked, else the phrase. Back on her page, she says her own line
    by default; locking it is how it stays."""
    p = load(PREFS) if p is None else p
    cur = (p.get("current") or {}).get(voice) if page else None
    return cur or (p.get("locked") or {}).get(voice) or p.get("phrase") or PHRASES[0]


# The audition: a page for the references, where each generated test clip can be
# played, and a clip is made only if it does not exist yet. One worker thread
# makes the clips, one at a time, so the tap loop never waits on the server and
# two clips never compete for it. A tap on one reference goes to the front of
# the queue (0); Make them all queues behind (1).
_AUD = {"lock": threading.Lock(), "q": queue.PriorityQueue(), "n": itertools.count(),
        "worker": None, "making": {}, "playing": set(), "batch": set(), "dirty": threading.Event()}


def line_hash(line: str) -> str:
    return hashlib.sha256(f"{CLONE_MODEL}\n{line}".encode()).hexdigest()[:10]


def clip_path(voice: str, ref: str, line: str) -> Path:
    return VOICES_DIR / voice / AUDITIONS / f"{ref}.{line_hash(line)}.mp3"


def ref_text(voice: str, ref: str) -> str:
    """A reference's transcript, from its .txt sidecar: the clone needs it. '' = none."""
    try:
        return (VOICES_DIR / voice / f"{ref}.txt").read_text().strip()
    except OSError:
        return ""


def tts_post(payload: dict) -> bytes:
    req = urllib.request.Request(TTS_URL, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return r.read()


def write_atomic(p: Path, data: bytes) -> None:
    tmp = p.with_name(f".{p.name}.tmp-{os.getpid()}-{threading.get_ident()}")
    tmp.write_bytes(data)
    tmp.replace(p)  # one step: a reader never sees half a clip


def synth(ref_wav: Path, text: str, line: str, out: Path) -> tuple:
    """`line`, spoken from the one clip ref_wav (transcript `text`), into out,
    as the audition tool asks the server. The .txt (the line) lands first, so
    an mp3 never sits without it. Returns (seconds, bytes)."""
    if not text:
        raise ValueError(f"{ref_wav.name} has no transcript")
    t0 = time.monotonic()
    data = tts_post({"model": CLONE_MODEL, "input": line, "ref_audio": str(ref_wav),
                     "ref_text": text, "response_format": "mp3", "lang_code": "auto"})
    if not data:
        raise RuntimeError("the server sent no audio")
    out.parent.mkdir(parents=True, exist_ok=True)
    write_atomic(out.with_suffix(".txt"), (line + "\n").encode())
    write_atomic(out, data)
    return round(time.monotonic() - t0, 2), len(data)


def play_file(path: Path, device: str) -> int:
    try:
        r = subprocess.run([str(MOUTH_BIN), "play", "--device", device, str(path)],
                           capture_output=True, text=True, timeout=15)
        return r.returncode
    except (OSError, subprocess.TimeoutExpired):
        return -1


def _worker() -> None:
    q = _AUD["q"]
    while True:
        _, _, job = q.get()
        try:
            job()
        except Exception as x:  # a bad clip must not kill the worker
            print(f"audition worker: {type(x).__name__}: {x}", flush=True)
        finally:
            q.task_done()
            _AUD["dirty"].set()  # the tap loop pushes the new state now, not at the next poll


def _enqueue(prio: int, job) -> None:
    with _AUD["lock"]:
        w = _AUD["worker"]
        if w is None or not w.is_alive():
            w = threading.Thread(target=_worker, name="audition", daemon=True)
            w.start()
            _AUD["worker"] = w
    _AUD["q"].put((prio, next(_AUD["n"]), job))


def audition_drain() -> None:
    """Block until every queued clip is made (or failed). For tests."""
    _AUD["q"].join()


def _clip_job(voice: str, ref: str, line: str, device: str | None = None):
    """A job that makes one clip if it isn't there yet, then plays it when a
    device is given. The caller has already counted it in making."""
    key = (voice, ref, line_hash(line))

    def job() -> None:
        stamp = lambda: time.strftime("%Y-%m-%dT%H:%M:%S%z")  # noqa: E731
        try:
            out = clip_path(voice, ref, line)
            if not out.is_file():
                secs, size = synth(VOICES_DIR / voice / f"{ref}.wav", ref_text(voice, ref), line, out)
                log({"ts": stamp(), "audition": f"{voice}/{ref}", "hash": key[2], "secs": secs, "bytes": size})
            if device:
                rc = play_file(out, device)
                if rc:
                    log({"ts": stamp(), "audition": f"{voice}/{ref}", "error": f"mouth play rc={rc}"})
        except Exception as x:
            log({"ts": stamp(), "audition": f"{voice}/{ref}", "hash": key[2], "error": f"{type(x).__name__}: {x}"})
        finally:
            with _AUD["lock"]:
                n = _AUD["making"].get(key, 0) - 1
                if n > 0:
                    _AUD["making"][key] = n
                else:
                    _AUD["making"].pop(key, None)
                if device:
                    _AUD["playing"].discard(key)
    return job


def audition(voice: str, ref: str, line: str, device: str) -> str:
    """The 🔈 on a reference row: play the clip if it's made, else make it at
    the front of the queue and play it when it lands."""
    out = clip_path(voice, ref, line)
    if out.is_file():
        rc = play_file(out, device)
        return "ok" if rc == 0 else f"error: mouth play rc={rc}"
    key = (voice, ref, line_hash(line))
    with _AUD["lock"]:
        if key in _AUD["playing"]:
            return "ok: already making it"
        _AUD["playing"].add(key)
        _AUD["making"][key] = _AUD["making"].get(key, 0) + 1
    _enqueue(0, _clip_job(voice, ref, line, device))
    return "ok: making it"


def audition_all(voice: str, line: str) -> str:
    """Make them all: every reference with a transcript and no clip for this
    line, queued behind any single tap. One batch per voice at a time."""
    todo = [r for r in list_samples(VOICES_DIR / voice)
            if ref_text(voice, r) and not clip_path(voice, r, line).is_file()]
    with _AUD["lock"]:
        if voice in _AUD["batch"]:
            return "ok: already making them"
        if not todo:
            return "ok: all made"
        _AUD["batch"].add(voice)
        for r in todo:
            key = (voice, r, line_hash(line))
            _AUD["making"][key] = _AUD["making"].get(key, 0) + 1

    def done() -> None:
        with _AUD["lock"]:
            _AUD["batch"].discard(voice)
    for r in todo:
        _enqueue(1, _clip_job(voice, r, line))
    _enqueue(1, done)  # FIFO within a priority: runs after the last of this batch
    return f"ok: making {len(todo)}"


def known(voice) -> bool:
    v = mouth_voices()
    return isinstance(voice, str) and voice in v.get("kokoro", []) + v.get("clone", [])


def say_line(voice: str, text: str, device: str) -> int:
    """The speaker button: the phrase, in that voice, NOW (a tap is his ask; it
    used to wait for his turn end and a tap mid-sentence played 46 s late),
    interest zero (no agent wakes on his reply). As preview() does it."""
    env = dict(os.environ, VOICEMODE_AGENT="settings")
    try:
        r = subprocess.run([str(MOUTH_BIN), "say", "--device", device, "--voice", voice,
                            "--expires", "60", "--interest", "zero", text],
                           capture_output=True, text=True, timeout=15, env=env)
        return r.returncode
    except (OSError, subprocess.TimeoutExpired):
        return -1


def relink(d: Path, name: str, target: str) -> None:
    """Point d/name at target (relative), in one step: a new link, renamed over."""
    tmp = d / f".{name}.tmp-{os.getpid()}"
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    os.symlink(target, tmp)
    os.replace(tmp, d / name)


def repoint(voice: str, sample: str) -> str:
    """Star a sample: default.wav (and default.txt, which a clone reads as the
    clip's transcript) now point at it. Checked whole before anything moves."""
    d = VOICES_DIR / voice
    dw, dt, st = d / "default.wav", d / "default.txt", d / f"{sample}.txt"
    if not dw.is_symlink():
        return f"refused: {voice}/default.wav is a real file, not a link"
    if st.is_file():
        if dt.exists() and not dt.is_symlink():
            return f"refused: {voice}/default.txt is a real file, not a link"
    elif dt.exists() or dt.is_symlink():
        return f"refused: {sample} has no transcript; default.txt would not match it"
    relink(d, "default.wav", f"{sample}.wav")
    if st.is_file():
        relink(d, "default.txt", f"{sample}.txt")
    _VOICES["t"] = -VOICES_TTL  # the mouth re-reads its voices on the next state
    return "ok"


def edit_line(p: dict, value: dict) -> str:
    """The pencil: an edit for the lines. The line changes where it is kept (the
    voice's, the family's or everyone's), and wherever a voice is saying it or has
    it locked, so an edited line never leaves a stale copy behind."""
    voice, level = value.get("voice"), value.get("level")
    if not known(voice):
        return f"refused: no voice {voice!r}"
    old, new = clean_line(value.get("old")), clean_line(value.get("text"))
    if not old or not new:
        return "refused: no line"
    found = False
    if level == "global":
        phrases = list(p.get("phrases") or PHRASES)
        if old in phrases:
            found = True
            p["phrases"] = [new if t == old else t for t in phrases if t != new or t == old]
        if p.get("phrase") == old:
            p["phrase"] = new
    elif level in ("voice", "family"):
        key = voice if level == "voice" else family_of(voice)
        if not key:
            return f"refused: {voice} has no family"
        where = f"{level}_lines"
        lines = dict(p.get(where) or {})
        mine = list(lines.get(key) or [])
        if old in mine:
            found = True
            lines[key] = [new if t == old else t for t in mine if t != new or t == old]
            p[where] = lines
    else:
        return f"refused: no level {level!r} (voice, family or global)"
    for k in ("current", "locked"):
        d = dict(p.get(k) or {})
        hit = [v for v, t in d.items() if t == old]
        for v in hit:
            d[v] = new
        found = found or bool(hit)
        p[k] = d
    if not found:
        return f"refused: no line {old!r} to edit"
    save(PREFS, p)
    return "ok"


def apply_voice(rest: list, value) -> str:
    """The voice browser's paths (voices.*). Every voice is checked against
    `mouth voices`; every sample against its folder's own listing."""
    p = load(PREFS)
    if rest == ["heart"] and isinstance(value, dict) and is_bool(value.get("on")):
        voice = value.get("voice")
        if not known(voice):
            return f"refused: no voice {voice!r}"
        hearts = [h for h in p.get("hearts") or [] if h != voice]
        if value["on"]:
            hearts.append(voice)
        p["hearts"] = hearts
        save(PREFS, p)
        return "ok"
    if rest == ["hide"] and isinstance(value, dict) and is_bool(value.get("on")):
        voice = value.get("voice")
        if not known(voice):
            return f"refused: no voice {voice!r}"
        hidden = [h for h in p.get("hidden") or [] if h != voice]
        if value["on"]:
            hidden.append(voice)
        p["hidden"] = hidden
        save(PREFS, p)
        return "ok"
    if rest == ["star"] and isinstance(value, dict) and isinstance(value.get("family"), str):
        fam, voice = value["family"], value.get("voice")
        stars = dict(p.get("stars") or {})
        if voice is None:
            stars.pop(fam, None)
        elif not known(voice):
            return f"refused: no voice {voice!r}"
        elif family_of(voice) != fam:
            return f"refused: {voice!r} is not in family {fam!r}"
        else:
            stars[fam] = voice
        p["stars"] = stars
        save(PREFS, p)
        return "ok"
    if rest in (["phrase"], ["phrases", "add"]) and isinstance(value, str) and value.strip():
        text = " ".join(value.split())[:PHRASE_MAX]
        if rest == ["phrase"]:
            p["phrase"] = text
        else:
            phrases = list(p.get("phrases") or PHRASES)
            if text not in phrases:
                phrases.append(text)
            p["phrases"] = phrases
        save(PREFS, p)
        return "ok"
    if rest == ["refheart"] and isinstance(value, dict) and is_bool(value.get("on")):
        voice, ref = value.get("voice"), value.get("ref")
        if not known(voice) or not (VOICES_DIR / voice).is_dir():
            return f"refused: no clone {voice!r}"
        if not isinstance(ref, str) or ref not in list_samples(VOICES_DIR / voice):
            return f"refused: no reference {ref!r} for {voice}"
        refhearts = dict(p.get("refhearts") or {})
        mine = [r for r in refhearts.get(voice) or [] if r != ref]
        if value["on"]:
            mine.append(ref)
        if mine:
            refhearts[voice] = mine
        else:
            refhearts.pop(voice, None)
        p["refhearts"] = refhearts
        save(PREFS, p)
        return "ok"
    if rest == ["lines", "edit"] and isinstance(value, dict):
        return edit_line(p, value)
    if rest in (["line"], ["lines", "add"], ["lock"]) and isinstance(value, dict):
        voice = value.get("voice")
        if not known(voice):
            return f"refused: no voice {voice!r}"
        if rest == ["lock"]:
            if not is_bool(value.get("on")):
                return "refused: path not allowed"
            locked = dict(p.get("locked") or {})
            if value["on"]:
                locked[voice] = line_for(voice, True, p)
            else:
                locked.pop(voice, None)
            p["locked"] = locked
            save(PREFS, p)
            return "ok"
        text = clean_line(value.get("text"))
        if not text:
            return "refused: no line"
        if rest == ["lines", "add"]:
            level = value.get("level")
            if level == "global":
                phrases = list(p.get("phrases") or PHRASES)
                if text not in phrases:
                    phrases.append(text)
                p["phrases"] = phrases
            elif level in ("voice", "family"):
                key = voice if level == "voice" else family_of(voice)
                if not key:
                    return f"refused: {voice} has no family"
                where = f"{level}_lines"
                lines = dict(p.get(where) or {})
                mine = list(lines.get(key) or [])
                if text not in mine:
                    mine.append(text)
                lines[key] = mine
                p[where] = lines
            else:
                return f"refused: no level {level!r} (voice, family or global)"
        current = dict(p.get("current") or {})
        current[voice] = text
        p["current"] = current
        save(PREFS, p)
        return "ok"
    device = load(MOUTH).get("output") or "default"
    if rest == ["say"] and isinstance(value, dict):
        voice = value.get("voice")
        if not known(voice):
            return f"refused: no voice {voice!r}"
        # Try it: a "text" is said once and
        # kept nowhere. Before, Try it sent voices.line, which SAVED it and said nothing.
        text = clean_line(value.get("text")) if "text" in value else line_for(voice, value.get("page") is True, p)
        if not text:
            return "refused: no line"
        rc = say_line(voice, text, device)
        return "ok" if rc == 0 else f"error: mouth say rc={rc}"
    if rest in (["audition"], ["audition", "all"]) and isinstance(value, dict):
        voice = value.get("voice")
        if not known(voice) or not (VOICES_DIR / voice).is_dir():
            return f"refused: no clone {voice!r}"
        line = line_for(voice, True, p)
        if rest == ["audition", "all"]:
            return audition_all(voice, line)
        ref = value.get("ref")
        if not isinstance(ref, str) or ref not in list_samples(VOICES_DIR / voice):
            return f"refused: no reference {ref!r} for {voice}"
        if not ref_text(voice, ref):
            return f"refused: {voice}/{ref} has no transcript ({ref}.txt), so it can't be cloned from"
        return audition(voice, ref, line, device)
    if rest in (["play"], ["sample"]) and isinstance(value, dict):
        voice, sample = value.get("voice"), value.get("sample")
        if not known(voice) or not (VOICES_DIR / voice).is_dir():
            return f"refused: no clone {voice!r}"
        d = VOICES_DIR / voice
        if not isinstance(sample, str) or sample not in list_samples(d):
            return f"refused: no sample {sample!r} for {voice}"
        if rest == ["sample"]:
            return repoint(voice, sample)
        try:
            r = subprocess.run([str(MOUTH_BIN), "play", "--device", device, str(d / f"{sample}.wav")],
                               capture_output=True, text=True, timeout=15)
            return "ok" if r.returncode == 0 else f"error: mouth play rc={r.returncode}"
        except (OSError, subprocess.TimeoutExpired) as x:
            return f"error: mouth play {type(x).__name__}"
    return "refused: path not allowed"


def apply_tool(tool: str, keys: list, value) -> str:
    """One key of a drawn tool's file: the schema decides, plus each widget's live check."""
    try:
        s = schemas.prop(json.loads(schemas.schema_path(tool).read_text()), keys)
    except (OSError, ValueError):
        s = None
    if isinstance(s, dict) and s.get("x-widget") == "audio-input" and isinstance(value, str) \
            and value != "default" and value not in devices("input"):
        return f"refused: no input {value!r}"
    if isinstance(s, dict) and s.get("x-widget") == "audio-output" and isinstance(value, str) \
            and value != "default" and value not in devices("output"):
        return f"refused: no output {value!r}"
    return schemas.write(tool, keys, value)


def public(d: dict) -> dict:
    return {k: v for k, v in d.items() if not k.startswith("_")}


def tools(ears: dict, cap: dict) -> dict:
    """tools.TOOL = {schema, value, file, schema_file[, status]} for each tool drawn."""
    out = {}
    for t in schemas.registry():
        out[t] = schemas.tool_state(t)
        if t == "ears":   # the one live line the ears' screen shows above its settings
            on = ears.get("listen", True) is not False
            out[t]["status"] = {"up": on and cap["up"], "text": "Stopped" if not on else
                                "Capturing on " + cap["device"] if cap["up"] else "Starting…"}
    return out


def sounds_service() -> bool:
    """Whether the ear-sounds launch agent is loaded on this Mac. A bare `ears services
    install` does not load it (sessionvoice services.py:51-57), so a toggle drawn from the
    sounds file alone would look on while nothing plays.
    launchctl print answers only for a loaded service; run() is "" otherwise. Three labels:
    the kit's, and the two a hand-rendered sidecar still runs under (services.py HAND)."""
    return any(run("launchctl", "print", f"gui/{os.getuid()}/{label}") for label in SOUNDS_LABELS)


SOUNDS_LABELS = ("dev.sessionkit.voice.ears-sounds", "dev.sessionvoice.ears-sounds", "dev.voicemode.ears-sounds")


_HEARS: tuple = (0.0, {"names": [], "more": 0, "by": {}})


def hears() -> dict:
    """Who can hear the room: {"names": [...], "more": N}. A name per seat whose live session
    is subscribed to the heard box (`message subscription list heard`), named by `session
    list --json`; a session with no seat goes by its name's stem ("alex-ec" -> "alex").
    `more` counts the subscribed sessions this host cannot name (another host's, or one
    that ended with its subscription listed): the page never says fewer than the truth.
    `by` is the same, per address: "room" for the box itself (every
    device's turns) and "+TAG" for a claimed device tag (that device's turns only), each
    a list of names, an unnamed session as its short id. Empty on a Mac with no heard box. Two commands (~0.6 s), so the
    answer is kept for 30 s between polls."""
    global _HEARS
    if time.time() - _HEARS[0] < 30:
        return _HEARS[1]
    subs = run("message", "subscription", "list", "heard")
    shorts = set(re.findall(r"\b([0-9a-f]{8})\s+session\b", subs))
    names = list(re.findall(r"\b([a-z][\w-]*)\s+seat subscription\b", subs))
    by, tag = {}, "room"   # the tree: a "+TAG ... claimed" line opens a tag; the lines under it are its
    for line in subs.splitlines():
        m = re.match(r"^(\s*)[│├└─\s]*(\+\w[\w-]*)\s", line)
        if m and "claimed" in line:
            tag = m.group(2); continue
        m = re.match(r"^(\s*)[│├└─\s]*([0-9a-f]{8}|[a-z][\w-]*)\s+(session|seat) subscription\b", line)
        if m:
            by.setdefault("room" if not m.group(1) and tag == "room" or not m.group(1) and not line.startswith(" ") else tag, []).append(m.group(2))
    try:
        sessions = json.loads(run("session", "list", "--json") or "[]") if shorts else []
    except ValueError:
        sessions = []
    named = set()
    for s in sessions if isinstance(sessions, list) else []:
        if isinstance(s, dict) and s.get("short") in shorts:
            n = s.get("seat") or re.sub(r"-[0-9a-f]{2}$", "", s.get("name") or "")
            if n:
                named.add(s["short"])
                if n not in names:
                    names.append(n)
    label = {s["short"]: (s.get("seat") or re.sub(r"-[0-9a-f]{2}$", "", s.get("name") or "") or s["short"])
             for s in (sessions if isinstance(sessions, list) else []) if isinstance(s, dict) and s.get("short")}
    by = {k: [label.get(v, v) for v in vs] for k, vs in by.items()}
    _HEARS = (time.time(), {"names": names, "more": len(shorts - named), "by": by})
    return _HEARS[1]


def host() -> str:
    """The short name of the host this runs on ("studio", "laptop"): the app names it, never
    a hard-coded host. Read on every call, so the state names wherever it is served."""
    return socket.gethostname().split(".")[0]


def state() -> dict:
    ears, mouth, vs = load(EARS), load(MOUTH), mouth_voices()
    cap = capture()
    outs = devices("output")
    out = mouth.get("output") or "default"
    if out != "default" and out not in outs:
        # The picked speaker left. Left as is, EVERY line dies as device-absent and the
        # Talk page draws each as a cut (a whole set of
        # auditions vanished that way). Follow the Mac again, and say so in the event log.
        mouth["output"] = "default"
        save(MOUTH, mouth)
        event(["mouth", "output"], out, "default", "settings: output not present")
    return {"kind": "state", "app": "notify", "at": time.strftime("%H:%M:%S"), "host": host(),
            "global": {"allow": load(GLOBAL).get("allow", True) is not False},
            "ears": {"listen": ears.get("listen", True) is not False,
                     "input": ears.get("input") or "default",
                     "capture": cap,
                     "sounds": public(load(SOUNDS)),
                     "sounds_service": sounds_service()},
            "hears": hears(),
            "mouth": {"output": mouth.get("output") or "default",
                      "voice": mouth.get("voice", "af_sky"),
                      "voices": vs,
                      "families": families(vs),
                      "samples": samples(vs),
                      "prefs": prefs()},
            "devices": {"inputs": devices("input"), "outputs": outs,
                        "in": run("SwitchAudioSource", "-c", "-t", "input"),
                        "out": run("SwitchAudioSource", "-c", "-t", "output")},
            "tools": tools(ears, cap)}


_SAID_ABSENT: set = set()


def absent_once(line: str) -> None:
    """One stderr line per missing thing per process: a fresh install has no
    ears event log; the browser desk runs without it."""
    if line not in _SAID_ABSENT:
        _SAID_ABSENT.add(line)
        print(f"settled: {line}", file=sys.stderr, flush=True)


def is_bool(v) -> bool:
    return isinstance(v, bool)


def apply(path: list, value) -> str:
    if path == ["global", "allow"] and is_bool(value):
        g = load(GLOBAL); g["allow"] = value; save(GLOBAL, g); return "ok"
    if path == ["ears", "listen"] and is_bool(value):
        e = load(EARS); e["listen"] = value; save(EARS, e); return "ok"
    if path == ["ears", "input"] and isinstance(value, str):
        if value != "default" and value not in devices("input"):
            return f"refused: no input {value!r}"
        e = load(EARS); e["input"] = value; save(EARS, e); return "ok"
    if path == ["mouth", "output"] and isinstance(value, str):
        if value != "default" and value not in devices("output"):
            return f"refused: no output {value!r}"
        m = load(MOUTH); m["output"] = value; save(MOUTH, m); return "ok"
    # Outputs get a heart and a test button: ♥ = one
    # you like, so the list can show just those; 🔈 = say a line on THAT output now.
    if path == ["mouth", "heart"] and isinstance(value, dict) and is_bool(value.get("on")):
        dev = value.get("device")
        if not isinstance(dev, str) or dev not in devices("output"):
            return f"refused: no output {dev!r}"
        p = load(PREFS)
        hearts = [d for d in p.get("out_hearts") or [] if d != dev]
        if value["on"]:
            hearts.append(dev)
        p["out_hearts"] = hearts
        save(PREFS, p)
        return "ok"
    if path == ["mouth", "test"] and isinstance(value, str):
        if value != "default" and value not in devices("output"):
            return f"refused: no output {value!r}"
        rc = say_line(load(MOUTH).get("voice") or "af_sky", "This is where I'll speak.", value)
        return "ok" if rc == 0 else f"error: mouth say rc={rc}"
    if path == ["mouth", "voice"] and isinstance(value, str):
        v = mouth_voices()
        if value not in v.get("kokoro", []) + v.get("clone", []):
            return f"refused: no voice {value!r}"
        m = load(MOUTH); m["voice"] = value; save(MOUTH, m)
        preview(value, m.get("output") or "default")
        return "ok"
    if path[:1] == ["voices"]:
        return apply_voice(path[1:], value)
    if path[:1] == ["tool"] and len(path) > 2 and isinstance(path[1], str):
        return apply_tool(path[1], path[2:], value)
    if path[:2] != ["ears", "sounds"]:
        return "refused: path not allowed"
    rest, s = path[2:], load(SOUNDS)
    if rest in (["allow"], ["pair"]) and is_bool(value):
        s[rest[0]] = value
    elif len(rest) == 3 and rest[0] == "sounds" and rest[2] == "on" and is_bool(value):
        if rest[1] not in s.get("sounds", {}):
            return f"refused: no sound {rest[1]!r}"
        s["sounds"][rest[1]]["on"] = value
    elif rest == ["to"] and isinstance(value, list) and all(isinstance(x, str) for x in value):
        s["to"] = value
    else:
        return "refused: path not allowed"
    save(SOUNDS, s)
    return "ok"


def event(path: list, old, new, by: str, source: str = "phone") -> int:
    """A config-change event in the ears' event log (heard_DATE.jsonl), so the
    change sits in the same timeline as what was heard: a config change belongs
    in the event log. SOURCE says which side made it ("web" from the desk). 127, said once, when
    no writer is named or it is not there."""
    text = f"{'.'.join(map(str, path))}: {json.dumps(old)} -> {json.dumps(new)} (by {by or '?'})"
    if HEARD is None or not HEARD.is_file():
        absent_once(f"no ears event writer at {HEARD or '(SETTINGS_HEARD unset)'}: "
                    "config changes are not written to the heard log")
        return 127
    r = subprocess.run(["python3", str(HEARD), "write", "event", "config-change",
                        "--source", source, "--text", text],
                       capture_output=True, text=True, timeout=10)
    return r.returncode


def lookup(d, path: list):
    if path[:1] == ["tool"] and len(path) > 1:   # its value sits at tools.TOOL.value in the state
        path = ["tools", path[1], "value"] + list(path[2:])
    for k in path:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def log(rec: dict) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(json.dumps(rec) + "\n")


def said(s: dict, path: list, result: str) -> dict:
    """A tap the Mac refused rides back in the state as `said`, so the screen can show why.
    The next push without it clears it (the poll sees the difference)."""
    if result != "ok":
        s["said"] = {"path": path, "result": result}
    return s


def same(a: dict, b: dict) -> bool:
    return {k: v for k, v in a.items() if k != "at"} == {k: v for k, v in b.items() if k != "at"}


def keep(d: dict, t0: float) -> None:
    """Save a state() read as SNAPSHOT, its mtime set to t0, when the read BEGAN: a reader
    that applied a change before t0 knows this read saw it."""
    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    tmp = SNAPSHOT.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d) + "\n")
    os.utime(tmp, (t0, t0))
    tmp.replace(SNAPSHOT)  # a rename keeps the mtime; readers never see half a file


def snapshot(newer_than: float = 0.0):
    """The applier's last state() read (private/apply.py, the follower, keeps it), for a
    second reader (web.py's poll).

    (state, when it began) if SNAPSHOT is fresh (under 3 POLLs old) and began after newer_than;
    None if it is fresh but not newer: nothing has changed since the caller's own read
    (a web tap just now must not be undone by an older snapshot); else, when the applier
    is down or the file is unreadable, (state(), now), read here."""
    try:
        t = SNAPSHOT.stat().st_mtime
        if time.time() - t < 3 * POLL:
            return (json.loads(SNAPSHOT.read_text()), t) if t > newer_than else None
    except (OSError, ValueError):
        pass
    t0 = time.time()
    return state(), t0
