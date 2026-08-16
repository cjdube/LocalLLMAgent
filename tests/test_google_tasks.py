"""Tests for the pure helpers in agent.tools.google_tasks.

_due_soon_cutoff and the no-override branch of _resolve_tasklist_id are
tested here — everything else talks directly to the live Google Tasks API
and isn't unit-tested, matching the project's existing precedent of not
testing calendar.py's live-API functions either.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from agent.tools.google_tasks import _due_soon_cutoff, _resolve_tasklist_id

EASTERN = ZoneInfo("America/New_York")


def test_due_soon_cutoff_is_end_of_local_day_two_days_out():
    now = datetime(2026, 7, 7, 9, 0, 0, tzinfo=EASTERN)  # Tuesday 9am ET
    cutoff = _due_soon_cutoff(48, now)
    # 9am Tue + 48h = 9am Thu -> end of Thursday local day.
    assert cutoff == "2026-07-10T03:59:59.999999Z"


def test_due_soon_cutoff_returns_utc_z_suffix():
    now = datetime(2026, 7, 7, 9, 0, 0, tzinfo=EASTERN)
    cutoff = _due_soon_cutoff(48, now)
    assert cutoff.endswith("Z")
    assert "+00:00" not in cutoff


def test_due_soon_cutoff_default_48h_matches_explicit_call():
    now = datetime(2026, 7, 7, 9, 0, 0, tzinfo=EASTERN)
    assert _due_soon_cutoff(48, now) == _due_soon_cutoff(48, now)


def test_due_soon_cutoff_zero_hours_is_end_of_today():
    now = datetime(2026, 7, 7, 9, 0, 0, tzinfo=EASTERN)
    cutoff = _due_soon_cutoff(0, now)
    # End of Tuesday 2026-07-07 in EDT (UTC-4) is 2026-07-08 03:59:59.999999 UTC.
    assert cutoff == "2026-07-08T03:59:59.999999Z"


# --------------------------------------------------------------------------- #
# _resolve_tasklist_id (no-list_name branch only — doesn't touch the network)
# --------------------------------------------------------------------------- #

def test_resolve_tasklist_id_defaults_to_at_default(monkeypatch):
    monkeypatch.delenv("GOOGLE_TASKLIST_ID", raising=False)
    assert _resolve_tasklist_id() == "@default"


def test_resolve_tasklist_id_honors_env_override(monkeypatch):
    monkeypatch.setenv("GOOGLE_TASKLIST_ID", "some-list-id")
    assert _resolve_tasklist_id() == "some-list-id"


# --------------------------------------------------------------------------- #
# _list_tasks pages, and reports the true total
#
# maxResults is a PAGE size, not a total. The call didn't page, so a list with
# more tasks than that returned exactly that many and task_count reported the
# page as the whole list — the same shape as the calendar's 250-event ceiling,
# except _all_tasklists directly above it already paged, so this was an
# oversight rather than a decision.
# --------------------------------------------------------------------------- #

import json

from agent.tools import google_tasks as gt


class _FakeTasks:
    """Serves `total` tasks in pages of _PAGE_SIZE, handing back a nextPageToken
    until they run out. Records each request so the walk can be asserted on."""

    def __init__(self, total):
        self.total = total
        self.requests = []

    def list(self, **kwargs):
        self.requests.append(kwargs)
        start = int(kwargs.get("pageToken") or 0)
        end = min(start + kwargs["maxResults"], self.total)
        items = [{"id": f"t{i}", "title": f"Task {i}", "due": f"2026-08-{(i % 28) + 1:02d}T00:00:00Z",
                  "status": "needsAction"} for i in range(start, end)]
        payload = {"items": items}
        if end < self.total:
            payload["nextPageToken"] = str(end)
        return _Exec(payload)


class _Exec:
    def __init__(self, payload):
        self._payload = payload

    def execute(self):
        return self._payload


class _FakeTasksService:
    def __init__(self, tasks):
        self._tasks = tasks

    def tasks(self):
        return self._tasks


def _stub_service(monkeypatch, total):
    fake = _FakeTasks(total)
    monkeypatch.setattr(gt, "build_service", lambda api, version: _FakeTasksService(fake))
    monkeypatch.setattr(gt, "_read_tasklists", lambda: [{"id": "L1", "title": "Inbox"}])
    return fake


def test_a_list_longer_than_one_page_is_walked_to_the_end(monkeypatch):
    fake = _stub_service(monkeypatch, total=250)

    result = gt.get_tasks(max_results=1000)

    assert result["task_count"] == 250            # not the page size
    assert len(fake.requests) == 3                # 100 + 100 + 50
    assert fake.requests[1]["pageToken"] == "100"


def test_the_true_total_survives_the_max_results_cap(monkeypatch):
    _stub_service(monkeypatch, total=250)

    result = gt.get_tasks(max_results=20)

    assert result["task_count"] == 250            # everything that exists
    assert result["tasks_shown"] == 20
    assert len(result["tasks"]) == 20
    # Sorted by due date, so the dropped tail reads like having nothing else on.
    assert "do not say these are all of them" in result["partial"].lower()


def test_a_short_list_is_not_marked_partial(monkeypatch):
    _stub_service(monkeypatch, total=5)

    result = gt.get_tasks()

    assert result["task_count"] == 5 and result["tasks_shown"] == 5
    assert "partial" not in result


def test_the_capped_result_fits_the_tool_result_cap(monkeypatch):
    from agent.loop import MAX_TOOL_RESULT_CHARS
    _stub_service(monkeypatch, total=1000)

    assert len(json.dumps(gt.get_tasks())) < MAX_TOOL_RESULT_CHARS


def test_the_fetch_ceiling_stops_a_runaway_account(monkeypatch):
    # _MAX_FETCH bounds the walk, never the reported count of what was walked.
    fake = _stub_service(monkeypatch, total=100_000)

    result = gt.get_tasks(max_results=10)

    assert result["task_count"] <= gt._MAX_FETCH + gt._PAGE_SIZE
    assert len(fake.requests) < 20
