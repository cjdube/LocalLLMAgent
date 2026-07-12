"""Tests for agent/prefs.py and the contract of the shipped preferences file.

The shipped config/preferences.json is committed data that several modules
consume at import time; these tests are the schema guard that keeps an edit
from silently breaking a consumer.
"""

import json

from agent import prefs


# ---- shipped file satisfies every consumer's contract ----------------------

def test_shipped_file_parses():
    assert isinstance(prefs.PREFS, dict) and prefs.PREFS, \
        "config/preferences.json failed to load"


def test_persona_complete():
    persona = prefs.persona()
    for field in ("user_name", "positioning", "engagement_model"):
        assert persona.get(field), f"persona.{field} missing or empty"


def test_calendar_categories_complete():
    categories = prefs.calendar_categories()
    assert categories, "no valid calendar categories"
    for c in categories:
        for field in ("name", "color_id", "color_name"):
            assert c.get(field), f"category {c} missing {field}"


def test_calendar_roles_present():
    roles = [c.get("role") for c in prefs.calendar_categories() if c.get("role")]
    # weekly_learnings needs work/meetings/appointments, daily_log needs
    # fitness, calendar_colorizer needs exactly one fallback.
    for role in ("work", "meetings", "appointments", "fitness"):
        assert role in roles, f"no category has role {role!r}"
    assert roles.count("fallback") == 1, "exactly one category must have role 'fallback'"


def test_job_search_lists_nonempty():
    job_search = prefs.job_search()
    for key in ("seniority_terms", "function_terms", "title_acronyms",
                "hn_phrases", "states"):
        values = job_search.get(key)
        assert isinstance(values, list) and values, f"job_search.{key} missing or empty"
        assert all(isinstance(v, str) and v for v in values), \
            f"job_search.{key} has non-string entries"


def test_location_present():
    assert prefs.PREFS.get("location")


# ---- loader degradation -----------------------------------------------------

def test_load_missing_file_returns_empty(tmp_path):
    assert prefs._load(tmp_path / "nope.json") == {}


def test_load_invalid_json_returns_empty_and_keeps_file(tmp_path):
    bad = tmp_path / "preferences.json"
    bad.write_text("{not json")
    assert prefs._load(bad) == {}
    # Unlike store.load_json, no quarantine rename — the file stays put.
    assert bad.exists() and bad.read_text() == "{not json"


def test_load_non_object_returns_empty(tmp_path):
    lst = tmp_path / "preferences.json"
    lst.write_text(json.dumps([1, 2]))
    assert prefs._load(lst) == {}


# ---- helper fallbacks -------------------------------------------------------

def test_helpers_degrade_on_empty_prefs(monkeypatch):
    monkeypatch.setattr(prefs, "PREFS", {})
    assert prefs.section("calendar") == {}
    assert prefs.user_name() == "the user"
    assert prefs.calendar_categories() == []
    assert prefs.category_color_by_role("fallback", "11") == "11"
    assert prefs.job_search() == {}


def test_category_color_by_role():
    assert prefs.category_color_by_role("fitness", "0") == "4"
    assert prefs.category_color_by_role("no-such-role", "0") == "0"
