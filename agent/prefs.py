"""Personal preferences: who the agent serves, and how.

Separates the personal part (name, positioning, calendar categories, job-search
terms) from the operational code that uses them, so a cloner edits data instead
of Python. Read once at import — tool-schema enums and digest regexes are built
at module import time and need the values then.

The values come from agent/config.py, which layers them: the shipped
config/preferences.example.json, then whichever sections have been saved
through the /settings page. A saved section replaces the shipped one whole; it
is never merged into it, because a half-merged list of calendar categories is a
worse answer than either version alone.

config/preferences.json is gone — agent/migrate_settings.py folded it into the
settings document and renamed it to preferences.json.migrated.

This module keeps its own shape. PREFS is still a module global that every
accessor reads, so the ~22 modules that bind `_NAME = prefs.user_name()` at
import are unchanged, and the tests that monkeypatch PREFS still work. reload()
is for the save route: a section saved from the page lands in the running
process without a restart.

validate_section() holds the invariants the shipped file has always had to
satisfy. It is the guard both ways — the test suite asserts through it, and the
save route rejects through it — so the page refuses an emptied job-search list
with the same message the test would print. It is re-exported from
agent/schema.py, which is where every save now runs it; see the note above it.
"""

from agent import config, schema

PREFS = config.preferences()


def reload() -> None:
    """Re-read the sections after a save, so a change from the page reaches this
    process without a restart. Values bound at import (a tool-schema enum, a
    digest regex) still need one — that is what the schema row's `applies` says.
    """
    global PREFS
    PREFS = config.preferences()


_DEFAULT_PROJECT_INSTRUCTION_FILES = ("AGENTS.md",)


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


def brief_calendar_hours(default: int = 48) -> int:
    """How far ahead the morning brief's calendar section looks. A missing or
    non-positive value falls back to `default` — a bad edit should shorten
    nothing, since an empty calendar section reads like a quiet day."""
    value = section("morning_brief").get("calendar_hours_ahead")
    return value if isinstance(value, int) and value > 0 else default


def followed_teams() -> list:
    """Sports teams whose previous-day scores appear in the morning brief.

    Entries need a league and an ESPN team id; malformed ones are skipped the
    way calendar_categories() skips its own, so a bad hand-edit costs one team
    rather than the whole Scores section. An empty list means the feature is
    simply off — nothing downstream treats it as an error."""
    entries = section("sports").get("teams", [])
    if not isinstance(entries, list):
        return []
    return [t for t in entries
            if isinstance(t, dict) and t.get("league") and t.get("id")]


def job_search() -> dict:
    return section("job_search")


def project_instruction_files() -> tuple[str, ...]:
    """Ordered, root-level filenames the project scanner may read.

    Invalid entries are dropped rather than allowed to widen the scanner's
    explicit-file boundary. An absent or unusable list uses the cross-harness
    default.
    """
    entries = section("projects").get("instruction_files")
    if not isinstance(entries, list):
        return _DEFAULT_PROJECT_INSTRUCTION_FILES

    safe = []
    for entry in entries:
        if (not isinstance(entry, str) or not entry or entry in (".", "..")
                or "/" in entry or "\\" in entry):
            continue
        if entry not in safe:
            safe.append(entry)
    return tuple(safe) or _DEFAULT_PROJECT_INSTRUCTION_FILES


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

# validate_section moved to agent/schema.py, next to PREFERENCE_SECTIONS, so
# that agent/config.py can call it on every save. config cannot import this
# module — this module imports config — which is why the check used to live
# out here where only the two callers that remembered it ran it.
#
# Re-exported, not forwarded: tests/test_prefs.py, chat/routes_settings.py and
# agent/migrate_settings.py all reach it through this name, and it is still a
# preferences concept even though it is defined one module over.
validate_section = schema.validate_section
