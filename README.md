# settled

A secure, live, local web desk for any settings file a JSON Schema describes.
Each tool keeps its own settings file and its own behaviour; settled draws a
screen from the tool's schema, checks every write against it, and serves the
screens on this Mac, locked with a passkey. Session Kit is its first user, and
the kit requires it.

Home: [github.com/sessionkit/settled](https://github.com/sessionkit/settled).

## Install: the `settled` command

settled is a Python package with one command, so nothing needs a checkout or a
particular directory. From PyPI, a checkout, or a wheel (`make wheel`):

```sh
uv tool install settled             # or: --from . settled, or --from dist/settled-*.whl settled
settled install     # the launch agent dev.sessionkit.settled.desk, port 8770
settled status      # the agent, its port, the passkey count
```

uv puts the command in `~/.local/bin`; if your shell does not find `settled`,
run `uv tool update-shell` (uv says so after the install) and open a new shell.

**The first passkey.** Run `settled enrol` on the Mac: it prints a one-time
code, valid 10 minutes for one passkey. Open **<http://localhost:8770/>** in
your browser on this Mac. Use `localhost`, not `127.0.0.1`: the desk accepts
only its own origin, and an IP address cannot hold a passkey (that page says
"can't hold a passkey"). Choose **Enrol this device**, type the code, then
choose **Make the passkey** and touch Touch ID. `settled status` then says
`passkeys: 1`. More passkeys (a phone, another browser) are added from the
unlocked page.

Every passkey that is not added from an unlocked page needs a code, the very
first included: the page never trusts whoever reaches it first, since a
browser names its own origin and `tailscale serve` passes on what it claims.

**`settled enrol`** works the same way for an address that has no passkey of its
own while another address already holds one (for example the Mac's https name,
once the `localhost` page has a passkey). Run it on the Mac itself, open that
address, choose **Enrol this device**, type the code, then choose
**Make the passkey**. If you can reach the address that has a passkey,
unlocking there and adding one is the other way.

`settled install` also writes the desk's service file,
`~/.sessionkit/settled/services/desk.json`, so `sessionkit service list` shows it
as `desk` in the `settled` group. `settled serve [--port N]` runs the desk in the
foreground; `settled uninstall` boots the agent out and leaves the service file
saying `enabled: false`; `settled web-room` is the front door's room, and refuses until the
door can proxy a WebSocket. `settled[kit]` adds sessiontools for the kit's
registry. Every module is inside the one package (`settled.web`,
`settled.desk`, ...), so nothing of settled's sits at the top level of a
shared venv.

**Where the files live** (`settled/paths.py`): settled's own state
(`passkeys.json`, `enrol.json`, `sessions.json`, `voice-prefs.json`, `log/`)
sits in ONE folder, `~/.sessionkit/settled/` (`$SETTLED_HOME`). The tools'
settings files are NOT there: each lives where its reader reads it. With the
kit (`settled[kit]`, and `~/.sessionkit/` present) that is the kit's registry,
read the kit's own way (`sessiontools.settings`: `PACKAGES`, `package_dir`,
`discovered_tools`): `~/.sessionkit/voice/settings/ears.json`, `mouth.json`,
`ears-sounds.json` with their `.<tool>.schema.json` beside them,
`~/.sessionkit/mail/settings/mail.json`, and the kit's own validator
(jsonschema) for the kit's own schemas; the desk writes the HOST layer, the
file the launch agents read. Without the kit, or with `SETTINGS_ROOT` set, the
standalone registry below applies (`$SETTINGS_ROOT/<tool>/`, by default
`~/.sessionkit/settled/settings/<tool>/`). A fresh install writes nothing
outside `~/.sessionkit`.

A passkey store from an older layout is carried once: run any `settled` verb
once with `SETTLED_CARRY_FROM` naming the old state folder, and its
`passkeys.json` is copied to the new folder (said on stderr; the old file is
left). With no Delta Chat bot, no `SwitchAudioSource` and no ears event writer,
the desk still serves, and says once what it is not doing. All of it is
measured by `tests/test_fresh_install.py`, the fresh-install gate.

## What it is

- **The page:** `app/`, one `index.html`, served by the desk over HTTP with a
  `webxdc.js` shim (it also builds as a webxdc app for Delta Chat). The screens:
  - Home: Allow notifications; Ears, Mouth, Sounds; Eyes greyed out as "later".
  - Ears: Listening on/off, and Input (follow the Mac, or pin one mic).
  - Mouth: Output (follow the Mac, or one speaker), and Voice (the default for a line that names none).
  - Mouth > Voice, the voice browser:
    - The phrase they'll say: pick one from the list, or type your own. "Try it" is not kept; "Add to list" is.
    - Hearts only: a filter on this device.
    - Kokoro and Cloned: families fold in place (a show or gang; Kokoro by af, am, bf, bm).
    - Each voice row has 🔈 (says its locked line, else the phrase, on the Mac) and ♥. Tap the name to dive in.
    - A voice's own screen: use it as the mouth's voice, its family's one ★, and its samples.
      Each sample has ▶ (plays on the Mac) and ★ (the default: repoints `default.wav` and `default.txt`).
    - The audition:
      - Its line: the one it was last saying on its page (sticky per voice), else its 🔒 locked
        default, else the phrase. "Change the line" lists its own lines, then the family's, then
        everyone's; a typed line can be tried, or saved at any of the three levels.
      - Each reference also has 🔈: the line, cloned from that one clip, made once into
        `<voice>/.auditions/<ref>.<hash>.mp3` (with a `.txt` holding the line) and kept.
        🔊 made, ⏳ making, 🔇 no transcript (refused, never another voice). "Make them all"
        makes the rest in the background, one at a time.
  - Sounds: which ears events make a sound, and where they play.
- **The core, `settled/desk.py`:** the one set/read path the desk calls.
  - It applies each tap to an allow-listed path, and reads the whole state back.
  - It writes a `config-change` event to the ears' log for each tap, when `SETTINGS_HEARD` names the writer.
  - Its own files go under `~/.sessionkit/settled/`: the log, the voice preferences, the state snapshot.
- **The lock:** a passkey (WebAuthn) per address. The first at every address,
  the first of all included, is enrolled with a one-time code shown on the
  machine itself: run `settled enrol` on the Mac, open `http://localhost:8770/`,
  choose Enrol this device, type the code. More are added from an unlocked page.
- **The launch agent:** `settled install` writes it. From a checkout,
  `make agent` renders `launchd/dev.sessionkit.settled.desk.plist`, the same
  label, onto its own env (below).

## From a checkout

```bash
make build    # dist/settings.xdc, the page as a webxdc app
make icon     # re-render app/icon.png from app/icon.html
make venv     # build (or top up) the env the launch agent runs, from requirements.txt
make agent    # render the launch agent's template, install it and (re)start it
make status   # is it running, the env, and its last log lines
make enrol    # a one-time code: an address with no passkey of its own enrols one (settled enrol)
make test     # the suite: scratch HOME, stub tmux, temp TMUX_TMPDIR, no TMUX
make wheel    # the wheel and sdist, into dist/
```

`make test` is the one way to test settled. Its deps are declared in
`requirements-test.txt` (add one there, never inline in the Makefile).
tests/conftest.py sets the same four guards for a bare `pytest tests`, and
fails any test that leaves a fake behind; tests/test_guards.py proves both.

**Runtime deps** are declared in `pyproject.toml` and, for the checkout's env,
in `requirements.txt`: cryptography, websockets, tomlkit, on python3 >= 3.11.
Without tomlkit a TOML write refuses and names the module; reading TOML uses
stdlib `tomllib`, so TOML screens still draw. `requirements-test.txt` repeats
every runtime dep for the suite, and tests/test_guards.py holds the two lists
together.

### The launch agent from a checkout

The agent runs the checkout's own env, never a borrowed python:
`~/.local/share/settings/venv` (`VENV`), built with uv from `requirements.txt`.
It sits outside every git checkout, so no worktree cleanup can take it.
Deploying touches `~/Library/LaunchAgents` and restarts a live service.

1. `make venv`: builds the env with `uv venv` (python >= 3.11, a uv-managed
   interpreter), then `uv pip install -r requirements.txt` into it. Re-running
   it is safe: a working env is kept and only a missing dep is added.
2. `make agent`: fills the template's slots (`HOME`, `VENV`, the checkout,
   `PORT`, `SETTINGS_ROOT`), writes it to
   `~/Library/LaunchAgents/dev.sessionkit.settled.desk.plist`, then
   `launchctl bootout` and `launchctl bootstrap gui/$(id -u)` it. It refuses if
   the env is missing, or if run from a task worktree.
3. `make status`: the label loaded, the env's python version, and the last
   lines of `~/.sessionkit/settled/log/web.log`.

The template names no home; tests/test_own_env.py holds it, its rendering and
the Makefile's `VENV` default to one env, and builds one into the suite's
scratch HOME to prove it imports the deps.

## Screens drawn from a schema

Each tool's settings are defined by a JSON Schema, and settled draws the
screen from it.

- **The registry:** `<root>/<tool>/.<tool>.schema.json` (the undotted name
  still counts), values in `<tool>.json` or `<tool>.toml` beside it. When both
  exist settled picks neither: the screen says so and every write refuses.
  `schemas.DRAWN` names the tools drawn so far (`ears mouth ears/ears-sounds
  ears/talk notifications/global desk`; `SETTINGS_DRAWN` overrides it).
- **TOML:** validated with the same subset, on the parsed data. TOML has
  no null, so a null write to a TOML file refuses. A write round-trips through
  tomlkit, keeping every comment and the key order, then is proved: a write that
  would drop a comment (or reorder keys) refuses and leaves the file as it was.
- **Desk's schema ships here, not installed:** `tool-schemas/desk.schema.json`,
  written from desk's own `desk.toml.example` (every key desk accepts; desk
  refuses any other). Installing it beside a live `desk.toml` puts Desk on the
  screen.
- **The controls:** boolean = switch; enum, or anyOf of titled consts = picker (a
  branch with no const is "type your own"); string or number = a line with 📝;
  object = a group. `_` keys are notes, never drawn.
- **Hints, inside the schema:** `x-widget` plugs in a built-in control
  (`audio-input`: the Mac's inputs, folded); `x-hint` is the line under a label.
- **A write:** `tool.TOOL.KEY...` sets one key, then the whole file is checked
  against the schema (`settled/schemas.py`, a 2020-12 subset; a schema using anything
  outside it refuses every write). Every other key is kept; the file is replaced
  in one step. A refusal comes back in the state as `said`, and the screen shows it.

## The protocol

- **In, a tap:** `{from:"xdc", app:"notify", kind:"set", path, value, by, at}`
- **Out, the truth:** `{kind:"state", app:"notify", at, global, ears, mouth, devices}`
- The page folds updates in order: a "state" replaces everything, and a "set" patches one path.

## The settings it edits (the allow-list in `settled/desk.py`)

Each file is where its reader reads it: the kit's registry with the kit, else
the standalone root (`$SETTINGS_ROOT`).

- **`global.allow`:** the notifications switch, `notifications/global.json`.
- **`ears.listen`, `ears.input`:** the ears' file, `ears.json`. The ears read it every second.
- **`mouth.output`, `mouth.voice`:** the mouth's file, `mouth.json`. The mouth reads it for every
  line that names no voice. The choices are `mouth voices`: Kokoro's English voices and every
  clone in the voices folder (`SETTINGS_VOICES_DIR`, else `VOICEMODE_VOICES_DIR`, else
  `~/.sessionkit/settled/voices`).
- **`ears.sounds.*`:** the sounds file, `ears-sounds.json`, read live.
- **`voices.heart`, `voices.star`, `voices.phrase`, `voices.phrases.add`:** the voice browser's
  own prefs, `~/.sessionkit/settled/voice-prefs.json` (`SETTINGS_VOICE_PREFS`).
- **`voices.sample`:** repoints a clone's `default.wav` (and `default.txt`) in the voices folder.
  Refused when either is a real file, or when the sample has no transcript but `default.txt` exists.
- **`voices.line`, `voices.lines.add`, `voices.lock`:** the audition's lines, in the same prefs
  file: `current` (sticky per voice), `voice_lines`, `family_lines` (global stays `phrases`)
  and `locked`.
- **`voices.say`, `voices.play`:** actions, not settings. They run `mouth say` / `mouth play` and
  store nothing. No config-change event. `voices.say` with `page: true` says the voice page's line.
- **`voices.audition`, `voices.audition.all`:** actions. They clone the page's line from one
  reference (or every missing one) through the mlx-audio server on 8890, with the model the mouth
  clones with (`SETTINGS_TTS_URL`, `SETTINGS_CLONE_MODEL` to override), on one worker thread so a
  tap never waits. The files land in `.auditions/`. The hash is the model and the line: a new
  line, a new clip.
- Tests: `python3 tests/test_voice_browser.py` (a temp voices dir; the mouth is stubbed).

## Licence

AGPL-3.0-or-later, like Session Kit: the full text is in [LICENSE](LICENSE).
