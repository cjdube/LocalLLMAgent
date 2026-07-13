"""Tests for tasks/daily_chrome_learnings.py — event bucketing (which must track
calendar.CATEGORY_COLORS) and that main() drafts and persists a Daily-Chrome
entry. All collaborators are monkeypatched; nothing touches the model, calendar,
Chrome, the vault, or Gmail."""

import pytest

from agent.tools.calendar import CATEGORY_COLORS
from tasks import daily_chrome_learnings as dc


def test_categorize_tracks_category_colors():
    events = [
        {"summary": "Deep work", "colorId": CATEGORY_COLORS["Work/LLC"][0]},
        {"summary": "1:1 with Sam", "colorId": CATEGORY_COLORS["Meetings"][0]},
        {"summary": "Dentist", "colorId": CATEGORY_COLORS["Appointments"][0]},
        {"summary": "AARP volunteer call", "colorId": CATEGORY_COLORS["Work/LLC"][0]},
        {"summary": "Morning run", "colorId": CATEGORY_COLORS["Fitness"][0]},
    ]
    buckets = dc._categorize(events)
    assert [e["summary"] for e in buckets["work"]] == ["Deep work"]
    assert [e["summary"] for e in buckets["meetings"]] == ["1:1 with Sam"]
    assert [e["summary"] for e in buckets["appointments"]] == ["Dentist"]
    assert [e["summary"] for e in buckets["aarp"]] == ["AARP volunteer call"]


@pytest.fixture
def stubbed_run(monkeypatch):
    """Stub every collaborator for a happy-path run."""
    seen = {"persists": []}
    monkeypatch.setattr(dc, "get_events_in_range", lambda *a, **k: {"events": []})
    monkeypatch.setattr(dc, "fetch_chrome_history", lambda *a, **k: {"sites": []})
    monkeypatch.setattr(dc, "resolve_backend", lambda key: None)
    monkeypatch.setattr(dc, "warm_model", lambda **k: True)
    monkeypatch.setattr(dc, "complete_text", lambda **k: "## Daily Log: July 12, 2026")
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


def test_fetch_failure_is_a_failed_run(stubbed_run, monkeypatch):
    calls = []
    monkeypatch.setattr(dc, "fetch_chrome_history",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("sqlite boom")))
    monkeypatch.setattr(dc, "notify_failure", lambda name, detail, logger=None: calls.append(str(detail)))
    assert dc.main() == 1
    assert any("sqlite boom" in c for c in calls)
    assert stubbed_run["persists"] == []  # never reached the write
