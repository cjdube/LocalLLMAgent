"""Tests for agent/prefs.py and the contract every preference section must meet.

The sections several modules consume at import time now arrive through
agent/config.py, layered: the shipped config/preferences.example.json, then the
pre-settings config/preferences.json, then whatever the /settings page has
saved. The invariants that used to live as assertions in this file live in
prefs.validate_section() instead, so the save route rejects a bad section with
the same words this file would print — a test the page cannot disagree with.

The suite pins WREN_PREFERENCES_FILE at a path that does not exist (see
tests/conftest.py), so what these tests read is the shipped example file.
"""

from agent import prefs, schema


# ---- shipped sections satisfy every consumer's contract --------------------

def test_shipped_file_parses():
    assert isinstance(prefs.PREFS, dict) and prefs.PREFS, \
        "the shipped preferences failed to load"


def test_every_shipped_section_passes_its_own_validator():
    # The old per-section assertions, now asserted through the function the save
    # route uses. Every section at once, so a new section with no validator
    # cannot slip past by simply not having a test written for it.
    for name in schema.PREFERENCE_SECTIONS:
        problems = prefs.validate_section(name, prefs.PREFS.get(name, {}))
        assert not problems, f"shipped {name} section: {problems}"


def test_every_shipped_section_has_a_validator():
    # Guard on the guard above: a section absent from _VALIDATORS would make
    # validate_section return "not a known preference section" — which the loop
    # above would catch — but a section added to _VALIDATORS and NOT to the
    # schema would go unchecked in the other direction.
    assert set(prefs._VALIDATORS) == set(schema.PREFERENCE_SECTIONS)


# ---- validate_section actually bites ---------------------------------------
#
# Without these, a validator that returned [] unconditionally would keep every
# assertion above green forever. Each case breaks exactly one invariant that a
# real consumer depends on.

def test_persona_needs_all_three_fields():
    assert prefs.validate_section("persona", {"user_name": "A", "positioning": "B"}) \
        == ["persona.engagement_model is missing or empty"]


def test_calendar_needs_exactly_one_fallback():
    # calendar_colorizer picks the first fallback; two make the choice arbitrary
    # and none makes it crash.
    two = {"categories": [
        {"name": "a", "color_id": "1", "color_name": "x", "role": "fallback"},
        {"name": "b", "color_id": "2", "color_name": "y", "role": "fallback"},
    ]}
    problems = prefs.validate_section("calendar", two)
    assert any("exactly one" in p for p in problems), problems


def test_calendar_needs_the_roles_its_consumers_look_up():
    # strava_download looks up 'fitness'; work/meetings/appointments are pinned
    # as legacy (see docs/preferences.md).
    one = {"categories": [
        {"name": "a", "color_id": "1", "color_name": "x", "role": "fallback"}]}
    problems = prefs.validate_section("calendar", one)
    for role in ("work", "meetings", "appointments", "fitness"):
        assert f"no calendar category has role {role!r}" in problems


def test_calendar_category_needs_its_display_fields():
    missing = {"categories": [{"name": "a", "role": "fallback"}]}
    problems = prefs.validate_section("calendar", missing)
    assert "calendar.categories[0].color_id is missing or empty" in problems
    assert "calendar.categories[0].color_name is missing or empty" in problems


def test_job_search_rejects_an_emptied_list():
    # An emptied list does not narrow the scout's search, it silently matches
    # nothing — the whole reason this is a refusal and not a warning.
    full = {key: ["x"] for key in prefs._JOB_SEARCH_LISTS}
    assert prefs.validate_section("job_search", full) == []
    emptied = dict(full, states=[])
    assert prefs.validate_section("job_search", emptied) == \
        ["job_search.states must be a non-empty list"]


def test_job_search_rejects_non_string_entries():
    full = dict({key: ["x"] for key in prefs._JOB_SEARCH_LISTS}, hn_phrases=["ok", 7])
    assert prefs.validate_section("job_search", full) == \
        ["job_search.hn_phrases must hold non-empty strings"]


def test_projects_rejects_a_path():
    # The scanner reads these from a project root it does not otherwise trust,
    # so a separator would widen that boundary.
    problems = prefs.validate_section("projects", {"instruction_files": ["../x.md"]})
    assert problems == ["projects.instruction_files[0] must be a bare filename, not a path"]


def test_morning_brief_rejects_a_window_of_zero():
    assert prefs.validate_section("morning_brief", {"calendar_hours_ahead": 0})
    assert prefs.validate_section("morning_brief", {"calendar_hours_ahead": "48"})
    assert prefs.validate_section("morning_brief", {}) == []


def test_sports_allows_no_teams_but_not_a_broken_one():
    # No teams means the Scores block is off, which is a choice, not a fault.
    assert prefs.validate_section("sports", {"teams": []}) == []
    assert prefs.validate_section("sports", {"teams": [{"league": "mlb"}]}) == \
        ["sports.teams[0] needs a league and an id"]


def test_an_unknown_section_is_a_message_not_a_crash():
    # The caller is a save route handling a form.
    assert prefs.validate_section("nope", {}) == ["nope is not a known preference section"]
    assert prefs.validate_section("persona", "not a dict") == ["persona must be an object"]


def test_the_location_left_the_sections_for_a_schema_row():
    # It is a string, not a section, so it could never be one. It is
    # DEFAULT_LOCATION now, resolved through the same four layers as every other
    # key — which also collapsed the hand-rolled fallback in morning_brief and
    # weather.
    assert "location" not in prefs.PREFS
    assert schema.by_key("DEFAULT_LOCATION") is not None


# ---- morning_brief.calendar_hours_ahead ------------------------------------

def test_brief_calendar_hours_reads_configured_value(monkeypatch):
    monkeypatch.setattr(prefs, "PREFS", {"morning_brief": {"calendar_hours_ahead": 72}})
    assert prefs.brief_calendar_hours() == 72


def test_brief_calendar_hours_defaults_when_absent(monkeypatch):
    monkeypatch.setattr(prefs, "PREFS", {})
    assert prefs.brief_calendar_hours() == 48
    assert prefs.brief_calendar_hours(24) == 24


def test_brief_calendar_hours_rejects_unusable_values(monkeypatch):
    # A bad edit must not shorten the window to nothing: an empty Calendar
    # section reads like a quiet day rather than like a broken config.
    for bad in (0, -12, "48", None, 12.5):
        monkeypatch.setattr(prefs, "PREFS", {"morning_brief": {"calendar_hours_ahead": bad}})
        assert prefs.brief_calendar_hours() == 48, f"{bad!r} should have fallen back"


# ---- sports.teams -----------------------------------------------------------

def test_followed_teams_reads_shipped_file():
    for team in prefs.followed_teams():
        for field in ("league", "id", "name"):
            assert team.get(field), f"team {team} missing {field}"


def test_followed_teams_skips_malformed_entries(monkeypatch):
    # A bad hand-edit must cost one team, not the whole Scores section.
    monkeypatch.setattr(prefs, "PREFS", {"sports": {"teams": [
        {"league": "mlb", "id": "2", "name": "Red Sox"},
        {"league": "mlb", "name": "no id"},
        {"id": "9", "name": "no league"},
        "not a dict",
    ]}})
    assert [t["name"] for t in prefs.followed_teams()] == ["Red Sox"]


def test_followed_teams_absent_or_unusable_is_empty(monkeypatch):
    # Absent means the feature is off, not broken — no error anywhere downstream.
    for value in ({}, {"sports": {}}, {"sports": {"teams": "nope"}}):
        monkeypatch.setattr(prefs, "PREFS", value)
        assert prefs.followed_teams() == []


# ---- projects.instruction_files --------------------------------------------

def test_project_instruction_files_default_to_agents_md(monkeypatch):
    monkeypatch.setattr(prefs, "PREFS", {})
    assert prefs.project_instruction_files() == ("AGENTS.md",)


def test_project_instruction_files_preserve_order_and_remove_duplicates(monkeypatch):
    monkeypatch.setattr(prefs, "PREFS", {"projects": {"instruction_files": [
        "PRIMARY.md", "SECONDARY.md", "PRIMARY.md",
    ]}})
    assert prefs.project_instruction_files() == ("PRIMARY.md", "SECONDARY.md")


def test_project_instruction_files_reject_paths_and_malformed_entries(monkeypatch):
    monkeypatch.setattr(prefs, "PREFS", {"projects": {"instruction_files": [
        "../outside.md", "nested/file.md", "nested\\file.md", "", None, "SAFE.md",
    ]}})
    assert prefs.project_instruction_files() == ("SAFE.md",)


def test_project_instruction_files_fall_back_when_no_entry_is_safe(monkeypatch):
    for value in (None, "AGENTS.md", [], ["../outside.md"]):
        monkeypatch.setattr(prefs, "PREFS", {"projects": {"instruction_files": value}})
        assert prefs.project_instruction_files() == ("AGENTS.md",)


# ---- reload -----------------------------------------------------------------

def test_reload_picks_up_a_section_saved_since_import(monkeypatch):
    # What the save route calls, so a section edited on the page reaches this
    # process without a restart. Values bound at import still need one.
    monkeypatch.setattr(prefs, "PREFS", {})
    prefs.reload()
    assert prefs.PREFS.get("persona"), "reload did not re-read the sections"


# ---- helper fallbacks -------------------------------------------------------

def test_helpers_degrade_on_empty_prefs(monkeypatch):
    monkeypatch.setattr(prefs, "PREFS", {})
    assert prefs.section("calendar") == {}
    assert prefs.user_name() == "the user"
    assert prefs.calendar_categories() == []
    assert prefs.category_color_by_role("fallback", "11") == "11"
    assert prefs.job_search() == {}
    assert prefs.project_instruction_files() == ("AGENTS.md",)


def test_category_color_by_role():
    assert prefs.category_color_by_role("fitness", "0") == "4"
    assert prefs.category_color_by_role("no-such-role", "0") == "0"
