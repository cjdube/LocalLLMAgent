"""Personal preferences: who the agent serves, and how.

Separates the personal part (name, positioning, calendar categories, job-search
terms) from the operational code that uses them, so a cloner edits data instead
of Python. Read once at import — tool-schema enums and digest regexes are built
at module import time and need the values then.

The values come from agent/config.py, which layers them: the shipped
config/preferences.example.json, then the pre-settings config/preferences.json,
then whichever sections have been saved through the /settings page. A saved
section replaces its predecessor whole; it is never merged into it, because a
half-merged list of calendar categories is a worse answer than either version
alone.

This module keeps its own shape. PREFS is still a module global that every
accessor reads, so the ~22 modules that bind `_NAME = prefs.user_name()` at
import are unchanged, and the tests that monkeypatch PREFS still work. reload()
is for the save route: a section saved from the page lands in the running
process without a restart.

validate_section() holds the invariants the shipped file has always had to
satisfy. It is the guard both ways — the test suite asserts through it, and the
save route rejects through it — so the page refuses an emptied job-search list
with the same message the test would print.
"""

from agent import config

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

# The job-search lists the opportunity scout builds its matchers from. Every one
# has to be a non-empty list of non-empty strings: an emptied list does not
# narrow the search, it silently matches nothing.
_JOB_SEARCH_LISTS = ("seniority_terms", "function_terms", "title_acronyms",
                     "hn_phrases", "states")

# Calendar roles with a consumer, plus the three kept as legacy. strava_download
# needs `fitness`; calendar_colorizer needs exactly one `fallback`.
_REQUIRED_CALENDAR_ROLES = ("work", "meetings", "appointments", "fitness")


def validate_section(name: str, value: dict) -> list[str]:
    """Problems with one preference section, as sentences a person can act on.

    Empty means the section is usable. The accessors above already degrade
    safely on a bad section — this is the layer that says so out loud, before a
    save lands, rather than letting the Scores block quietly go missing.

    Unknown section names return one problem rather than raising: the caller is
    a save route handling a form, and a name it does not know is a message to
    show, not a crash.
    """
    if name not in _VALIDATORS:
        return [f"{name} is not a known preference section"]
    if not isinstance(value, dict):
        return [f"{name} must be an object"]
    return _VALIDATORS[name](value)


def _validate_persona(value: dict) -> list[str]:
    return [f"persona.{field} is missing or empty"
            for field in ("user_name", "positioning", "engagement_model")
            if not value.get(field)]


def _validate_calendar(value: dict) -> list[str]:
    entries = value.get("categories")
    if not isinstance(entries, list) or not entries:
        return ["calendar.categories must be a non-empty list"]

    problems = []
    for i, category in enumerate(entries):
        if not isinstance(category, dict):
            problems.append(f"calendar.categories[{i}] is not an object")
            continue
        for field in ("name", "color_id", "color_name"):
            if not category.get(field):
                problems.append(f"calendar.categories[{i}].{field} is missing or empty")

    roles = [c.get("role") for c in entries if isinstance(c, dict) and c.get("role")]
    problems += [f"no calendar category has role {role!r}"
                 for role in _REQUIRED_CALENDAR_ROLES if role not in roles]
    if roles.count("fallback") != 1:
        problems.append("exactly one calendar category must have role 'fallback', "
                        f"found {roles.count('fallback')}")
    return problems


def _validate_job_search(value: dict) -> list[str]:
    problems = []
    for key in _JOB_SEARCH_LISTS:
        entries = value.get(key)
        if not isinstance(entries, list) or not entries:
            problems.append(f"job_search.{key} must be a non-empty list")
            continue
        if not all(isinstance(v, str) and v for v in entries):
            problems.append(f"job_search.{key} must hold non-empty strings")
    return problems


def _validate_projects(value: dict) -> list[str]:
    entries = value.get("instruction_files")
    if not isinstance(entries, list) or not entries:
        return ["projects.instruction_files must be a non-empty list"]
    # A bare filename, never a path: the scanner reads these from a project root
    # it does not otherwise trust, so a separator would widen that boundary.
    return [f"projects.instruction_files[{i}] must be a bare filename, not a path"
            for i, entry in enumerate(entries)
            if (not isinstance(entry, str) or not entry or entry in (".", "..")
                or "/" in entry or "\\" in entry)]


def _validate_morning_brief(value: dict) -> list[str]:
    hours = value.get("calendar_hours_ahead")
    if hours is None:
        return []
    if not isinstance(hours, int) or isinstance(hours, bool) or hours <= 0:
        return ["morning_brief.calendar_hours_ahead must be a positive whole "
                "number of hours"]
    return []


def _validate_sports(value: dict) -> list[str]:
    entries = value.get("teams")
    if entries is None or entries == []:
        return []  # no teams means the Scores block is off, which is allowed
    if not isinstance(entries, list):
        return ["sports.teams must be a list"]
    return [f"sports.teams[{i}] needs a league and an id"
            for i, team in enumerate(entries)
            if not isinstance(team, dict) or not team.get("league") or not team.get("id")]


def _validate_learnings(value: dict) -> list[str]:
    return []  # no consumer asserts a shape here yet


_VALIDATORS = {
    "persona": _validate_persona,
    "calendar": _validate_calendar,
    "job_search": _validate_job_search,
    "projects": _validate_projects,
    "morning_brief": _validate_morning_brief,
    "sports": _validate_sports,
    "learnings": _validate_learnings,
}
