"""Personal preferences loaded from config/preferences.json (gitignored, not a secret).

Separates who the agent serves (name, positioning, calendar categories, job-search
terms) from the operational code that uses them, so a cloner can edit one JSON
file instead of Python. Loaded once at import — tool-schema enums and digest
regexes are built at module import time and need the values then.

The real file is gitignored so personal details stay out of the repo;
config/preferences.example.json is the committed template and the fallback, so a
fresh clone boots with a valid schema before anyone has edited anything.

Deliberately not agent/store.py's load_json: its corrupt-file quarantine
os.replace()s the file aside, which is wrong for a hand-maintained file. A
missing or unparseable file degrades to {} — callers fall back to their coded
defaults.
"""

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
_PREFS_PATH = _ROOT / "config" / "preferences.json"
if not _PREFS_PATH.exists():
    # Fresh clone: nobody has made their own copy yet. The example file is the
    # same schema with generic values, so every consumer still gets valid data.
    _PREFS_PATH = _ROOT / "config" / "preferences.example.json"


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
        logger.error(f"could not load preferences from {path}: {e}")
        return {}
    if not isinstance(data, dict):
        logger.error(f"preferences file {path} is not a JSON object")
        return {}
    return data


PREFS = _load(_PREFS_PATH)


def section(name: str) -> dict:
    value = PREFS.get(name)
    return value if isinstance(value, dict) else {}


def persona() -> dict:
    return section("persona")


def user_name() -> str:
    return persona().get("user_name", "the user")


def calendar_categories() -> list:
    """Category entries with at least a name and color_id; malformed ones skipped."""
    entries = section("calendar").get("categories", [])
    if not isinstance(entries, list):
        return []
    return [c for c in entries
            if isinstance(c, dict) and c.get("name") and c.get("color_id")]


def category_color_by_role(role: str, default: str) -> str:
    """colorId of the first category tagged with `role`, decoupling operational
    lookups (fitness logging, colorizer fallback) from the personal category
    names, which a cloner is free to rename."""
    return next((c["color_id"] for c in calendar_categories()
                 if c.get("role") == role), default)


def job_search() -> dict:
    return section("job_search")
