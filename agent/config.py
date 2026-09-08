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
that the chat server (threaded) and the launchd workers both need.

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
    """Where the pre-settings config/preferences.json lives. Transitional; see
    _legacy_preferences below."""
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
_EMPTY: dict = {"values": {}, "preferences": {}}

# Overlaps between config/.env and the settings document, collected at load and
# surfaced twice: logged at WARNING by the chat server, and returned by
# GET /api/settings as a banner naming the keys whose fields are lying.
STARTUP_WARNINGS: list[str] = []


def _load() -> dict:
    path = settings_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return dict(_EMPTY)
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
        # Logged, not quarantined. See the module docstring.
        logger.error(f"could not load settings from {path}: {e}")
        return dict(_EMPTY)
    if not isinstance(raw, dict):
        logger.error(f"settings file {path} is not a JSON object")
        return dict(_EMPTY)
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


def _legacy_preferences() -> dict:
    """Sections still living in config/preferences.json, the file this document
    replaces.

    TRANSITIONAL. agent/migrate_settings.py folds this file into settings.json
    and renames it to preferences.json.migrated; the commit that adds the script
    deletes this function and the layer below it. Until then the file is the
    live source of the user's own persona, calendar, learnings and projects
    sections, and reading only the shipped defaults would quietly swap every
    one of them for the example file's placeholder values — a morning brief
    addressed to the wrong name.

    Whole sections only, and only known ones: the same filter the saved layer
    applies, so the two layers can never disagree about what a section is.
    """
    path = preferences_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
        # Logged, not quarantined — a hand-maintained file. Same rule as _load.
        logger.error(f"could not load preferences from {path}: {e}")
        return {}
    if not isinstance(raw, dict):
        logger.error(f"preferences file {path} is not a JSON object")
        return {}
    return {name: value for name, value in raw.items()
            if name in schema.PREFERENCE_SECTIONS and isinstance(value, dict)}


def preferences() -> dict:
    """The structured sections: the shipped defaults, then the pre-settings
    config/preferences.json, then each section the user has saved through the
    page. A section replaces its predecessor whole; it is never merged into it.

    The middle layer is transitional — see _legacy_preferences.
    """
    merged = dict(schema.STRUCTURED_DEFAULTS)
    merged.update(_legacy_preferences())
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


def set_value(staged: dict, key: str, value: str) -> None:
    """Stage one flat key. Raises ConfigError for anything the schema will not
    accept; writes nothing to disk."""
    row = schema.by_key(key)
    if row is None:
        raise ConfigError(f"{key} is not a known setting")
    if row.secret:
        raise ConfigError(f"{key} is a secret and is not editable here")
    if not row.editable:
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
    """Stage one whole preference section. The section replaces its shipped
    counterpart; it is never merged into it."""
    if name not in schema.PREFERENCE_SECTIONS:
        raise ConfigError(f"{name} is not a known preference section")
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be an object")
    staged["preferences"][name] = value


def flush(staged: dict) -> None:
    """Write the staged document, then reload. One lock, one atomic replace."""
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with locked(path):
        atomic_write_json(path, staged)
    reload()


def apply(values: dict, prefs: dict) -> list[str]:
    """Validate everything, then write once. All-or-nothing.

    Returns the keys and section names that actually changed, so a re-save of
    an unchanged form raises no restart banner. On any failure it raises
    ConfigError having written nothing — a half-applied config is how a 4:30 AM
    task dies unattended.
    """
    staged = _staged()
    for key, value in (values or {}).items():
        set_value(staged, key, value)
    for name, value in (prefs or {}).items():
        set_preference(staged, name, value)

    changed = [k for k in (values or {})
               if staged["values"].get(k) != CONFIG["values"].get(k)]
    changed += [n for n in (prefs or {})
                if staged["preferences"].get(n) != CONFIG["preferences"].get(n)]
    if changed:
        flush(staged)
    return changed
