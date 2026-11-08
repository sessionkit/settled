"""schemas.py: the tools that publish a JSON Schema, and the check on every write.

The settings app draws its screens from JSON Schema, migrated one tool at a
time, a little outage allowed, each looked at before the next. The first was
the ears.

The registry: a tool publishes `<root>/<tool>/<tool>.schema.json`, and its
settings live beside it in `<tool>.json`. Publishing is consent: a tool
that ships no schema is not drawn. The app draws a screen from the schema and
the file, so a new tool needs no new screen. DRAWN names the tools drawn so far: the ears
first, then the rest one at a time, each once the last has been looked at (Talk's
Show Debug, ears/talk)
(SETTINGS_DRAWN overrides it, a list of tool names).

The check: a write sets one key path, then the whole file is validated against
the schema. It is refused if it adds an error the file did not already have,
so one bad key elsewhere never locks a screen out. Every other key is kept,
the `_` notes included. The file is replaced in one step, so a reader never
sees half of it.

The validator is the subset of JSON Schema 2020-12 that settings schemas use.
Neither deploy python3 had the jsonschema package, and the applier runs on
Homebrew's, which refuses a pip install. A schema that uses a keyword outside
the subset is refused as a whole (UNCHECKED), so nothing passes unchecked.

TOML (Desk's desk.toml first): a tool's settings may be
`NAME.toml` instead of `NAME.json`, paired with the same `.NAME.schema.json`. When
both exist settings picks neither: the screen shows why and every write refuses.
A TOML file is validated with the same subset, on the PARSED data (stdlib
tomllib, so the screen draws on any python3 >= 3.11). TOML has no null, so a
null write to a TOML file refuses. A TOML write round-trips through tomlkit
(parse, set the one key path, dump), keeping comments and key order; it is then
PROVED: if any comment in the old text is missing from the new, or the keys
already there change order, the write refuses and the file is left byte for
byte as it was. tomlkit is a runtime dependency (requirements.txt) that this
repo cannot install for you: Homebrew's python3, which runs the applier, does not
have it and refuses a pip install. Without it a TOML write refuses and names it
(fail closed); reads still work. A missing file is still written from nothing
as NAME.json.

Hints for the screen live in the schema itself as `x-` keywords, not in a
second file, so the two cannot drift apart: `x-widget` plugs a built-in control in place of the drawn one
("audio-input": the Mac's inputs, folded, with Follow the Mac first), and
`x-hint` is the short line under a label (else the description's first
sentence). Validation ignores every `x-` keyword. The widgets: "audio-input" and
"audio-output" (the Mac's devices, folded, Follow the Mac first; on an array, []
is Follow the Mac and a pick is [name]), "voice-picker" (a row into the voice
browser).
"""
import collections, copy, json, os, re, sys, tomllib
from pathlib import Path

from settled import paths  # settled's own folder, and the kit when it is installed

try:  # the TOML write path; reads use stdlib tomllib (see the docstring)
    import tomlkit
except ImportError:  # Homebrew's python3: no tomlkit, and pip refuses
    tomlkit = None

#: The standalone root: `$SETTINGS_ROOT`, else `settings/` in settled's own folder,
#: so a standalone desk writes nothing outside `~/.sessionkit` either.
ROOT = Path(os.environ.get("SETTINGS_ROOT") or paths.settled_home() / "settings").expanduser()
#: sessiontools.settings when the kit is installed and SETTINGS_ROOT is unset, else
#: None: with it, a tool's folder is the kit's registry (PACKAGES, package_dir,
#: discovered_tools), the schema `.<tool>.schema.json` then `<tool>.schema.json`
#: beside the file, exactly as the kit's own page reads it; without it, ROOT/<dir>.
KIT = paths.kit()
#: settled's own tools: in the kit they live under settled_home(), not in a package.
OWN = ("notifications/global", "desk")
#: Files in the kit's voice folder that sessiontools' map does not name by tool.
VOICE_FILES = {"ears-sounds": "voice", "mouth-sounds": "voice", "wake": "voice"}
DRAWN = tuple((os.environ.get("SETTINGS_DRAWN") or
                "ears mouth ears/ears-sounds ears/talk notifications/global desk").replace(",", " ").split())
UNCHECKED = ("$ref", "$dynamicRef", "if", "then", "else", "not", "dependentSchemas",
             "dependentRequired", "propertyNames", "unevaluatedProperties",
             "unevaluatedItems", "prefixItems", "contains", "uniqueItems", "multipleOf",
             "exclusiveMinimum", "exclusiveMaximum", "minProperties", "maxProperties",
             "minItems", "maxItems", "format")
WIDGETS = ("audio-input", "audio-output", "voice-picker")


def _where(tool: str) -> tuple:
    """A tool id is NAME when the file is <NAME>/NAME.json, else DIR/NAME."""
    d, _, n = tool.partition("/")
    return (d, n) if n else (d, d)


def _discovered() -> list:
    """The kit's discovered tools (a schema beside a settings file the map does
    not name): sessiontools' own walk; an unreadable folder is no tool."""
    if KIT is None:
        return []
    try:
        found, _notes = KIT.discovered_tools()
    except Exception:  # SettingsError, OSError, ValueError: not a tool
        return []
    return list(found)


def _dir(tool: str) -> Path:
    """The folder TOOL's settings file and schema sit in. Standalone: ROOT/<dir>.
    With the kit: the package folder sessiontools' map names for it
    (`~/.sessionkit/<package>/settings/`), else the voice folder for a file the
    map does not name, else where the kit's walk found its schema, else
    settled's own folder (the desk, notifications)."""
    d, n = _where(tool)
    if KIT is None:
        return ROOT / d
    package = KIT.PACKAGES.get(n) or VOICE_FILES.get(n)
    if package:
        return KIT.package_dir(package)
    for found in _discovered():
        if found.tool == n:
            return found.folder
    return paths.settled_home() / d if d != n else paths.settled_home()


def schema_path(tool: str) -> Path:
    """The tool's schema, a dot file beside its settings file (the tool
    owns it and installs it there; dot,
    because nobody edits it by hand). NAME.schema.json still counts until each
    tool's install writes the dot name."""
    d, n = _where(tool)
    here = _dir(tool)
    dot = here / f".{n}.schema.json"
    return dot if dot.is_file() or not (here / f"{n}.schema.json").is_file() else here / f"{n}.schema.json"


def file_path(tool: str) -> Path:
    """NAME.toml when only that exists, else NAME.json (a missing file is written as
    JSON). When both exist this still names the .json; `ambiguous` says so, and
    tool_state and write refuse rather than pick one."""
    d, n = _where(tool)
    here = _dir(tool)
    j, t = here / f"{n}.json", here / f"{n}.toml"
    return t if t.is_file() and not j.is_file() else j


def ambiguous(tool: str) -> str:
    """Why settings won't pick a file for the tool ('' when it can)."""
    d, n = _where(tool)
    here = _dir(tool)
    if (here / f"{n}.json").is_file() and (here / f"{n}.toml").is_file():
        return f"both {n}.json and {n}.toml exist; settings won't pick one, so remove one"
    return ""


def is_toml(p: Path) -> bool:
    return p.suffix == ".toml"


def registry() -> list:
    """The tools drawn, each only when its schema is there. Standalone, or with
    SETTINGS_DRAWN set: DRAWN's order. With the kit: every tool sessiontools'
    map names, then every schema its walk discovers beside a file the map does
    not name (ears-sounds), then settled's own (OWN). No schema, no form."""
    if KIT is None or os.environ.get("SETTINGS_DRAWN"):
        return [t for t in DRAWN if ".." not in t and schema_path(t).is_file()]
    out = []
    for t in [*KIT.PACKAGES, *(f.tool for f in _discovered()), *OWN]:
        if t not in out and ".." not in t and schema_path(t).is_file():
            out.append(t)
    return out


def _same(a, b) -> bool:
    """JSON equality: true is not 1, and key order does not matter."""
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def _kind(v) -> str:
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, int):
        return "integer"
    return {dict: "object", list: "array", str: "string", float: "number",
            type(None): "null"}.get(type(v), type(v).__name__)


def _is(t: str, v) -> bool:
    k = _kind(v)
    return k == t or (t == "number" and k == "integer")


def unchecked(schema, at: str = "") -> list:
    """Every keyword in the schema this validator does not check, with where it is."""
    out = []
    if isinstance(schema, dict):
        for k, v in schema.items():
            if k in UNCHECKED:
                out.append(f"{at or '/'}: {k}")
            if k in ("properties", "patternProperties") and isinstance(v, dict):
                for name, sub in v.items():
                    out += unchecked(sub, f"{at}/{name}")
            elif k in ("anyOf", "oneOf", "allOf") and isinstance(v, list):
                for i, sub in enumerate(v):
                    out += unchecked(sub, f"{at}/{k}/{i}")
            elif k in ("additionalProperties", "items") and isinstance(v, dict):
                out += unchecked(v, f"{at}/{k}")
    return out


def _titles(branches: list) -> str:
    return ", ".join(b.get("title") or json.dumps(b.get("const", "?")) for b in branches
                     if isinstance(b, dict))


def errors(schema, v, at: str = "") -> list:
    """What is wrong with v under schema, one line each, each naming where."""
    where = at or "/"
    if schema is True or schema == {}:
        return []
    if schema is False:
        return [f"{where}: not allowed"]
    if "type" in schema:
        ts = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_is(t, v) for t in ts):
            return [f"{where}: wants {' or '.join(ts)}, got {_kind(v)}"]
    out = []
    if "const" in schema and not _same(v, schema["const"]):
        out.append(f"{where}: must be {json.dumps(schema['const'])}")
    if "enum" in schema and not any(_same(v, e) for e in schema["enum"]):
        out.append(f"{where}: must be one of {json.dumps(schema['enum'])}")
    if isinstance(v, str):
        if len(v) < schema.get("minLength", 0):
            out.append(f"{where}: shorter than {schema['minLength']}")
        if "maxLength" in schema and len(v) > schema["maxLength"]:
            out.append(f"{where}: longer than {schema['maxLength']}")
        if "pattern" in schema and not re.search(schema["pattern"], v):
            out.append(f"{where}: does not match {schema['pattern']}")
    if _kind(v) in ("integer", "number"):
        if "minimum" in schema and v < schema["minimum"]:
            out.append(f"{where}: below {schema['minimum']}")
        if "maximum" in schema and v > schema["maximum"]:
            out.append(f"{where}: above {schema['maximum']}")
    if isinstance(v, dict):
        props, pats = schema.get("properties", {}), schema.get("patternProperties", {})
        for k in schema.get("required", []):
            if k not in v:
                out.append(f"{at}/{k}: is required")
        for k, x in v.items():
            named = False
            if k in props:
                named = True
                out += errors(props[k], x, f"{at}/{k}")
            for p, s in pats.items():
                if re.search(p, k):
                    named = True
                    out += errors(s, x, f"{at}/{k}")
            if not named:
                extra = schema.get("additionalProperties", True)
                if extra is False:
                    out.append(f"{at}/{k}: not a setting here")
                elif isinstance(extra, dict):
                    out += errors(extra, x, f"{at}/{k}")
    if isinstance(v, list) and isinstance(schema.get("items"), dict):
        for i, x in enumerate(v):
            out += errors(schema["items"], x, f"{at}/{i}")
    if "anyOf" in schema and not any(not errors(s, v, at) for s in schema["anyOf"]):
        out.append(f"{where}: is none of {_titles(schema['anyOf'])}")
    if "oneOf" in schema:
        n = sum(1 for s in schema["oneOf"] if not errors(s, v, at))
        if n != 1:
            out.append(f"{where}: is {n} of {_titles(schema['oneOf'])}, wants exactly one")
    for s in schema.get("allOf", []):
        out += errors(s, v, at)
    return out


def _plain(v):
    """TOML's parsed data as JSON's: a date or time becomes its ISO text."""
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_plain(x) for x in v]
    return v.isoformat() if hasattr(v, "isoformat") else v


def _toml(text: str) -> dict:
    return _plain(tomllib.loads(text))


def _load(p: Path):
    """A schema, or a settings file: JSON, or TOML parsed to plain data."""
    return _toml(p.read_text()) if is_toml(p) else json.loads(p.read_text())


def comments(text: str) -> list:
    """Every comment in a TOML text, each with its `#`, trailing space dropped.
    A `#` inside a string (basic, literal, or either multi-line kind) is not one.
    A scanner of its own, so the proof of a write does not trust tomlkit."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if c == "#":
            j = text.find("\n", i)
            j = n if j < 0 else j
            out.append(text[i:j].rstrip())
            i = j
        elif text.startswith(('"""', "'''"), i):
            q = text[i:i + 3]
            i += 3
            while i < n and not text.startswith(q, i):
                i += 2 if q == '"""' and text[i] == "\\" else 1
            i += 3
            while i < n and text[i] == q[0]:  # a quote or two before the close is content
                i += 1
        elif c in "\"'":
            i += 1
            while i < n and text[i] != c and text[i] != "\n":
                i += 2 if c == '"' and text[i] == "\\" else 1
            i += 1
        else:
            i += 1
    return out


def lost_comments(old: str, new: str) -> list:
    """The comments in old that new does not have (counted, so a repeat counts)."""
    return list((collections.Counter(comments(old)) - collections.Counter(comments(new))).elements())


def _order_kept(old, new) -> bool:
    """The keys of old keep their order in new, at every level (new keys may join)."""
    if not isinstance(old, dict) or not isinstance(new, dict):
        return True
    kept = [k for k in new if k in old]
    return kept == [k for k in old if k in new] and all(_order_kept(old[k], new[k]) for k in kept)


def _toml_text(text: str, keys: list, value) -> str:
    """text with keys set to value, through tomlkit, everything else as it was."""
    doc = tomlkit.parse(text)
    d = doc
    for k in keys[:-1]:
        if not isinstance(d.get(k), dict):
            d[k] = tomlkit.table()
        d = d[k]
    d[keys[-1]] = value
    return tomlkit.dumps(doc)


def _nulls(v) -> bool:
    return v is None or (isinstance(v, dict) and any(_nulls(x) for x in v.values())) or \
        (isinstance(v, list) and any(_nulls(x) for x in v))


def public(d: dict) -> dict:
    return {k: v for k, v in d.items() if not k.startswith("_")}


def prop(schema: dict, keys: list):
    """The schema of the property at keys: a named property, else the object's
    additionalProperties when that is a schema (a map, e.g. one sound per kind);
    None if neither."""
    s = schema
    for k in keys:
        if not isinstance(s, dict):
            return None
        named = (s.get("properties") or {}).get(k)
        s = named if named is not None else (
            s["additionalProperties"] if isinstance(s.get("additionalProperties"), dict) else None)
        if s is None:
            return None
    return s


def tool_state(tool: str) -> dict:
    """What the app draws a tool's screen from: its schema and its file's values."""
    out = {"file": str(file_path(tool)).replace(str(Path.home()), "~", 1),
           "schema_file": schema_path(tool).name.lstrip(".")}
    try:
        out["schema"] = _load(schema_path(tool))
    except (OSError, ValueError) as x:
        out["error"] = f"its schema can't be read: {type(x).__name__}: {x}"
        return out
    if ambiguous(tool):
        out["error"] = ambiguous(tool)
        return out
    try:
        doc = _load(file_path(tool))
    except FileNotFoundError:
        doc = {}
    except (OSError, ValueError) as x:
        out["error"] = f"its settings can't be read: {type(x).__name__}: {x}"
        return out
    out["value"] = public(doc) if isinstance(doc, dict) else {}
    return out


def write_atomic(p: Path, d) -> None:
    """d as JSON, or a str written as it is (a TOML text), replacing p in one step."""
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    tmp.write_text(d if isinstance(d, str) else json.dumps(d, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(p)  # one step: readers never see half a file


def _validator(schema):
    """With the kit: jsonschema's Draft 2020-12 validator for SCHEMA (sessiontools
    depends on jsonschema, so a kit install has it), which checks the kit's own
    schemas whole ($ref, uniqueItems, anyOf: the mouth's uses them). Standalone:
    None, and the subset applies, refusing a schema it cannot check whole."""
    if KIT is None:
        return None
    try:
        import jsonschema
        return jsonschema.Draft202012Validator(schema)
    except Exception:  # not installed, or a schema jsonschema itself refuses
        return None


def _full_errors(validator, v) -> list:
    """errors() as jsonschema finds them: one line each, each naming where."""
    out = []
    for e in sorted(validator.iter_errors(v), key=lambda e: [str(x) for x in e.absolute_path]):
        where = "/" + "/".join(str(x) for x in e.absolute_path)
        out.append(f"{where}: {e.message}")
    return out


def write(tool: str, keys: list, value) -> str:
    """Set keys to value in the tool's file, inside its schema; 'ok' or why not."""
    if tool not in registry():
        return f"refused: no tool {tool!r}"
    if not keys or any(not isinstance(k, str) or not k or k[0] in "_$" for k in keys):
        return f"refused: not a setting {keys!r}"
    try:
        schema = _load(schema_path(tool))
    except (OSError, ValueError) as x:
        return f"refused: the schema can't be read ({type(x).__name__})"
    full = _validator(schema)
    gaps = [] if full is not None else unchecked(schema)
    if gaps:
        return f"refused: the schema uses what settings can't check yet: {', '.join(gaps)}"
    if prop(schema, keys) is None:
        return f"refused: the schema has no {'/'.join(keys)}"
    if ambiguous(tool):
        return f"refused: {ambiguous(tool)}"
    p = file_path(tool)
    toml = is_toml(p)
    if toml and _nulls(value):
        return f"refused: TOML has no null, so {p.name} can't hold one"
    if toml and tomlkit is None:
        return (f"refused: writing {p.name} needs tomlkit, which this python "
                f"({sys.executable}) does not have")
    try:
        text = p.read_text() if toml else None
        doc = _load(p)
    except FileNotFoundError:
        doc, text = {}, ""
    except (OSError, ValueError) as x:
        return f"refused: {p.name} can't be read ({type(x).__name__})"
    if not isinstance(doc, dict):
        return f"refused: {p.name} is not an object"
    new = copy.deepcopy(doc)
    d = new
    for k in keys[:-1]:
        if not isinstance(d.get(k), dict):
            d[k] = {}
        d = d[k]
    d[keys[-1]] = value
    check = (lambda v: _full_errors(full, v)) if full is not None else (lambda v: errors(schema, v))
    before = set(check(doc))
    fresh = [e for e in check(new) if e not in before]
    if fresh:
        more = f" (and {len(fresh) - 1} more)" if len(fresh) > 1 else ""
        return f"refused: {fresh[0]}{more}"
    if toml:
        try:
            out = _toml_text(text, keys, value)
            back = _toml(out)
        except Exception as x:  # tomlkit can't place it, or wrote what tomllib won't read
            return f"refused: {p.name} can't take that ({type(x).__name__})"
        lost = lost_comments(text, out)
        if lost:
            more = f" (and {len(lost) - 1} more)" if len(lost) > 1 else ""
            return f"refused: the write would lose a comment in {p.name}: {lost[0]}{more}"
        if not _same(back, new):
            return f"refused: {p.name} would not read back as written"
        if not _order_kept(doc, back):
            return f"refused: the write would reorder the keys in {p.name}"
        write_atomic(p, out)
        return "ok"
    write_atomic(p, new)
    return "ok"
