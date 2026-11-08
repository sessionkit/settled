# settled: build the Settings webxdc app, run the desk from a checkout, test it.
# A checkout may carry an export-ignored private/agents.mk (never in the public
# tree): it adds that host's own launch agents and targets to the lists below.

CHAT   ?= 12
PORT   ?= 8770
# The desk's launch agent: the label `settled install` writes too (settled/cli.py).
DESK   := dev.sessionkit.settled.desk
# Each agent `make agent` installs, by its template (the label is the file name).
AGENT_PLISTS  := launchd/$(DESK).plist
# Labels `make agent` boots out and removes first (an older name for the same job).
LEGACY_AGENTS :=
STATUS_LOGS   := $(HOME)/.sessionkit/settled/log/web.log
# Standalone (no kit), the desk reads and writes each tool's settings here.
SETTINGS_ROOT ?= $(HOME)/.sessionkit/settled/settings
# The env the launch agents run: built by `make venv` from requirements.txt, at
# a fixed path outside every git checkout, so no worktree cleanup can take it
# away. The desk's template in launchd/ runs its python (@VENV@);
# tests/test_own_env.py holds this default and the template to the same path.
VENV   ?= $(HOME)/.local/share/settings/venv
UV     ?= uv
CHROME := /Applications/Google Chrome.app/Contents/MacOS/Google Chrome

-include private/agents.mk

.PHONY: build icon venv agent status enrol test wheel

build: dist/settings.xdc  ## zip the app (webxdc.js is never zipped)

dist/settings.xdc: app/index.html app/manifest.toml app/icon.png
	mkdir -p dist
	cd app && python3 -c 'import zipfile,sys; z=zipfile.ZipFile("../$@","w",zipfile.ZIP_DEFLATED); [z.write(f) for f in sys.argv[1:]]; z.close()' index.html desk.css manifest.toml icon.png
	@ls -l $@

icon:  ## render app/icon.html to app/icon.png (512, then 256 px)
	mkdir -p dist
	"$(CHROME)" --headless=new --disable-gpu --hide-scrollbars --window-size=512,512 \
	  --screenshot=$(CURDIR)/dist/icon-512.png "file://$(CURDIR)/app/icon.html"
	sips -Z 256 dist/icon-512.png --out app/icon.png

# Idempotent: a working env (python >= 3.11) is kept, as uv refuses to create
# over one, and the install then finds every dep already there. --clear only
# replaces a BROKEN venv (pyvenv.cfg there, its python dead); a directory that
# is not a venv is never cleared: uv refuses it and the target fails.
# --managed-python pins the interpreter to one uv installs and owns, never
# Homebrew's, so a `brew upgrade` cannot pull the python out from under the env.
venv:  ## build (or top up) the env the launch agents run, from requirements.txt, with uv
	@test -n "$(VENV)" || { echo "VENV is empty" >&2; exit 2; }
	@if "$(VENV)/bin/python" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then \
	  echo "env: $(VENV) (exists, kept)"; \
	elif test -f "$(VENV)/pyvenv.cfg"; then \
	  $(UV) venv --no-project --managed-python --clear --python '>=3.11' "$(VENV)"; \
	else \
	  $(UV) venv --no-project --managed-python --python '>=3.11' "$(VENV)"; \
	fi
	$(UV) pip install --python "$(VENV)/bin/python" -r requirements.txt
	$(UV) pip check --python "$(VENV)/bin/python"

agent:  ## render and install each launch agent, then (re)start it; run `make venv` first
	@test -x "$(VENV)/bin/python" || { echo "no env at $(VENV): run make venv first" >&2; exit 1; }
	@case "$(CURDIR)" in */worktree|*/worktree/*) echo "make agent: run it from a checkout, never a task worktree (its cleanup would take the agent's code)" >&2; exit 1;; esac
	@mkdir -p "$(HOME)/.sessionkit/settled/log" ~/Library/LaunchAgents
	@for a in $(LEGACY_AGENTS); do \
	  launchctl bootout gui/$$(id -u)/$$a 2>/dev/null || true; rm -f ~/Library/LaunchAgents/$$a.plist; \
	done
	@for p in $(AGENT_PLISTS); do a=$$(basename $$p .plist); \
	  sed -e 's|@HOME@|$(HOME)|g' -e 's|@VENV@|$(VENV)|g' -e 's|@CHECKOUT@|$(CURDIR)|g' \
	      -e 's|@PORT@|$(PORT)|g' -e 's|@SETTINGS_ROOT@|$(SETTINGS_ROOT)|g' \
	      $$p > ~/Library/LaunchAgents/$$a.plist || exit 1; \
	  launchctl bootout gui/$$(id -u)/$$a 2>/dev/null || true; \
	  launchctl bootstrap gui/$$(id -u) ~/Library/LaunchAgents/$$a.plist || exit 1; \
	done
	@sleep 3; for p in $(AGENT_PLISTS); do a=$$(basename $$p .plist); launchctl list | grep $$a || echo "$$a: not loaded"; done

status:  ## is each launch agent running, the env they run, and their last log lines
	@for p in $(AGENT_PLISTS); do a=$$(basename $$p .plist); launchctl list | grep $$a || echo "$$a: not loaded"; done
	@"$(VENV)/bin/python" --version 2>/dev/null && echo "env: $(VENV)" || echo "env: $(VENV) missing (make venv)"
	@for l in $(STATUS_LOGS); do tail -3 $$l; done

enrol:  ## print a one-time code to enrol this host's first passkey of its own
	python3 -m settled.web enrol

# The suite runs behind four guards, so a test never touches the live HOME or
# tmux: (1) a scratch HOME, as it once wrote
# live voice data; (2) a stub tmux first on PATH that fails loudly if called;
# (3) a temp TMUX_TMPDIR; (4) TMUX and TMUX_PANE unset. Unsetting TMUX alone is
# unsafe: tmux then falls back to the default socket. tests/conftest.py applies
# the same four, so a bare `pytest tests` is guarded too; tests/test_guards.py
# proves them. Its deps are declared in requirements-test.txt, not here.
test:  ## the suite, behind its four guards: scratch HOME, stub tmux, temp TMUX_TMPDIR, no TMUX
	scratch=$$(mktemp -d "$${TMPDIR:-/tmp}/settings-make-test.XXXXXX") && mkdir "$$scratch/home" "$$scratch/tmux" && \
	env -u TMUX -u TMUX_PANE HOME="$$scratch/home" TMUX_TMPDIR="$$scratch/tmux" \
	  PATH="$(CURDIR)/tests/stubs:$$PATH" \
	  uvx --with-requirements requirements-test.txt pytest -q tests

wheel:  ## build the settled wheel and sdist into dist/ (pyproject; `uv tool install --from dist/settled-*.whl settled`)
	uv build
