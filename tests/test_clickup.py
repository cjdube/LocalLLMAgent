"""Tests for agent.tools.clickup — the ClickUp Space/List/Task tools.

Every fixture below is shaped from a real captured response (a probe against the
live workspace on 2026-08-27), not invented: the status objects carry the
`type` field the closed-group filter depends on, timestamps are the string
milliseconds ClickUp actually sends, and `space` is the bare `{"id": ...}` the
task payload really contains rather than the fuller object the space endpoint
returns. No test here makes a network call — `_get` is stubbed in every case.
"""

import json
import time
from datetime import datetime, timedelta

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
          tags=(), priority="normal", description="", task_id="86bbnfav7",
          list_name="Backlog", list_id="901419409898"):
    return {
        "id": task_id,
        "name": name,
        "list": {"id": list_id, "name": list_name},
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
            # clickup_digest makes two task calls with different windows. The
            # one that asks for closed items reads its own route when a test
            # sets one, so a test can give the two calls different answers.
            key = "tasks_with_done" if params.get("include_closed") and "tasks_with_done" in routes else "tasks"
            pages = routes[key]
            return pages[min(params.get("page", 0), len(pages) - 1)]
        if path.endswith("/list"):
            return routes.get("lists", {"lists": [{"id": "901419409898", "name": "Backlog"}]})
        if path.endswith("/folder"):
            return routes.get("folders", {"folders": []})
        if "/comment" in path:
            return routes.get("comments", {"comments": []})
        raise AssertionError(f"unexpected path {path}")

    writes = []

    def _write(method, path, token, payload):
        writes.append((method, path, payload))
        return routes.get("write_result", {
            "id": "newid", "name": payload.get("name", ""),
            "status": {"status": payload.get("status", "")},
            "url": "https://app.clickup.com/t/newid",
        })

    monkeypatch.setattr(clickup, "_get", _get)
    monkeypatch.setattr(clickup, "_write", _write)
    monkeypatch.setattr(clickup, "resolve_key", lambda name, arg=None: arg or "pk_test")
    return type("Stub", (), {"calls": calls, "routes": routes, "writes": writes})()


def _set_tasks(stub, tasks, last_page=True):
    stub.routes["tasks"] = [{"tasks": tasks, "last_page": last_page}]


def _set_moved(stub, tasks):
    """The answer to the include_closed call only — what clickup_digest sees as
    "moved", as opposed to what it sees as currently open."""
    stub.routes["tasks_with_done"] = [{"tasks": tasks, "last_page": True}]


def _ms_days_ago(n: int) -> int:
    return int((datetime.now() - timedelta(days=n)).timestamp() * 1000)


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
# Space and status resolution
# --------------------------------------------------------------------------- #

def _spaces():
    return [
        {"space": "wren", "name": "Wren", "id": "90147276901", "statuses": WREN_STATUSES},
        {"space": "blog", "name": "Blog", "id": "90147349460", "statuses": BLOG_STATUSES},
    ]


def test_resolve_space_returns_all_when_none_named():
    assert len(clickup._resolve_space(_spaces(), None)) == 2


def test_resolve_space_is_forgiving_about_spelling():
    assert clickup._resolve_space(_spaces(), "WREN")[0]["id"] == "90147276901"


def test_resolve_space_unknown_names_the_real_spaces():
    with pytest.raises(clickup._ClickUpError) as e:
        clickup._resolve_space(_spaces(), "nope")
    assert "wren" in str(e.value) and "blog" in str(e.value)


def test_resolve_status_returns_canonical_spelling():
    assert clickup._resolve_status(_spaces(), "PARKED") == "parked"


def test_resolve_status_is_scoped_to_the_chosen_space():
    """'parked' exists in Wren but not in Blog. Validating per-space is the whole
    point: without it a Blog filter for 'parked' returns an empty list, which
    reads as 'nothing is parked' rather than 'that status does not exist here'."""
    blog = [a for a in _spaces() if a["space"] == "blog"]
    with pytest.raises(clickup._ClickUpError) as e:
        clickup._resolve_status(blog, "parked")
    assert "to do" in str(e.value) and "complete" in str(e.value)
    assert "parked" not in str(e.value).split("Statuses:")[1]


# --------------------------------------------------------------------------- #
# list_clickup_tasks
# --------------------------------------------------------------------------- #

def test_list_clickup_tasks_returns_rows_and_the_space_names(stub):
    _set_tasks(stub, [_task("Add an audio voice", status="parked", tags=["ux"], priority="low")])
    out = clickup.list_clickup_tasks()
    assert out["item_count"] == 1
    assert out["spaces"] == ["wren", "blog"]
    row = out["items"][0]
    assert row["title"] == "Add an audio voice"
    assert row["space"] == "wren"
    assert row["status"] == "parked"
    assert row["tags"] == ["ux"]
    assert row["priority"] == "low"


def test_list_clickup_tasks_never_leaks_a_clickup_id(stub):
    """The model must never be handed an identifier it would have to copy back
    — read_clickup_task takes a title instead (docs/opaque-identifiers.md)."""
    _set_tasks(stub, [_task("Improve UX of map", task_id="86bbnfav7")])
    blob = json.dumps(clickup.list_clickup_tasks())
    assert "86bbnfav7" not in blob
    assert "90147276901" not in blob


def test_list_clickup_tasks_excludes_done_by_default_and_says_so(stub):
    _set_tasks(stub, [_task("Shipped thing", status="shipped")])
    out = clickup.list_clickup_tasks()
    task_calls = [p for path, p in stub.calls if path.endswith("/task")]
    assert "include_closed" not in task_calls[0]
    assert "include_done" in out["note"]


def test_list_clickup_tasks_include_done_asks_clickup_for_closed_items(stub):
    _set_tasks(stub, [_task("Shipped thing", status="shipped")])
    out = clickup.list_clickup_tasks(include_done=True)
    task_calls = [p for path, p in stub.calls if path.endswith("/task")]
    assert task_calls[0]["include_closed"] == "true"
    assert "note" not in out


def test_list_clickup_tasks_scopes_the_query_to_one_space(stub):
    _set_tasks(stub, [])
    clickup.list_clickup_tasks(space="blog")
    task_calls = [p for path, p in stub.calls if path.endswith("/task")]
    assert task_calls[0]["space_ids[]"] == ["90147349460"]


def test_list_clickup_tasks_filters_by_status(stub):
    _set_tasks(stub, [_task("A", status="parked"), _task("B", status="idea")])
    out = clickup.list_clickup_tasks(space="wren", status="parked")
    assert [r["title"] for r in out["items"]] == ["A"]


def test_list_clickup_tasks_bad_status_errors_instead_of_returning_nothing(stub):
    _set_tasks(stub, [_task("A", status="parked")])
    out = clickup.list_clickup_tasks(space="blog", status="parked")
    assert "no status 'parked'" in out["error"]


def test_list_clickup_tasks_sorts_most_recently_updated_first(stub):
    _set_tasks(stub, [
        _task("older", updated="1787000000000"),
        _task("newer", updated="1787826143872"),
    ])
    assert [r["title"] for r in clickup.list_clickup_tasks()["items"]] == ["newer", "older"]


def test_list_clickup_tasks_char_budget_drops_rows_and_admits_it(stub):
    """A row cap would not catch this: 200 items are all short, and it is their
    total size that blows the loop's context, not their number."""
    _set_tasks(stub, [_task(f"Backlog item number {i} with a fairly wordy title", tags=["feature", "chore"])
                      for i in range(200)])
    out = clickup.list_clickup_tasks()
    assert out["item_count"] == 200
    assert out["items_shown"] < 200
    assert "Do not say these are all of them" in out["partial"]


def test_list_clickup_tasks_worst_case_fits_under_the_loop_cap(stub):
    """Pins the budget below agent.loop's default result cap, so the loop never
    has to trim a payload this tool already trimmed on purpose."""
    from agent.loop import MAX_TOOL_RESULT_CHARS, TOOL_RESULT_CHAR_CAPS
    _set_tasks(stub, [_task(f"A very long backlog item title number {i} " + "x" * 60,
                            tags=["feature", "chore", "ux"]) for i in range(300)])
    payload = json.dumps(clickup.list_clickup_tasks())
    assert len(payload) < TOOL_RESULT_CHAR_CAPS.get("list_clickup_tasks", MAX_TOOL_RESULT_CHARS)


def test_list_clickup_tasks_walks_every_page(stub):
    stub.routes["tasks"] = [
        {"tasks": [_task(f"p0-{i}") for i in range(clickup._PAGE_SIZE)], "last_page": False},
        {"tasks": [_task("p1-0")], "last_page": True},
    ]
    assert clickup.list_clickup_tasks()["item_count"] == clickup._PAGE_SIZE + 1


def test_list_clickup_tasks_without_a_token_says_which_key(monkeypatch):
    monkeypatch.setattr(clickup, "resolve_key", lambda name, arg=None: None)
    assert "CLICKUP_API_TOKEN" in clickup.list_clickup_tasks()["error"]


def test_list_clickup_tasks_degrades_on_a_dead_api(stub, monkeypatch):
    def _boom(*a, **k):
        raise requests.exceptions.ConnectionError("no route to host")
    monkeypatch.setattr(clickup, "_get", _boom)
    out = clickup.list_clickup_tasks()
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
        out = clickup.list_clickup_tasks()
    finally:
        mod._get = orig
    assert "2 ClickUp workspaces" in out["error"]


# --------------------------------------------------------------------------- #
# read_clickup_task
# --------------------------------------------------------------------------- #

def test_read_clickup_task_matches_a_partial_title(stub):
    _set_tasks(stub, [_task("Wren Gemini Notebook sync - script solution",
                            description="A description.")])
    out = clickup.read_clickup_task("gemini notebook")
    assert out["title"] == "Wren Gemini Notebook sync - script solution"
    assert out["description"] == "A description."
    assert out["url"].startswith("https://app.clickup.com/t/")


def test_read_clickup_task_prefers_an_exact_title_over_a_substring(stub):
    _set_tasks(stub, [_task("Logging", task_id="a1"),
                      _task("Logging rotation for launchd", task_id="a2")])
    assert clickup.read_clickup_task("logging")["title"] == "Logging"


def test_read_clickup_task_ambiguous_returns_candidates(stub):
    _set_tasks(stub, [_task("Logging rotation", task_id="a1"),
                      _task("Logging for launchd", task_id="a2")])
    out = clickup.read_clickup_task("logging")
    assert "matches 2 tasks" in out["error"]
    assert len(out["candidates"]) == 2


def test_read_clickup_task_unknown_title_points_at_the_listing(stub):
    _set_tasks(stub, [_task("Something else")])
    assert "list_clickup_tasks" in clickup.read_clickup_task("does not exist")["error"]


def test_read_clickup_task_rejects_an_empty_title(stub):
    assert "must not be empty" in clickup.read_clickup_task("  ")["error"]


def test_read_clickup_task_can_reach_a_shipped_item(stub):
    """The default listing hides the closed group, so reading one by name has
    to ask for closed items or a shipped item is unreachable."""
    _set_tasks(stub, [_task("Shipped thing", status="shipped")])
    out = clickup.read_clickup_task("shipped thing")
    task_calls = [p for path, p in stub.calls if path.endswith("/task")]
    assert task_calls[0]["include_closed"] == "true"
    assert out["status"] == "shipped"


def test_read_clickup_task_trims_a_long_description_and_says_so(stub):
    _set_tasks(stub, [_task("Big one", description="word " * 2000)])
    out = clickup.read_clickup_task("big one")
    assert len(out["description"]) <= clickup._MAX_DESCRIPTION_CHARS + 1
    assert "not shown" in out["description_truncated"]


def test_read_clickup_task_returns_comments(stub):
    _set_tasks(stub, [_task("With comments")])
    stub.routes["comments"] = {"comments": [
        {"comment_text": "first note", "date": "1787826143872"},
        {"comment_text": "second note", "date": "1787826143872"},
    ]}
    out = clickup.read_clickup_task("with comments")
    assert [c["text"] for c in out["comments"]] == ["first note", "second note"]
    assert out["comments"][0]["date"]


def test_read_clickup_task_survives_a_comment_fetch_failure(stub, monkeypatch):
    """A failing comments call costs the comments, not the item."""
    _set_tasks(stub, [_task("Still readable", description="body")])
    real = clickup._get

    def _get(path, token, **params):
        if "/comment" in path:
            raise requests.exceptions.Timeout("slow")
        return real(path, token, **params)

    monkeypatch.setattr(clickup, "_get", _get)
    out = clickup.read_clickup_task("still readable")
    assert out["description"] == "body"
    assert out["comments"] == []
    assert out["comments_error"]


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #

def test_both_tools_are_registered_and_read_only():
    from agent import toolset
    for name in ("list_clickup_spaces", "list_clickup_tasks", "read_clickup_task"):
        assert name in toolset.DISPATCH
        assert name in toolset.TOOL_GROUP_NAMES["clickup"]
        assert name not in toolset.WRITE_TOOLS
        assert name not in toolset.CONSEQUENTIAL_TOOLS


def test_tool_descriptions_deny_pretraining():
    """A catalogue tool that doesn't say the list is unknowable gets skipped,
    and the model invents entries instead (docs/model-constraints.md).

    Scoped to the two READ tools on purpose: the rule is about answering "what
    exists?" from pretraining. A write tool is told what to write."""
    catalogue = (clickup.LIST_CLICKUP_SPACES_SCHEMA,
                 clickup.LIST_CLICKUP_TASKS_SCHEMA,
                 clickup.READ_CLICKUP_TASK_SCHEMA)
    for schema in catalogue:
        text = schema["function"]["description"].lower()
        assert "not something you know" in text \
            or "only the tasks this tool returns exist" in text \
            or "only tasks list_clickup_tasks returns exist" in text


# --------------------------------------------------------------------------- #
# clickup_digest — the morning brief's two lists.
#
# The status GROUP is read off the Space, never off the task. Every fixture
# below leans on that: _task() stamps a wrong `type` on the task itself, so a
# test that passes here can only be reading the Space's status table.
# --------------------------------------------------------------------------- #

def test_digest_asks_for_closed_items_only_in_the_moved_window(stub):
    """Two calls, deliberately different: "moved" must include shipped items,
    "in flight" must not. One filtered call could not do both."""
    _set_tasks(stub, [])
    _set_moved(stub, [])
    clickup.clickup_digest(since_ms=1787000000000)

    task_calls = [params for path, params in stub.calls if path.endswith("/task")]
    assert len(task_calls) == 2
    moved, current = task_calls
    assert moved["include_closed"] == "true"
    assert moved["date_updated_gt"] == 1787000000000
    assert "include_closed" not in current
    assert "date_updated_gt" not in current


def test_digest_skips_the_moved_call_entirely_without_a_cursor(stub):
    """No cursor, no window: asking ClickUp for "everything ever" and calling it
    yesterday's news is worse than reporting nothing moved."""
    _set_tasks(stub, [])
    clickup.clickup_digest(since_ms=None)

    task_calls = [params for path, params in stub.calls if path.endswith("/task")]
    assert len(task_calls) == 1
    assert "date_updated_gt" not in task_calls[0]


def test_digest_change_labels(stub):
    """An item created AND shipped inside one window is news because it
    shipped. Checking "created since" first would label it "added"."""
    _set_tasks(stub, [])
    _set_moved(stub, [
        # Created inside the window and already closed — both rules match.
        _task("Ship it", status="shipped", updated=str(_ms_days_ago(0)), task_id="a"),
        _task("Brand new", status="idea", updated=str(_ms_days_ago(0)), task_id="b"),
        _task("Older, moved on", status="building", updated=str(_ms_days_ago(0)), task_id="c"),
    ])
    # date_created on the fixtures is 1787826125157; put the cursor either side.
    stub.routes["tasks_with_done"][0]["tasks"][2]["date_created"] = "1000000000000"
    out = clickup.clickup_digest(since_ms=1787826125000)

    by_title = {r["title"]: r["change"] for r in out["moved"]}
    assert by_title["Ship it"] == "shipped"
    assert by_title["Brand new"] == "added"
    assert by_title["Older, moved on"] == "now building"


def test_digest_in_flight_is_the_active_group_per_space(stub):
    """"Active" is ClickUp's own status type, read off each Space. Blog's "in
    progress" and Wren's "building" are both custom; "idea" and "to do" are
    not. Matching on status NAMES would need an edit per Space."""
    _set_tasks(stub, [
        _task("Wren building", status="building", updated=str(_ms_days_ago(1)), task_id="a"),
        _task("Wren idea", status="idea", updated=str(_ms_days_ago(1)), task_id="b"),
        _task("Blog in progress", status="in progress", space="90147349460",
              updated=str(_ms_days_ago(1)), task_id="c"),
        _task("Blog to do", status="to do", space="90147349460",
              updated=str(_ms_days_ago(1)), task_id="d"),
    ])
    out = clickup.clickup_digest()

    assert sorted(r["title"] for r in out["in_flight"]) == ["Blog in progress", "Wren building"]
    assert out["in_flight_total"] == 2


def test_digest_caps_both_lists_and_reports_the_true_totals(stub):
    """The caps are for a human reading over coffee, so the "+N more" line has
    to come off the real count, not off the capped list."""
    _set_tasks(stub, [
        _task(f"Building {i}", status="building", updated=str(_ms_days_ago(i)), task_id=f"c{i}")
        for i in range(clickup._MAX_IN_FLIGHT + 4)
    ])
    _set_moved(stub, [
        _task(f"Moved {i}", status="shipped", updated=str(_ms_days_ago(i)), task_id=f"m{i}")
        for i in range(clickup._MAX_MOVED + 3)
    ])
    out = clickup.clickup_digest(since_ms=1787826125000)

    assert len(out["moved"]) == clickup._MAX_MOVED
    assert out["moved_total"] == clickup._MAX_MOVED + 3
    assert len(out["in_flight"]) == clickup._MAX_IN_FLIGHT
    assert out["in_flight_total"] == clickup._MAX_IN_FLIGHT + 4


def test_digest_sorts_freshest_first(stub):
    _set_tasks(stub, [
        _task("Stale", status="building", updated=str(_ms_days_ago(9)), task_id="a"),
        _task("Fresh", status="building", updated=str(_ms_days_ago(0)), task_id="b"),
        _task("Middling", status="building", updated=str(_ms_days_ago(3)), task_id="c"),
    ])
    out = clickup.clickup_digest()
    assert [r["title"] for r in out["in_flight"]] == ["Fresh", "Middling", "Stale"]


def test_digest_stalest_names_the_quiet_one(stub):
    _set_tasks(stub, [
        _task("Fresh", status="building", updated=str(_ms_days_ago(0)), task_id="a"),
        _task("Forgotten", status="building", updated=str(_ms_days_ago(clickup._STALE_DAYS + 2)),
              task_id="b"),
    ])
    out = clickup.clickup_digest()
    assert out["stalest"]["title"] == "Forgotten"
    assert out["stalest"]["days_since_update"] >= clickup._STALE_DAYS


def test_digest_stays_quiet_when_nothing_has_actually_gone_stale(stub):
    """Without the threshold this would name the oldest of three items touched
    this week — a nag every morning that means nothing."""
    _set_tasks(stub, [
        _task("A", status="building", updated=str(_ms_days_ago(0)), task_id="a"),
        _task("B", status="building", updated=str(_ms_days_ago(2)), task_id="b"),
    ])
    assert clickup.clickup_digest()["stalest"] is None


def test_digest_does_not_call_a_lone_in_flight_item_stale(stub):
    """The section has already printed it; naming it again as "untouched
    longest" is the same line twice."""
    _set_tasks(stub, [
        _task("Only one", status="building", updated=str(_ms_days_ago(60)), task_id="a"),
    ])
    out = clickup.clickup_digest()
    assert len(out["in_flight"]) == 1
    assert out["stalest"] is None


def test_digest_stalest_can_be_an_item_the_capped_list_never_showed(stub):
    """The cap hides the oldest rows, which is exactly where a stalled item
    hides. Taking the tail before slicing is what makes the callout useful."""
    _set_tasks(stub, [
        _task(f"Item {i}", status="building", updated=str(_ms_days_ago(i)), task_id=f"c{i}")
        for i in range(clickup._MAX_IN_FLIGHT + 3)
    ])
    out = clickup.clickup_digest()
    stalest = out["stalest"]["title"]
    assert stalest == f"Item {clickup._MAX_IN_FLIGHT + 2}"
    assert stalest not in [r["title"] for r in out["in_flight"]]


def test_digest_cursor_is_taken_before_the_fetch(stub, monkeypatch):
    """Persisting a cursor stamped AFTER the fetch silently drops anything
    changed while the brief was running. Never seen again, no error."""
    seen = {}
    real = clickup._get

    def _get(path, token, **params):
        if path.endswith("/task"):
            # A real fetch takes time; without it both stamps land in the same
            # millisecond and the assertion below passes either way.
            time.sleep(0.01)
            seen["at_fetch"] = int(datetime.now().timestamp() * 1000)
        return real(path, token, **params)

    monkeypatch.setattr(clickup, "_get", _get)
    _set_tasks(stub, [])
    out = clickup.clickup_digest()
    assert out["checked_ms"] < seen["at_fetch"]


def test_digest_degrades_to_an_error_rather_than_killing_the_brief(stub, monkeypatch):
    """One dead source must never take the whole morning brief with it."""
    monkeypatch.setattr(clickup, "_get", lambda *a, **k: (_ for _ in ()).throw(
        requests.exceptions.Timeout("slow")))
    out = clickup.clickup_digest(since_ms=1787826125000)
    assert "error" in out
    assert not out.get("moved")


# --------------------------------------------------------------------------- #
# Writes. Every test here asserts on stub.writes — what would actually have
# been sent to ClickUp — not just on the returned dict, because a tool that
# reports success while sending the wrong payload is the failure that matters.
# --------------------------------------------------------------------------- #

def test_add_clickup_task_sends_the_right_payload_to_the_right_list(stub):
    out = clickup.add_clickup_task("Better logging", "wren",
                                   description="Rotate the launchd logs.",
                                   tags=["maintenance"], priority="high")
    assert out["created"] is True
    assert out["tool_name"] == "add_clickup_task"

    (method, path, payload), = stub.writes
    assert method == "POST"
    assert path == "/list/901419409898/task"
    assert payload["name"] == "Better logging"
    assert payload["description"] == "Rotate the launchd logs."
    assert payload["tags"] == ["maintenance"]


def test_add_clickup_task_opens_at_the_spaces_own_not_started_status(stub):
    """'idea' in the Wren Space, 'to do' in Blog. The model picks neither."""
    wren = clickup.add_clickup_task("A", "wren")
    assert stub.writes[-1][2]["status"] == "idea"
    assert wren["status"] == "idea"

    clickup.add_clickup_task("B", "blog")
    assert stub.writes[-1][2]["status"] == "to do"


def test_add_clickup_task_translates_the_priority_word_to_clickups_number(stub):
    """ClickUp's priority is inverted and numeric — 1 is Urgent, 4 is Low.
    Python owns the number; the model never emits a digit."""
    clickup.add_clickup_task("Urgent thing", "wren", priority="urgent")
    assert stub.writes[-1][2]["priority"] == 1
    clickup.add_clickup_task("Low thing", "wren", priority="low")
    assert stub.writes[-1][2]["priority"] == 4


def test_add_clickup_task_refuses_an_unknown_priority_and_names_the_real_ones(stub):
    out = clickup.add_clickup_task("Thing", "wren", priority="critical")
    assert "critical" in out["error"]
    assert "urgent" in out["error"]
    assert stub.writes == []


def test_add_clickup_task_requires_a_space_rather_than_guessing(stub):
    """Three Spaces, no safe default: an item filed in the wrong space is worse
    than a question, and it is invisible where the user goes looking."""
    out = clickup.add_clickup_task("Homeless idea", "")
    assert "space is required" in out["error"]
    assert stub.writes == []


def test_add_clickup_task_refuses_an_unknown_space_and_names_the_real_ones(stub):
    out = clickup.add_clickup_task("Thing", "marketing")
    assert "marketing" in out["error"]
    assert "wren" in out["error"]
    assert stub.writes == []


def test_add_clickup_task_refuses_rather_than_picking_one_of_two_lists(stub):
    """Same posture as two workspaces: silently taking the first would file
    items somewhere the user never looks."""
    stub.routes["lists"] = {"lists": [{"id": "1", "name": "Backlog"},
                                      {"id": "2", "name": "Someday"}]}
    out = clickup.add_clickup_task("Thing", "wren")
    assert "2 Lists" in out["error"]
    assert "Backlog" in out["error"] and "Someday" in out["error"]
    assert stub.writes == []


# --------------------------------------------------------------------------- #
# Lists. A Space is not a List: the Wren Space holds "Backlog" and the Blog
# Space holds "Blog Planner", and nothing in this module may assume either name
# or assume a Space holds exactly one.
# --------------------------------------------------------------------------- #


def test_a_row_names_the_list_the_task_actually_sits_on(stub):
    """Without this a Space with several Lists reads back as one pile, and the
    user cannot tell which List anything is on."""
    _set_tasks(stub, [_task("Draft the launch post", list_name="Blog Planner")])
    row = clickup.list_clickup_tasks()["items"][0]
    assert row["list"] == "Blog Planner"
    assert row["space"] == "wren"


def test_listing_by_list_name_filters_server_side(stub):
    stub.routes["lists"] = {"lists": [{"id": "1", "name": "Backlog"},
                                      {"id": "2", "name": "Someday"}]}
    _set_tasks(stub, [_task("Thing")])
    clickup.list_clickup_tasks(space="wren", list_name="Someday")
    task_calls = [p for path, p in stub.calls if path.endswith("/task")]
    assert task_calls[0]["list_ids[]"] == ["2"], "the List filter must reach ClickUp"


def test_listing_by_list_name_is_forgiving_about_spelling(stub):
    stub.routes["lists"] = {"lists": [{"id": "2", "name": "Blog Planner"}]}
    _set_tasks(stub, [_task("Thing")])
    clickup.list_clickup_tasks(space="wren", list_name="blog-planner")
    task_calls = [p for path, p in stub.calls if path.endswith("/task")]
    assert task_calls[0]["list_ids[]"] == ["2"]


def test_listing_by_list_name_without_a_space_is_refused(stub):
    """Two Spaces may hold Lists of the same name, so this has no answer — and
    silently searching every Space would return the wrong Space's tasks."""
    out = clickup.list_clickup_tasks(list_name="Backlog")
    assert "Space" in out["error"]
    assert "wren" in out["error"] and "blog" in out["error"]


def test_listing_by_an_unknown_list_names_the_real_lists(stub):
    stub.routes["lists"] = {"lists": [{"id": "1", "name": "Backlog"}]}
    out = clickup.list_clickup_tasks(space="wren", list_name="Someday")
    assert "Someday" in out["error"]
    assert "Backlog" in out["error"]


def test_listing_without_a_list_name_never_fetches_lists(stub):
    """The List lookup is a GET per Space. The common listing must not pay it."""
    _set_tasks(stub, [_task("Thing")])
    clickup.list_clickup_tasks()
    assert not [path for path, _ in stub.calls if path.endswith("/list")]


def test_list_clickup_spaces_reports_the_lists_in_each_space(stub):
    """This is the tool the model calls to learn the names, so a Space without
    its Lists does not answer the question it was called to answer."""
    stub.routes["lists"] = {"lists": [{"id": "1", "name": "Backlog"},
                                      {"id": "2", "name": "Someday"}]}
    out = clickup.list_clickup_spaces()
    assert [sp["lists"] for sp in out["spaces"]] == [["Backlog", "Someday"],
                                                     ["Backlog", "Someday"]]


def test_a_list_inside_a_folder_is_reachable(stub):
    """A Folder is an organising layer, not a place Tasks live. A List inside
    one is still a List, and missing it would make it unwritable."""
    stub.routes["lists"] = {"lists": []}
    stub.routes["folders"] = {"folders": [{"lists": [{"id": "9", "name": "Buried"}]}]}
    out = clickup.add_clickup_task("Thing", "wren", "Buried")
    assert out["created"] is True
    assert stub.writes[0][1] == "/list/9/task"


def test_add_writes_to_the_named_list_not_the_first_one(stub):
    stub.routes["lists"] = {"lists": [{"id": "1", "name": "Backlog"},
                                      {"id": "2", "name": "Someday"}]}
    out = clickup.add_clickup_task("Thing", "wren", "Someday")
    assert stub.writes[0][1] == "/list/2/task"
    assert out["list"] == "Someday"


def test_add_refuses_an_unknown_list_and_names_the_real_ones(stub):
    stub.routes["lists"] = {"lists": [{"id": "1", "name": "Backlog"}]}
    out = clickup.add_clickup_task("Thing", "wren", "Someday")
    assert "Someday" in out["error"]
    assert "Backlog" in out["error"]
    assert stub.writes == []


def test_add_needs_no_list_name_when_the_space_holds_one(stub):
    """The List name stays optional for the ordinary case, or every capture
    turns into a question the user already answered by naming the Space."""
    out = clickup.add_clickup_task("Thing", "wren")
    assert out["created"] is True
    assert out["list"] == "Backlog"


def test_add_to_a_space_with_no_list_says_so(stub):
    stub.routes["lists"] = {"lists": []}
    out = clickup.add_clickup_task("Thing", "wren")
    assert "no List" in out["error"]
    assert stub.writes == []


def test_add_clickup_task_rejects_an_empty_title(stub):
    assert "title" in clickup.add_clickup_task("   ", "wren")["error"]
    assert stub.writes == []


def test_move_clickup_task_validates_the_status_against_that_items_own_space(stub):
    """'parked' exists in the Wren Space and nowhere else. Validating against a
    merged list of every Space's statuses would let this through and either
    400 or land the item somewhere nobody asked for."""
    _set_tasks(stub, [_task("Blog post", status="to do", space="90147349460")])
    out = clickup.move_clickup_task("blog post", "parked")
    assert "parked" in out["error"]
    assert "to do" in out["error"] and "in progress" in out["error"]
    assert stub.writes == []


def test_move_clickup_task_sends_the_canonical_status_spelling(stub):
    _set_tasks(stub, [_task("Audio voice", status="idea")])
    out = clickup.move_clickup_task("audio voice", "PARKED")

    (method, path, payload), = stub.writes
    assert method == "PUT"
    assert path == "/task/86bbnfav7"
    assert payload == {"status": "parked"}
    assert out["moved"] is True
    assert out["from"] == "idea" and out["status"] == "parked"


def test_move_clickup_task_to_the_status_it_is_already_in_writes_nothing(stub):
    """Reports honestly instead of claiming a move that did not happen."""
    _set_tasks(stub, [_task("Audio voice", status="idea")])
    out = clickup.move_clickup_task("audio voice", "idea")
    assert out["moved"] is False
    assert "already" in out["note"]
    assert stub.writes == []


def test_comment_on_clickup_task_posts_the_text_and_does_not_notify_everyone(stub):
    _set_tasks(stub, [_task("Audio voice")])
    out = clickup.comment_on_clickup_task("audio voice", "  Checked the Piper docs.  ")

    (method, path, payload), = stub.writes
    assert method == "POST"
    assert path == "/task/86bbnfav7/comment"
    assert payload["comment_text"] == "Checked the Piper docs."
    assert payload["notify_all"] is False
    assert out["commented"] is True


def test_comment_on_clickup_task_rejects_an_empty_comment(stub):
    _set_tasks(stub, [_task("Audio voice")])
    assert "comment" in clickup.comment_on_clickup_task("audio voice", "   ")["error"]
    assert stub.writes == []


def test_writes_never_guess_between_two_matching_titles(stub):
    """On a read an ambiguous title costs a question. On a WRITE it would change
    the wrong item, and nothing would tell the user to look."""
    # Neither title matches exactly, so both fall to the substring tier.
    _set_tasks(stub, [_task("Add logging to the watcher", task_id="a"),
                      _task("Add logging to the brief", task_id="b")])
    for out in (clickup.move_clickup_task("add logging", "parked"),
                clickup.comment_on_clickup_task("add logging", "note")):
        assert "matches 2 tasks" in out["error"]
        assert len(out["candidates"]) == 2
    assert stub.writes == []


def test_writes_resolve_a_title_exactly_as_the_read_does(stub):
    """One matcher, shared. A write that resolved a title differently from the
    read the user just did is the trap nobody tests for."""
    _set_tasks(stub, [_task("Add logging", task_id="a"),
                      _task("Add logging to the watcher", task_id="b")])
    # Exact match wins over the substring that also matches — for both.
    assert clickup.read_clickup_task("Add logging")["title"] == "Add logging"
    assert clickup.move_clickup_task("Add logging", "parked")["title"] == "Add logging"


def test_a_failing_write_reports_an_error_and_claims_nothing(stub, monkeypatch):
    _set_tasks(stub, [_task("Audio voice")])
    monkeypatch.setattr(clickup, "_write", lambda *a, **k: (_ for _ in ()).throw(
        requests.exceptions.Timeout("slow")))
    out = clickup.comment_on_clickup_task("audio voice", "note")
    assert "error" in out
    assert "commented" not in out


def test_every_write_names_itself_in_its_result(stub):
    """A gated call returns out of advance(), so MAX_TOOL_ITERATIONS resets on
    every continuation. Each result naming its own tool is what stops one
    request producing four cards (docs/limits.md)."""
    _set_tasks(stub, [_task("Audio voice")])
    assert clickup.add_clickup_task("New", "wren")["tool_name"] == "add_clickup_task"
    assert clickup.move_clickup_task("audio voice", "parked")["tool_name"] == "move_clickup_task"
    assert clickup.comment_on_clickup_task("audio voice", "n")["tool_name"] == "comment_on_clickup_task"


# --------------------------------------------------------------------------- #
# Registration and gating
# --------------------------------------------------------------------------- #

def test_write_tools_are_registered_and_gated():
    from agent import toolset
    for name in ("add_clickup_task", "move_clickup_task", "comment_on_clickup_task"):
        assert name in toolset.DISPATCH
        assert name in toolset.TOOL_GROUP_NAMES["clickup"]
        assert name in toolset.WRITE_TOOLS, f"{name} would auto-execute in chat"


def test_free_text_writes_are_barred_from_unattended_runs():
    """read_clickup_task renders a ClickUp description and its comments into a
    later Wren prompt, so text a background job writes there is durable,
    prompt-visible state it authored itself — the same test that excludes
    remember/pin/write_skill."""
    from agent import toolset
    assert "add_clickup_task" in toolset.UNATTENDED_EXCLUDED_TOOLS
    assert "comment_on_clickup_task" in toolset.UNATTENDED_EXCLUDED_TOOLS


def test_move_clickup_task_stays_available_unattended_because_it_writes_no_text():
    """It sets one value out of a fixed list the Space defines. There is nothing
    to plant. It is still approval-gated like every other write."""
    from agent import toolset
    assert "move_clickup_task" not in toolset.UNATTENDED_EXCLUDED_TOOLS
    assert "move_clickup_task" in toolset.WRITE_TOOLS


def test_each_write_renders_a_readable_confirmation_card():
    """This is the line on his phone. A blank or generic card is how a write
    slips through unnoticed."""
    from agent import toolset

    def card(name, **args):
        return toolset.describe_call({"function": {"name": name, "arguments": args}})

    assert card("add_clickup_task", title="Better logging", space="wren") == \
        'Add "Better logging" to wren in ClickUp'
    assert card("move_clickup_task", title="Audio voice", status="parked") == \
        'Move "Audio voice" to parked in ClickUp'
    assert card("comment_on_clickup_task", title="Audio voice") == 'Comment on "Audio voice" in ClickUp'


def test_the_text_being_written_is_shown_before_it_is_approved():
    """The title alone does not say what is about to be written in his name."""
    from agent import toolset

    def detail(name, **args):
        return toolset.describe_call_detail({"function": {"name": name, "arguments": args}})

    assert detail("comment_on_clickup_task", title="X", comment="Piper needs 2GB.") == "Piper needs 2GB."
    assert detail("add_clickup_task", title="X", space="wren",
                  description="Rotate the logs.") == "Rotate the logs."


# --------------------------------------------------------------------------- #
# The tag watcher's two library functions. Both take a ClickUp id, which is why
# neither is a chat tool — see docs/opaque-identifiers.md.
# --------------------------------------------------------------------------- #

def test_tagged_tasks_asks_for_every_watched_tag_in_one_call(stub):
    _set_tasks(stub, [_task("Do the thing", tags=["wren-research"])])
    clickup.tagged_clickup_tasks(["wren-research", "wren-context"])
    task_calls = [p for path, p in stub.calls if path.endswith("/task")]
    assert len(task_calls) == 1, "one GET should cover every tag — tags[] is OR-ed"
    assert task_calls[0]["tags[]"] == ["wren-research", "wren-context"]


def test_tagged_tasks_include_shipped_ones(stub):
    """A tag on a finished Task is still a request. ClickUp hides its Closed
    group unless asked, so this is the difference between working and silently
    ignoring anything already marked shipped."""
    _set_tasks(stub, [_task("Shipped but asked about", status="shipped",
                            tags=["wren-context"])])
    found = clickup.tagged_clickup_tasks(["wren-context"])
    task_calls = [p for path, p in stub.calls if path.endswith("/task")]
    assert task_calls[0].get("include_closed") == "true"
    assert [t["title"] for t in found["tasks"]] == ["Shipped but asked about"]


def test_tagged_tasks_carry_the_id_the_watcher_needs(stub):
    _set_tasks(stub, [_task("Do the thing", tags=["wren-research"], task_id="86bbzzz")])
    found = clickup.tagged_clickup_tasks(["wren-research"])
    assert found["tasks"][0]["id"] == "86bbzzz"


def test_tagged_tasks_report_which_watched_tags_a_task_carries(stub):
    """`watched` is the watched tags only, in the order given — a Task also
    wearing unrelated tags must not have them treated as requests, and a Task
    wearing both watched tags must not depend on ClickUp's ordering."""
    _set_tasks(stub, [_task("Both", tags=["urgent", "wren-context", "wren-research"])])
    found = clickup.tagged_clickup_tasks(["wren-research", "wren-context"])
    assert found["tasks"][0]["watched"] == ["wren-research", "wren-context"]


def test_tagged_tasks_ignore_a_tag_that_is_not_watched(stub):
    _set_tasks(stub, [_task("Other", tags=["urgent"])])
    found = clickup.tagged_clickup_tasks(["wren-research"])
    assert found["tasks"][0]["watched"] == []


def test_tagged_tasks_carry_the_description_the_job_prompt_needs(stub):
    _set_tasks(stub, [_task("Do the thing", tags=["wren-research"],
                            description="Compare the two SDKs")])
    found = clickup.tagged_clickup_tasks(["wren-research"])
    assert found["tasks"][0]["description"] == "Compare the two SDKs"


def test_tagged_tasks_trim_a_long_description(stub):
    _set_tasks(stub, [_task("Long", tags=["wren-research"],
                            description="x " * 4000)])
    found = clickup.tagged_clickup_tasks(["wren-research"])
    assert len(found["tasks"][0]["description"]) <= clickup._MAX_DESCRIPTION_CHARS + 1


def test_tagged_tasks_degrade_to_an_error_rather_than_raising(stub, monkeypatch):
    """A dead network must read as an error to the watcher, never as "no tags"
    — reading it as empty would look like a working, quiet poller forever."""
    monkeypatch.setattr(clickup, "_get", lambda *a, **k: (_ for _ in ()).throw(
        requests.ConnectionError("no route to host")))
    found = clickup.tagged_clickup_tasks(["wren-research"])
    assert "error" in found
    assert "tasks" not in found


def test_remove_tag_deletes_exactly_that_tag_on_that_task(stub):
    clickup.remove_clickup_tag("86bbnfav7", "wren-research")
    assert stub.writes == [("DELETE", "/task/86bbnfav7/tag/wren-research", None)]


def test_remove_tag_escapes_a_name_that_would_otherwise_break_the_path(stub):
    """Tag names are free text, so they reach the URL escaped."""
    clickup.remove_clickup_tag("86bbnfav7", "needs review #2")
    _, path, _ = stub.writes[0]
    assert path == "/task/86bbnfav7/tag/needs%20review%20%232"


def test_a_tag_name_with_a_slash_is_refused_before_it_is_sent(stub):
    """Measured against the live API on 2026-08-27: ClickUp's ROUTER rejects a
    slash in this path, encoded (%2F) or raw, with a plain-text "404 page not
    found" that never reaches their code. Every other separator tried —
    hyphen, underscore, colon, dot — returns 200. There is no other endpoint;
    ClickUp has no "set all tags" call. So such a tag is not removable at all,
    and the watcher would have warned every five minutes forever. Refuse it
    here, where the message can say why."""
    result = clickup.remove_clickup_tag("86bbnfav7", "wren/research")
    assert "error" in result
    assert "slash" in result["error"]
    assert stub.writes == [], "nothing should have been sent"


def test_remove_tag_reports_a_failure_instead_of_claiming_success(stub, monkeypatch):
    monkeypatch.setattr(clickup, "_write", lambda *a, **k: (_ for _ in ()).throw(
        requests.HTTPError("404 Not Found")))
    assert "error" in clickup.remove_clickup_tag("86bbnfav7", "wren-research")


def test_the_tag_functions_are_not_offered_to_the_model():
    """They take ClickUp ids. Every model-facing tool here takes a title."""
    names = {s["function"]["name"] for s in clickup.CLICKUP_TOOL_SCHEMAS}
    assert "tagged_clickup_tasks" not in names
    assert "remove_clickup_tag" not in names


# ---- backlog_anchors: the one call tasks/daily_synthesis.py makes ----


def test_backlog_anchors_returns_the_three_fields_the_matcher_tokenizes(stub):
    """title, description and tags — nothing else. The consumer feeds exactly
    these three into _project_tokens, in that priority order."""
    _set_tasks(stub, [_task("Blog writer", description="Draft posts from the vault.",
                            tags=("writing", "llm"))])
    result = clickup.backlog_anchors()
    assert result["items"] == [{
        "title": "Blog writer",
        "description": "Draft posts from the vault.",
        "tags": ["writing", "llm"],
    }]


def test_an_item_with_no_description_is_dropped_and_counted(stub):
    """Both halves matter. Dropping it is the point — a bare title is a
    two-token anchor that outranks the wiki while saying nothing. Counting it
    is what stops an all-bare backlog reading as a broken matcher."""
    _set_tasks(stub, [_task("Blog writer", description="Draft posts."),
                      _task("Fix logging", description=""),
                      _task("Another idea", description="   ")])
    result = clickup.backlog_anchors()
    assert [i["title"] for i in result["items"]] == ["Blog writer"]
    assert result["skipped"] == 2, "whitespace-only counts as no description"


def test_backlog_anchors_never_asks_for_done_items(stub):
    """A shipped idea is not something to be nudged toward. ClickUp excludes
    its Closed group unless asked, so the assertion is that we never ask."""
    _set_tasks(stub, [_task("Blog writer", description="Draft posts.")])
    clickup.backlog_anchors()
    task_calls = [p for path, p in stub.calls if path.endswith("/task")]
    assert task_calls, "it should have fetched tasks"
    assert all("include_closed" not in p for p in task_calls)


def test_backlog_anchors_reads_every_item_in_one_request(stub):
    """ClickUp returns the description on the list endpoint. Reading each item
    individually would be one call per item against a 100/minute budget."""
    _set_tasks(stub, [_task(f"Idea {n}", description=f"About thing {n}.",
                            task_id=f"id{n}") for n in range(20)])
    result = clickup.backlog_anchors()
    assert len(result["items"]) == 20
    assert len([p for path, p in stub.calls if path.endswith("/task")]) == 1


def test_a_long_description_is_trimmed(stub):
    _set_tasks(stub, [_task("Blog writer", description="x" * 5000)])
    assert len(clickup.backlog_anchors()["items"][0]["description"]) == \
        clickup._MAX_DESCRIPTION_CHARS


def test_backlog_anchors_reports_a_failure_instead_of_looking_empty(stub, monkeypatch):
    """An empty result and a dead API must not look the same to the caller —
    one is a quiet day, the other is a source that stopped working."""
    monkeypatch.setattr(clickup, "_get", lambda *a, **k: (_ for _ in ()).throw(
        requests.HTTPError("500 Server Error")))
    assert "error" in clickup.backlog_anchors()


def test_backlog_anchors_is_not_offered_to_the_model():
    """Same reason as clickup_digest: in chat, list_clickup_tasks answers this
    better because it can be asked follow-ups."""
    names = {s["function"]["name"] for s in clickup.CLICKUP_TOOL_SCHEMAS}
    assert "backlog_anchors" not in names


def test_the_suite_cannot_reach_the_live_clickup_api(monkeypatch):
    """The conftest backstop, asserted rather than assumed. It fires below
    _get/_write, so a new caller whose test file forgets to stub its source
    fails loudly instead of hitting api.clickup.com with the real token."""
    monkeypatch.setattr(clickup, "resolve_key", lambda name, arg=None: "pk_test")
    with pytest.raises(BaseException, match="live ClickUp API"):
        clickup.backlog_anchors()


# ---- closed_tasks: the day's record of work that leaves no commit ----


from datetime import date as _date
from zoneinfo import ZoneInfo
from agent import toolset

DAY = _date(2026, 8, 26)


def _closed_task(name="Proposal for Acme", space_id="s1", status="complete",
                 status_type="closed", closed="2026-08-26T14:00:00-04:00",
                 updated=None):
    def _ms(iso):
        return None if iso is None else int(
            datetime.fromisoformat(iso).timestamp() * 1000)
    return {"name": name, "space": {"id": space_id},
            "list": {"name": "Backlog"},
            "status": {"status": status, "type": status_type},
            "date_closed": _ms(closed),
            "date_updated": _ms(updated or closed)}


def _stub_clickup(monkeypatch, tasks, spaces=(("s1", "Vibe Foundry"),)):
    """Stub the two helpers closed_tasks reaches through, and hand back the
    params the task fetch was called with so a test can assert on them."""
    seen = {}
    monkeypatch.setattr(clickup, "_client", lambda k: ("pk_x", None))
    monkeypatch.setattr(clickup, "_team_id", lambda t: "team1")
    monkeypatch.setattr(clickup, "_spaces",
                        lambda t, tid: [{"id": i, "name": n} for i, n in spaces])

    def _fetch(token, team_id, space_ids, include_done, updated_after_ms=None, **k):
        seen.update(include_done=include_done, updated_after_ms=updated_after_ms,
                    space_ids=space_ids)
        return tasks
    monkeypatch.setattr(clickup, "_fetch_tasks", _fetch)
    return seen


def test_a_task_closed_that_day_is_returned_with_its_space_and_status(monkeypatch):
    _stub_clickup(monkeypatch, [_closed_task()])
    items = clickup.closed_tasks(DAY)["items"]
    assert items == [{"title": "Proposal for Acme", "space": "Vibe Foundry",
                      "status": "complete"}]


def test_a_task_closed_on_another_day_is_left_out(monkeypatch):
    _stub_clickup(monkeypatch, [_closed_task(closed="2026-08-25T14:00:00-04:00")])
    assert clickup.closed_tasks(DAY)["items"] == []


def test_an_old_task_merely_edited_that_day_is_not_reported_as_closed(monkeypatch):
    """The whole reason this filters on date_closed. Editing a Task months after
    shipping it bumps date_updated; the two disagree on 2 of this account's 26
    closed Tasks, and using date_updated would invent work he did not do."""
    _stub_clickup(monkeypatch, [_closed_task(closed="2026-06-01T09:00:00-04:00",
                                             updated="2026-08-26T14:00:00-04:00")])
    assert clickup.closed_tasks(DAY)["items"] == []


def test_an_open_task_updated_that_day_is_left_out(monkeypatch):
    """The fetch is deliberately wide — it asks for everything touched since the
    day began — so the status-group filter is what makes the answer right."""
    _stub_clickup(monkeypatch, [_closed_task(status="building", status_type="custom")])
    assert clickup.closed_tasks(DAY)["items"] == []


def test_the_fetch_includes_done_and_starts_at_the_local_day(monkeypatch):
    """include_done because ClickUp excludes its Closed group by default, which
    is the only group this asks about. And date_updated_gt at the start of the
    LOCAL day, never a slice of a UTC stamp (docs/timezones.md)."""
    seen = _stub_clickup(monkeypatch, [])
    clickup.closed_tasks(DAY)
    assert seen["include_done"] is True
    started = datetime.fromtimestamp(seen["updated_after_ms"] / 1000,
                                     ZoneInfo(clickup.local_timezone()))
    assert started.date() == DAY
    assert (started.hour, started.minute) == (0, 0)


def test_a_failure_degrades_to_an_error_dict(monkeypatch):
    """One dead source must never kill the day's entry."""
    _stub_clickup(monkeypatch, [])
    monkeypatch.setattr(clickup, "_team_id",
                        lambda t: (_ for _ in ()).throw(clickup._ClickUpError("no workspace")))
    assert "error" in clickup.closed_tasks(DAY)


def test_no_spaces_is_an_empty_day_not_an_error(monkeypatch):
    _stub_clickup(monkeypatch, [], spaces=())
    assert clickup.closed_tasks(DAY) == {"items": []}


def test_closed_tasks_is_not_offered_to_the_model():
    """A library function like clickup_digest and backlog_anchors. ScribeJay calls
    it directly; in chat the question is answered better by list_clickup_tasks."""
    assert not hasattr(clickup, "CLOSED_TASKS_SCHEMA")
    assert "closed_tasks" not in {t["function"]["name"] for t in toolset.TOOLS}
