"""paths.py: where settled keeps its own state, and how it finds the kit.

settled's own files (the passkeys, the pending enrol code, the sign-in
sessions, the applier's message ids, the voice preferences, the logs) live
under ONE folder, `settled_home()`: `$SETTLED_HOME`, else
`~/.sessionkit/settled/`, the kit's layout (`~/.sessionkit/<package>/`;
settled/cli.py spells the same rule for the launch agent and its log).
The tools' settings are NOT here: each lives where its tool reads it, found
by schemas.py (the kit's registry through sessiontools when the kit is
installed, else the standalone `SETTINGS_ROOT`).

Before the package the state sat in a folder of its own
that this code does not name. `legacy_state()` is that folder, given by
`$SETTLED_CARRY_FROM`; unset, there is nothing to carry. `carry()` copies a
file from it ONCE, the first time its new path is wanted and absent, so a
passkey enrolled before the move still signs in and nobody enrols again. A host
that had the old folder sets the variable for one run (its checkout's
private make target does). Nothing is deleted at the old path; it is said
once on stderr.
"""
import os
import shutil
import sys
from pathlib import Path


def settled_home() -> Path:
    """`$SETTLED_HOME`, else `~/.sessionkit/settled`."""
    return Path(os.environ.get("SETTLED_HOME") or Path.home() / ".sessionkit" / "settled").expanduser()


def docs() -> Path:
    """The kit's installed documents: `$SETTLED_DOCS`, else `~/.sessionkit/docs`. The plain
    document (docs/what-listens.md in the sessionkit repo) is ONE file: GitHub readers and
    the settled screen read the same text; the kit's
    install puts it here. Read on every call, so a review server can point elsewhere."""
    return Path(os.environ.get("SETTLED_DOCS") or Path.home() / ".sessionkit" / "docs").expanduser()


def legacy_state() -> Path | None:
    """Where the state sat before the package: `$SETTLED_CARRY_FROM`, else None
    (nothing to carry). The old folder is the host's to say, never this code's."""
    old = os.environ.get("SETTLED_CARRY_FROM")
    return Path(old).expanduser() if old else None


def kit():
    """`sessiontools.settings` when the kit is here: the `settled[kit]` extra is
    installed AND the kit's folder exists (`~/.sessionkit/`, as sessiontools
    spells it), and `SETTINGS_ROOT` does not say otherwise; else None. The one
    switch between the kit's registry and the standalone root. A Mac with the
    extra but no kit folder has nothing of the kit's to draw, so it is
    standalone until a kit tool makes the folder."""
    if os.environ.get("SETTINGS_ROOT"):
        return None
    try:
        from sessiontools import settings
    except ImportError:
        return None
    root = getattr(settings, "kit_root", None)
    folder = root() if callable(root) else Path.home() / ".sessionkit"
    return settings if folder.is_dir() else None


def carry(new: Path, old: Path, mode: int = 0o600) -> bool:
    """Copy OLD to NEW once: only when NEW is absent and OLD is a file. The
    copy is MODE from its first byte (0600: a passkey store), its folder
    0700. Says what it did on stderr. True when it copied."""
    if new.exists() or not old.is_file():
        return False
    new.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(new.parent, 0o700)
    tmp = new.with_name(new.name + ".carry")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "wb") as f, old.open("rb") as src:
        shutil.copyfileobj(src, f)
    os.chmod(tmp, mode)
    os.replace(tmp, new)
    print(f"settled: carried {old} to {new} (the old file is left in place)", file=sys.stderr, flush=True)
    return True
