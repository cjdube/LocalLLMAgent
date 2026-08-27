"""Tests for agent.tools.clickup — the read-only ClickUp backlog tools.

Every fixture below is shaped from a real captured response (a probe against the
live workspace on 2026-08-27), not invented: the status objects carry the
`type` field the closed-group filter depends on, timestamps are the string
milliseconds ClickUp actually sends, and `space` is the bare `{"id": ...}` the
task payload really contains rather than the fuller object the space endpoint
returns. No test here makes a network call — `_get` is stubbed in every case.
"""

import json

import pytest
import requests

from agent.tools import clickup

# --------------------------------------------------------------------------- #
# Captured shapes
# --------------------------------------------------------------------------- #

WREN_STATUSES = [
    {"status": "idea", "type": "open"},
    {"status": "designed", "type": "custom"},
    {"status": "building", "type": "custom"},
    {"status": "parked", "type": "custom"},
    {"status": "shipped", "type": "closed"},
]
BLOG_STATUSES = [
    {"status": "to do", "type": "open"},
    {"status": "in progress", "type": "custom"},
    {"status": "complete", "type": "closed"},
]

SPACES = {
    "spaces": [
        {"id": "90147276901", "name": "Wren", "statuses": WREN_STATUSES},
        {"id": "90147349460", "name": "Blog", "statuses": BLOG_STATUSES},
    ]
}
TEAMS = {"teams": [{"id": "90141551004", "name": "Craig Dube"}]}


def _task(name, status="idea", space="90147276901", updated="1787826143872",
          tags=(), priority="normal", description="", task_id="86bbnfav7"):
    return {
        "id": task_id,
        "name": name,
        "status": {"status": status, "type": "closed" if status in ("shipped", "complete") else "open"},
        "date_created": "1787826125157",
        "date_updated": updated,
        "priority": {"priority": priority} if priority else None,
        "tags": [{"name": t} for t in tags],
        "space": {"id": space},
        "description": description,
        "text_content": description,
        "url": f"https://app.clickup.com/t/{task_id}",
    }


@pytest.fixture
def stub(monkeypatch):
    """Stub clickup._get, recording every call so a test can assert on the
    query ClickUp was actually asked, not just on what came back."""
    calls = []
    routes = {"tasks": [{"tasks": [], "last_page": True}]}

    def _get(path, token, **params):
        calls.append((path, params))
        if path == "/team":
            return TEAMS
        if path.endswith("/space"):
            return SPACES
        if path.endswith("/task"):
            pages = routes["tasks"]
            return pages[min(params.get("page", 0), len(pages) - 1)]
        if "/comment" in path:
            return routes.get("comments", {"comments": []})
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr(clickup, "_get", _get)
    monkeypatch.setattr(clickup, "resolve_key", lambda name, arg=None: arg or "pk_test")
    return type("Stub", (), {"calls": calls, "routes": routes})()


def _set_tasks(stub, tasks, last_page=True):
    stub.routes["tasks"] = [{"tasks": tasks, "last_page": last_page}]


# --------------------------------------------------------------------------- #
# _slug — the forgiving match that keeps ClickUp ids away from the model
# --------------------------------------------------------------------------- #

def test_slug_ignores_case_spacing_and_punctuation():
    assert clickup._slug("Vibe Foundry") == "vibefoundry"
    assert clickup._slug("vibe-foundry") == "vibefoundry"
    assert clickup._slug("  VIBE  FOUNDRY  ") == "vibefoundry"


def test_slug_handles_none():
    assert clickup._slug(None) == ""


# --------------------------------------------------------------------------- #
# Timestamps. ClickUp sends UTC milliseconds; the day we report is the LOCAL
# one. These pin an evening stamp, where the two dates disagree — the case that
# a naive slice of an ISO string passes anyway.
# --------------------------------------------------------------------------- #

def test_ms_to_local_date_converts_utc_to_the_local_day(monkeypatch):
    monkeypatch.setenv("TIMEZONE", "America/New_York")
    # 2026-08-27T01:30:00Z is still the 26th in New York.
    ms = 1787794200000
    assert clickup._ms_to_local_date(ms) == "2026-08-26"
    assert clickup._ms_to_local_date(str(ms)) == "2026-08-26"


def test_ms_to_local_date_same_instant_differs_by_zone(monkeypatch):
    ms = 1787794200000
    monkeypatch.setenv("TIMEZONE", "UTC")
    assert clickup._ms_to_local_date(ms) == "2026-08-27"
    monkeypatch.setenv("TIMEZONE", "America/New_York")
    assert clickup._ms_to_local_date(ms) == "2026-08-26"


def test_ms_to_local_date_tolerates_missing_and_junk():
    assert clickup._ms_to_local_date(None) is None
    assert clickup._ms_to_local_date("") is None
    assert clickup._ms_to_local_date("not-a-number") is None


def test_days_since_counts_in_local_days(monkeypatch):
    from datetime import date
    assert clickup._days_since("2026-08-20", today=date(2026, 8, 27)) == 7
    assert clickup._days_since(None) is None


# --------------------------------------------------------------------------- #
# Area and status resolution
# --------------------------------------------------------------------------- #

def _areas():
    return [
        {"area": "wren", "name": "Wren", "id": "90147276901", "statuses": WREN_STATUSES},
        {"area": "blog", "name": "Blog", "id": "90147349460", "statuses": BLOG_STATUSES},
    ]


def test_resolve_area_returns_all_when_none_named():
    assert len(clickup._resolve_area(_areas(), None)) == 2


def test_resolve_area_is_forgiving_about_spelling():
    assert clickup._resolve_area(_areas(), "WREN")[0]["id"] == "90147276901"


def test_resolve_area_unknown_names_the_real_areas():
    with pytest.raises(clickup._ClickUpError) as e:
        clickup._resolve_area(_areas(), "nope")
    assert "wren" in str(e.value) and "blog" in str(e.value)


def test_resolve_status_returns_canonical_spelling():
    assert clickup._resolve_status(_areas(), "PARKED") == "parked"


def test_resolve_status_is_scoped_to_the_chosen_area():
    """'parked' exists in Wren but not in Blog. Validating per-area is the whole
    point: without it a Blog filter for 'parked' returns an empty list, which
    reads as 'nothing is parked' rather than 'that status does not exist here'."""
    blog = [a for a in _areas() if a["area"] == "blog"]
    with pytest.raises(clickup._ClickUpError) as e:
        clickup._resolve_status(blog, "parked")
    assert "to do" in str(e.value) and "complete" in str(e.value)
    assert "parked" not in str(e.value).split("Statuses:")[1]


# --------------------------------------------------------------------------- #
# list_backlog
# --------------------------------------------------------------------------- #

def test_list_backlog_returns_rows_and_the_area_names(stub):
    _set_tasks(stub, [_task("Add an audio voice", status="parked", tags=["ux"], priority="low")])
    out = clickup.list_backlog()
    assert out["item_count"] == 1
    assert out["areas"] == ["wren", "blog"]
    row = out["items"][0]
    assert row["title"] == "Add an audio voice"
    assert row["area"] == "wren"
    assert row["status"] == "parked"
    assert row["tags"] == ["ux"]
    assert row["priority"] == "low"


def test_list_backlog_never_leaks_a_clickup_id(stub):
    """The model must never be handed an identifier it would have to copy back
    — read_backlog_item takes a title instead (docs/opaque-identifiers.md)."""
    _set_tasks(stub, [_task("Improve UX of map", task_id="86bbnfav7")])
    blob = json.dumps(clickup.list_backlog())
    assert "86bbnfav7" not in blob
    assert "90147276901" not in blob


def test_list_backlog_excludes_done_by_default_and_says_so(stub):
    _set_tasks(stub, [_task("Shipped thing", status="shipped")])
    out = clickup.list_backlog()
    task_calls = [p for path, p in stub.calls if path.endswith("/task")]
    assert "include_closed" not in task_calls[0]
    assert "include_done" in out["note"]


def test_list_backlog_include_done_asks_clickup_for_closed_items(stub):
    _set_tasks(stub, [_task("Shipped thing", status="shipped")])
    out = clickup.list_backlog(include_done=True)
    task_calls = [p for path, p in stub.calls if path.endswith("/task")]
    assert task_calls[0]["include_closed"] == "true"
    assert "note" not in out


def test_list_backlog_scopes_the_query_to_one_space(stub):
    _set_tasks(stub, [])
    clickup.list_backlog(area="blog")
    task_calls = [p for path, p in stub.calls if path.endswith("/task")]
    assert task_calls[0]["space_ids[]"] == ["90147349460"]


def test_list_backlog_filters_by_status(stub):
    _set_tasks(stub, [_task("A", status="parked"), _task("B", status="idea")])
    out = clickup.list_backlog(area="wren", status="parked")
    assert [r["title"] for r in out["items"]] == ["A"]


def test_list_backlog_bad_status_errors_instead_of_returning_nothing(stub):
    _set_tasks(stub, [_task("A", status="parked")])
    out = clickup.list_backlog(area="blog", status="parked")
    assert "no status 'parked'" in out["error"]


def test_list_backlog_sorts_most_recently_updated_first(stub):
    _set_tasks(stub, [
        _task("older", updated="1787000000000"),
        _task("newer", updated="1787826143872"),
    ])
    assert [r["title"] for r in clickup.list_backlog()["items"]] == ["newer", "older"]


def test_list_backlog_char_budget_drops_rows_and_admits_it(stub):
    """A row cap would not catch this: 200 items are all short, and it is their
    total size that blows the loop's context, not their number."""
    _set_tasks(stub, [_task(f"Backlog item number {i} with a fairly wordy title", tags=["feature", "chore"])
                      for i in range(200)])
    out = clickup.list_backlog()
    assert out["item_count"] == 200
    assert out["items_shown"] < 200
    assert "Do not say these are all of them" in out["partial"]


def test_list_backlog_worst_case_fits_under_the_loop_cap(stub):
    """Pins the budget below agent.loop's default result cap, so the loop never
    has to trim a payload this tool already trimmed on purpose."""
    from agent.loop import MAX_TOOL_RESULT_CHARS, TOOL_RESULT_CHAR_CAPS
    _set_tasks(stub, [_task(f"A very long backlog item title number {i} " + "x" * 60,
                            tags=["feature", "chore", "ux"]) for i in range(300)])
    payload = json.dumps(clickup.list_backlog())
    assert len(payload) < TOOL_RESULT_CHAR_CAPS.get("list_backlog", MAX_TOOL_RESULT_CHARS)


def test_list_backlog_walks_every_page(stub):
    stub.routes["tasks"] = [
        {"tasks": [_task(f"p0-{i}") for i in range(clickup._PAGE_SIZE)], "last_page": False},
        {"tasks": [_task("p1-0")], "last_page": True},
    ]
    assert clickup.list_backlog()["item_count"] == clickup._PAGE_SIZE + 1


def test_list_backlog_without_a_token_says_which_key(monkeypatch):
    monkeypatch.setattr(clickup, "resolve_key", lambda name, arg=None: None)
    assert "CLICKUP_API_TOKEN" in clickup.list_backlog()["error"]


def test_list_backlog_degrades_on_a_dead_api(stub, monkeypatch):
    def _boom(*a, **k):
        raise requests.exceptions.ConnectionError("no route to host")
    monkeypatch.setattr(clickup, "_get", _boom)
    out = clickup.list_backlog()
    assert "error" in out and "network error" in out["error"]


def test_two_workspaces_refuses_rather_than_guessing(stub):
    """Picking the first of two silently answers about the wrong workspace."""
    def _get(path, token, **params):
        if path == "/team":
            return {"teams": [{"id": "1", "name": "One"}, {"id": "2", "name": "Two"}]}
        raise AssertionError("should not get past /team")
    stub.calls.clear()
    import agent.tools.clickup as mod
    orig = mod._get
    mod._get = _get
    try:
        out = clickup.list_backlog()
    finally:
        mod._get = orig
    assert "2 ClickUp workspaces" in out["error"]


# --------------------------------------------------------------------------- #
# read_backlog_item
# --------------------------------------------------------------------------- #

def test_read_backlog_item_matches_a_partial_title(stub):
    _set_tasks(stub, [_task("Wren Gemini Notebook sync - script solution",
                            description="A description.")])
    out = clickup.read_backlog_item("gemini notebook")
    assert out["title"] == "Wren Gemini Notebook sync - script solution"
    assert out["description"] == "A description."
    assert out["url"].startswith("https://app.clickup.com/t/")


def test_read_backlog_item_prefers_an_exact_title_over_a_substring(stub):
    _set_tasks(stub, [_task("Logging", task_id="a1"),
                      _task("Logging rotation for launchd", task_id="a2")])
    assert clickup.read_backlog_item("logging")["title"] == "Logging"


def test_read_backlog_item_ambiguous_returns_candidates(stub):
    _set_tasks(stub, [_task("Logging rotation", task_id="a1"),
                      _task("Logging for launchd", task_id="a2")])
    out = clickup.read_backlog_item("logging")
    assert "matches 2 items" in out["error"]
    assert len(out["candidates"]) == 2


def test_read_backlog_item_unknown_title_points_at_the_listing(stub):
    _set_tasks(stub, [_task("Something else")])
    assert "list_backlog" in clickup.read_backlog_item("does not exist")["error"]


def test_read_backlog_item_rejects_an_empty_title(stub):
    assert "must not be empty" in clickup.read_backlog_item("  ")["error"]


def test_read_backlog_item_can_reach_a_shipped_item(stub):
    """The default listing hides the closed group, so reading one by name has
    to ask for closed items or a shipped item is unreachable."""
    _set_tasks(stub, [_task("Shipped thing", status="shipped")])
    out = clickup.read_backlog_item("shipped thing")
    task_calls = [p for path, p in stub.calls if path.endswith("/task")]
    assert task_calls[0]["include_closed"] == "true"
    assert out["status"] == "shipped"


def test_read_backlog_item_trims_a_long_description_and_says_so(stub):
    _set_tasks(stub, [_task("Big one", description="word " * 2000)])
    out = clickup.read_backlog_item("big one")
    assert len(out["description"]) <= clickup._MAX_DESCRIPTION_CHARS + 1
    assert "not shown" in out["description_truncated"]


def test_read_backlog_item_returns_comments(stub):
    _set_tasks(stub, [_task("With comments")])
    stub.routes["comments"] = {"comments": [
        {"comment_text": "first note", "date": "1787826143872"},
        {"comment_text": "second note", "date": "1787826143872"},
    ]}
    out = clickup.read_backlog_item("with comments")
    assert [c["text"] for c in out["comments"]] == ["first note", "second note"]
    assert out["comments"][0]["date"]


def test_read_backlog_item_survives_a_comment_fetch_failure(stub, monkeypatch):
    """A failing comments call costs the comments, not the item."""
    _set_tasks(stub, [_task("Still readable", description="body")])
    real = clickup._get

    def _get(path, token, **params):
        if "/comment" in path:
            raise requests.exceptions.Timeout("slow")
        return real(path, token, **params)

    monkeypatch.setattr(clickup, "_get", _get)
    out = clickup.read_backlog_item("still readable")
    assert out["description"] == "body"
    assert out["comments"] == []
    assert out["comments_error"]


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #

def test_both_tools_are_registered_and_read_only():
    from agent import toolset
    for name in ("list_backlog", "read_backlog_item"):
        assert name in toolset.DISPATCH
        assert name in toolset.TOOL_GROUP_NAMES["backlog"]
        assert name not in toolset.WRITE_TOOLS
        assert name not in toolset.CONSEQUENTIAL_TOOLS


def test_tool_descriptions_deny_pretraining():
    """A catalogue tool that doesn't say the list is unknowable gets skipped,
    and the model invents entries instead (docs/model-constraints.md)."""
    for schema in clickup.BACKLOG_TOOL_SCHEMAS:
        text = schema["function"]["description"].lower()
        assert "not something you know" in text or "only items list_backlog returns exist" in text
