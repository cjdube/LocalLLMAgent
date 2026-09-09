"""Resolve one setting, and write the ones the /settings page changes.

`config.getenv(key)` replaces `os.getenv(key)` everywhere a key has a row in
agent/schema.py. It resolves through four layers, first non-empty wins:

    1. environment variable — a real one, or one from config/.env
    2. config/settings.json — what the /settings page writes
    3. the schema default (agent/schema.py)
    4. the caller's own default argument

The environment stays on top, above the file the page writes. That looks
backwards for a settings page until you count what depends on it: 251
monkeypatch.setenv calls across tests/, and every WREN_X=... one-off run on the
command line. A file that outranked the environment would break both, silently.

It also makes the config/.env migration mandatory rather than optional. Any key
still set in .env wins over the page forever, so the page would show a field,
accept an edit, save it, and change nothing. agent/migrate_settings.py moves
those keys out, and STARTUP_WARNINGS names the ones still overlapping.

An empty string does not count as set. A key exported as "" is how a shell
unsets nothing in particular, and treating it as a real value pins the empty
string past every remaining layer.

Writes go through agent/store.py's locked() + atomic_write_json(): the mandated
primitive, already 0600 through mkstemp, and it adds the cross-process flock
that the chat server (threaded) and the launchd workers both need. The lock
covers the re-read as well as the write, per that module's own rule — CONFIG is
a global loaded at import, so a save that wrote it back whole would discard
whatever another process had written in the meantime. flush() merges instead.

Reads deliberately do NOT go through store.load_json(). Its corrupt-file
quarantine moves the file aside to <name>.corrupt-<ts>, which is right for a
machine-written store and wrong for one a person may hand-edit — the same
argument agent/prefs.py already makes for preferences.json. A file that will
not parse is logged and read as empty; nothing is moved.
"""

import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from agent import schema
from agent.store import atomic_write_json, locked

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent


def settings_path() -> Path:
    """Where the settings document lives — config/settings.json, beside the
    other stores.

    Resolved per call from an environment variable rather than pinned to a
    module constant, for the reason tests/conftest.py already resolves
    WREN_LOGS_DIR this way: monkeypatch stops at the process boundary, and the
    suite spawns a real child interpreter that has to inherit the redirect.
    """
    override = os.environ.get("WREN_SETTINGS_FILE")
    return Path(override) if override else _ROOT / "config" / "settings.json"


def env_path() -> Path:
    """Where config/.env lives. Same per-call resolution, same reason — plus
    the migration script and the startup warning both read it, and a test that
    could only point them at the developer's real credentials file would not be
    a test."""
    override = os.environ.get("WREN_ENV_FILE")
    return Path(override) if override else _ROOT / "config" / ".env"


def preferences_path() -> Path:
    """Where config/preferences.json lives — or lived. Nothing resolves through
    it any more; agent/migrate_settings.py is the only reader, and it uses this
    to find the file and rename it away. Same per-call resolution as the two
    above, so a test can drive the migration without pointing it at the
    developer's own sections."""
    override = os.environ.get("WREN_PREFERENCES_FILE")
    return Path(override) if override else _ROOT / "config" / "preferences.json"


# Fold config/.env into the process environment so layer 1 covers both a real
# environment variable and the file. load_dotenv does not override a variable
# that is already set, so a real one still wins over the file.
load_dotenv(env_path())


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #

# The shape on disk. Flat keys under "values"; whole preference sections under
# "preferences", so a section the user saves replaces the shipped one entirely
# rather than merging into it — a half-merged list of calendar categories is a
# worse answer than either version alone.
def _empty() -> dict:
    """A fresh empty document, inner dicts included.

    A function, not a module-level constant: flush() mutates the document it
    loads, and `dict(_EMPTY)` is a shallow copy — it would have handed out the
    same two inner dicts every time, so the first merge onto a document that
    does not exist yet would have written itself into every later load.
    """
    return {"values": {}, "preferences": {}}

# Overlaps between config/.env and the settings document, collected at load and
# surfaced twice: logged at WARNING by the chat server, and returned by
# GET /api/settings as a banner naming the keys whose fields are lying.
STARTUP_WARNINGS: list[str] = []


def _load() -> dict:
    path = settings_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _empty()
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
        # Logged, not quarantined. See the module docstring.
        logger.error(f"could not load settings from {path}: {e}")
        return _empty()
    if not isinstance(raw, dict):
        logger.error(f"settings file {path} is not a JSON object")
        return _empty()
    values = raw.get("values")
    prefs = raw.get("preferences")
    return {
        "values": values if isinstance(values, dict) else {},
        "preferences": prefs if isinstance(prefs, dict) else {},
    }


def _env_keys(path: Path) -> list[str]:
    """Key names assigned in `path`, in file order. Comments and blank lines
    skipped; the value is never read, because this is only ever used to say
    which keys exist there."""
    keys = []
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, UnicodeDecodeError, OSError):
        return keys
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name = line.split("=", 1)[0].strip()
        if name.startswith("export "):
            name = name[len("export "):].strip()
        if name and name not in keys:
            keys.append(name)
    return keys


def _compute_warnings(config: dict) -> list[str]:
    """The keys config/.env still sets that the page believes it owns.

    Tighter than "both files exist", which would fire forever: secrets stay in
    .env on purpose, and so does every key with no schema row. Only a
    non-secret key that HAS a row is a key whose field on the page is lying.
    """
    if not config.get("values") and not config.get("preferences"):
        return []
    overlap = sorted(
        k for k in _env_keys(env_path())
        if (row := schema.by_key(k)) is not None and not row.secret
    )
    if not overlap:
        return []
    return [
        f"config/.env still sets {overlap} — the environment outranks the "
        f"settings file, so edits to those fields on /settings will not take "
        f"effect. Run: .venv/bin/python -m agent.migrate_settings --apply"
    ]


CONFIG = _load()
STARTUP_WARNINGS[:] = _compute_warnings(CONFIG)


def reload() -> None:
    """Re-read the settings document from disk. Called after a save, so a live
    key lands in the running server without a restart."""
    global CONFIG
    CONFIG = _load()
    STARTUP_WARNINGS[:] = _compute_warnings(CONFIG)


# --------------------------------------------------------------------------- #
# Reading
# --------------------------------------------------------------------------- #

def getenv(key: str, default: str | None = None) -> str | None:
    """Resolve `key` through the four layers. See the module docstring."""
    value = os.environ.get(key)
    if value:
        return value
    value = CONFIG["values"].get(key)
    if isinstance(value, str) and value:
        return value
    value = schema.default_for(key)
    if value:
        return value
    return default


def source_of(key: str) -> str:
    """Which layer answered: env, file, default, or unset.

    The page renders a row sourced from env with a lock hint, because the
    environment wins and no save can override it.
    """
    if os.environ.get(key):
        return "env"
    if CONFIG["values"].get(key):
        return "file"
    if schema.default_for(key):
        return "default"
    return "unset"


def is_set(key: str) -> bool:
    """Whether `key` resolves to anything at all. The only thing the page is
    ever told about a secret — never the value, not even redacted."""
    return bool(getenv(key))


def preferences() -> dict:
    """The structured sections: the shipped defaults, then each section the user
    has saved through the page. A saved section replaces the shipped one whole;
    it is never merged into it, because a half-merged list of calendar
    categories is a worse answer than either version alone.

    config/preferences.json is not read here. agent/migrate_settings.py folded
    it into this document and renamed it to preferences.json.migrated, which is
    what stops a stale loader from finding it.
    """
    merged = dict(schema.STRUCTURED_DEFAULTS)
    for name, value in CONFIG["preferences"].items():
        if name in schema.PREFERENCE_SECTIONS and isinstance(value, dict):
            merged[name] = value
    return merged


# --------------------------------------------------------------------------- #
# Writing
# --------------------------------------------------------------------------- #

class ConfigError(Exception):
    """A staged write the schema refuses. Carries the message the page shows."""


def _staged() -> dict:
    """A deep-enough copy of CONFIG to stage edits into without touching the
    live document — so a rejected batch leaves the file byte-identical."""
    return {
        "values": dict(CONFIG["values"]),
        "preferences": {k: v for k, v in CONFIG["preferences"].items()},
    }


def set_value(staged: dict, key: str, value: str,
              allow_locked: bool = False) -> None:
    """Stage one flat key. Raises ConfigError for anything the schema will not
    accept; writes nothing to disk.

    `allow_locked` lifts the `editable` check only, and only for
    agent/migrate_settings.py. A locked row is locked against *editing* — the
    reason text always describes a change breaking something outside this
    process — but seven of them are set in config/.env today, and leaving them
    there would keep the environment outranking the document and the startup
    warning firing forever. The migration moves the value it already has; the
    page still refuses to change it, and every type, range and choice check
    below still runs.
    """
    row = schema.by_key(key)
    if row is None:
        raise ConfigError(f"{key} is not a known setting")
    if row.secret:
        raise ConfigError(f"{key} is a secret and is not editable here")
    if not row.editable and not allow_locked:
        raise ConfigError(f"{key} is not editable: {row.reason}")
    if not isinstance(value, str):
        raise ConfigError(f"{key} must be text")
    value = value.strip()
    if value == "":
        staged["values"].pop(key, None)
        return
    if row.type in ("int", "float"):
        try:
            number = int(value) if row.type == "int" else float(value)
        except ValueError:
            raise ConfigError(f"{key} must be a number")
        if row.minimum is not None and number < row.minimum:
            raise ConfigError(f"{key} must be at least {row.minimum}")
        if row.maximum is not None and number > row.maximum:
            raise ConfigError(f"{key} must be at most {row.maximum}")
    elif row.type == "bool":
        if value not in ("0", "1"):
            raise ConfigError(f"{key} must be 0 or 1")
    elif row.type == "choice":
        if value not in row.choices:
            raise ConfigError(f"{key} must be one of {list(row.choices)}")
    staged["values"][key] = value


def set_preference(staged: dict, name: str, value: dict) -> None:
    """Stage one whole preference section, fully checked. The section replaces
    its shipped counterpart; it is never merged into it.

    The shape check is schema.validate_section, the same function agent/prefs.py
    re-exports and the save route reports through. Running it here is what makes
    "validate everything, then write once" true of sections as well as of flat
    keys: before, this staged any object under a known name, and the two callers
    that checked first were the only thing standing between an emptied
    job_search list and a scout that silently matches nothing.
    """
    if name not in schema.PREFERENCE_SECTIONS:
        raise ConfigError(f"{name} is not a known preference section")
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be an object")
    problems = schema.validate_section(name, value)
    if problems:
        raise ConfigError("; ".join(problems))
    staged["preferences"][name] = value


def _written_elsewhere(current: dict) -> list[str]:
    """Names the document holds that differ from this process's CONFIG — someone
    else wrote them since we loaded.

    Worth a WARNING even though the merge below preserves them: the page that
    submitted this save rendered CONFIG, so the user was shown stale values and
    does not know it. A silent recovery is still a surprise.
    """
    names = {k for k in set(current["values"]) | set(CONFIG["values"])
             if current["values"].get(k) != CONFIG["values"].get(k)}
    names |= {n for n in set(current["preferences"]) | set(CONFIG["preferences"])
              if current["preferences"].get(n) != CONFIG["preferences"].get(n)}
    return sorted(names)


def flush(staged: dict, keys: list[str], sections: list[str]) -> list[str]:
    """Merge the staged edits onto the CURRENT document, under one lock.

    `staged` is this process's CONFIG plus the edits; `keys` and `sections` name
    what the caller actually touched. Only those are carried across — the rest of
    the staged copy is discarded, because CONFIG is a module global loaded at
    import and the file may have moved on since.

    That is not hypothetical, and it is why this no longer writes `staged` whole.
    agent/migrate_settings.py writes ~40 keys from a separate process and prints
    "restart the chat server"; nothing enforces it. Until the restart the running
    server's CONFIG is still the empty document it started with, so one unrelated
    save from /settings replaced the migrated file with that snapshot — and the
    values were already gone from config/.env by then, which is what made it a
    loss rather than a revert. agent/store.py states the rule this now follows:
    hold the lock across the whole read-modify-write.

    Returns the names whose value on disk actually changed, computed against the
    document as it really was rather than against CONFIG. A key absent from
    `staged["values"]` was cleared (set_value pops an empty value), so it is
    deleted from the merged document rather than left standing.
    """
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    changed: list[str] = []
    with locked(path):
        current = _load()
        arrived = _written_elsewhere(current)
        for key in keys:
            if staged["values"].get(key) != current["values"].get(key):
                changed.append(key)
            if key in staged["values"]:
                current["values"][key] = staged["values"][key]
            else:
                current["values"].pop(key, None)
        for name in sections:
            if staged["preferences"].get(name) != current["preferences"].get(name):
                changed.append(name)
            if name in staged["preferences"]:
                current["preferences"][name] = staged["preferences"][name]
            else:
                current["preferences"].pop(name, None)
        if changed:
            atomic_write_json(path, current)
    if arrived:
        logger.warning(
            f"settings changed outside this process since it loaded: {arrived} — "
            f"kept them and merged this save on top, but the page showed the "
            f"stale values. Restart to pick them up: {schema.RESTART_COMMAND}")
    # Always, even when nothing changed: the merge above is the moment this
    # process learns the document moved, and reloading is what makes the rest of
    # the run agree with the file it just wrote.
    reload()
    return changed


def apply(values: dict, prefs: dict) -> list[str]:
    """Validate everything, then write once. All-or-nothing.

    Returns the keys and section names that actually changed, so a re-save of
    an unchanged form raises no restart banner. On any failure it raises
    ConfigError having written nothing — a half-applied config is how a 4:30 AM
    task dies unattended.

    Both halves are fully checked. A flat value gets its type, range, choices,
    editable and secret rules; a preference section gets schema.validate_section
    on its contents, not just its name. Callers may still validate first to
    collect every problem for a form — chat/routes_settings.py does — but a
    caller that forgets is refused here instead of writing.
    """
    staged = _staged()
    for key, value in (values or {}).items():
        set_value(staged, key, value)
    for name, value in (prefs or {}).items():
        set_preference(staged, name, value)
    return flush(staged, list(values or {}), list(prefs or {}))
