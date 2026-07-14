"""Tests for tasks/daily_chrome_learnings.py — event bucketing (which must track
calendar.CATEGORY_COLORS) and that main() drafts and persists a Daily-Chrome
entry. All collaborators are monkeypatched; nothing touches the model, calendar,
Chrome, the vault, or Gmail."""

import pytest

from agent.tools.calendar import CATEGORY_COLORS
from tasks import _learnings_common as lc
from tasks import daily_chrome_learnings as dc


@pytest.fixture
def excluded_kw(monkeypatch):
    """is_excluded_text reads the module global at call time, so patching the
    list covers dc's imported reference too."""
    monkeypatch.setattr(lc, "_EXCLUDED_KEYWORDS", ["aarp"])


def test_categorize_tracks_category_colors(excluded_kw):
    events = [
        {"summary": "Deep work", "colorId": CATEGORY_COLORS["Work/LLC"][0]},
        {"summary": "1:1 with Sam", "colorId": CATEGORY_COLORS["Meetings"][0]},
        {"summary": "Dentist", "colorId": CATEGORY_COLORS["Appointments"][0]},
        {"summary": "Morning run", "colorId": CATEGORY_COLORS["Fitness"][0]},
    ]
    buckets = dc._categorize(events)
    assert [e["summary"] for e in buckets["work"]] == ["Deep work"]
    assert [e["summary"] for e in buckets["meetings"]] == ["1:1 with Sam"]
    assert [e["summary"] for e in buckets["appointments"]] == ["Dentist"]


def test_categorize_drops_excluded_events_whatever_their_color(excluded_kw):
    # The whole reason the drop is explicit: these would otherwise be bucketed by
    # colour into the two sections Craig wants AARP kept out of.
    events = [
        {"summary": "AARP volunteer call", "colorId": CATEGORY_COLORS["Work/LLC"][0]},
        {"summary": "AARP coordination", "colorId": CATEGORY_COLORS["Meetings"][0]},
        {"summary": "aarp speakers bureau", "colorId": None},
        {"summary": "Deep work", "colorId": CATEGORY_COLORS["Work/LLC"][0]},
    ]
    buckets = dc._categorize(events)
    assert [e["summary"] for e in buckets["work"]] == ["Deep work"]
    assert buckets["meetings"] == []
    assert "aarp" not in buckets


def test_categorize_keeps_everything_when_no_keywords(monkeypatch):
    monkeypatch.setattr(lc, "_EXCLUDED_KEYWORDS", [])
    events = [{"summary": "AARP volunteer call", "colorId": CATEGORY_COLORS["Work/LLC"][0]}]
    assert [e["summary"] for e in dc._categorize(events)["work"]] == ["AARP volunteer call"]


@pytest.fixture
def stubbed_run(monkeypatch):
    """Stub every collaborator for a happy-path run: some browsing yesterday and a
    draft with a real bullet, so both the pre-check and the all-None post-check pass."""
    seen = {"persists": [], "drafted": 0}
    monkeypatch.setattr(dc, "get_events_in_range", lambda *a, **k: {"events": []})
    monkeypatch.setattr(dc, "fetch_chrome_history",
                        lambda *a, **k: {"sites": [{"domain": "github.com", "title": "gh", "visits": 3}]})
    monkeypatch.setattr(dc, "resolve_backend", lambda key: None)
    monkeypatch.setattr(dc, "warm_model", lambda **k: True)

    def _draft(**k):
        seen["drafted"] += 1
        return "## Daily Log: July 12, 2026\n\n### Tools & Tech Encountered\n- **GitHub:** reviewed a PR"
    monkeypatch.setattr(dc, "complete_text", _draft)
    monkeypatch.setattr(dc, "persist_or_email",
                        lambda content, prefix, day, subject, task_name, logger:
                        seen["persists"].append((prefix, subject, content)) or {"written": True})
    monkeypatch.setattr(dc, "notify_failure", lambda *a, **k: None)
    return seen


def test_happy_path_persists_daily_chrome(stubbed_run):
    assert dc.main() == 0
    assert len(stubbed_run["persists"]) == 1
    prefix, subject, content = stubbed_run["persists"][0]
    assert prefix == "Daily-Chrome"
    assert "Daily Log" in content


def test_no_events_or_browsing_skips_without_calling_model(stubbed_run, monkeypatch):
    # Nothing happened yesterday — skip early, before warming the model.
    monkeypatch.setattr(dc, "fetch_chrome_history", lambda *a, **k: {"sites": []})
    assert dc.main() == 0
    assert stubbed_run["persists"] == []
    assert stubbed_run["drafted"] == 0  # model never ran


def test_all_none_draft_skips_the_write(stubbed_run, monkeypatch):
    # There was browsing, but the model found nothing relevant (all sections None).
    monkeypatch.setattr(
        dc, "complete_text",
        lambda **k: "## Daily Log: July 12, 2026\n\n### Tools & Tech Encountered\n"
                    "- **None:** [No qualifying items for this section]")
    assert dc.main() == 0
    assert stubbed_run["persists"] == []


def test_fetch_failure_is_a_failed_run(stubbed_run, monkeypatch):
    calls = []
    monkeypatch.setattr(dc, "fetch_chrome_history",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("sqlite boom")))
    monkeypatch.setattr(dc, "notify_failure", lambda name, detail, logger=None: calls.append(str(detail)))
    assert dc.main() == 1
    assert any("sqlite boom" in c for c in calls)
    assert stubbed_run["persists"] == []  # never reached the write
