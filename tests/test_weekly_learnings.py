"""Tests for tasks/weekly_learnings.py — the week-range math, the event
bucketing (which must track calendar.CATEGORY_COLORS, not copies of the raw
ids), and the vault-write-fails -> email-the-draft fallback that the README
advertises as "never silently lost". All collaborators are monkeypatched;
nothing touches the model, calendar, Chrome, YouTube, or Gmail."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from agent.tools.calendar import CATEGORY_COLORS
from tasks import weekly_learnings as wl

_TZ = ZoneInfo("America/New_York")


# --------------------------------------------------------------------------- #
# _week_range
# --------------------------------------------------------------------------- #

def test_week_range_on_the_monday_trigger():
    # Run Monday 2026-07-06 05:00 (the launchd shape): the completed week is
    # Mon Jun 29 .. Sun Jul 5.
    monday, sunday = wl._week_range(now=datetime(2026, 7, 6, 5, 0, tzinfo=_TZ))
    assert monday.date().isoformat() == "2026-06-29"
    assert sunday.date().isoformat() == "2026-07-05"
    assert (sunday.hour, sunday.minute, sunday.second) == (23, 59, 59)


def test_week_range_midweek_still_returns_last_completed_week():
    # Run manually on a Thursday: same completed week as the preceding Monday.
    monday, sunday = wl._week_range(now=datetime(2026, 7, 9, 12, 30, tzinfo=_TZ))
    assert monday.date().isoformat() == "2026-06-29"
    assert sunday.date().isoformat() == "2026-07-05"


def test_week_range_across_a_year_boundary():
    # Thursday 2026-01-01: the completed week is Mon Dec 22 .. Sun Dec 28, 2025.
    monday, sunday = wl._week_range(now=datetime(2026, 1, 1, tzinfo=_TZ))
    assert monday.date().isoformat() == "2025-12-22"
    assert sunday.date().isoformat() == "2025-12-28"
    assert monday.weekday() == 0 and sunday.weekday() == 6


def test_week_range_on_a_sunday_excludes_the_in_progress_week():
    # Sunday itself isn't a completed week yet — anchor is the Sunday BEFORE.
    monday, sunday = wl._week_range(now=datetime(2026, 7, 5, tzinfo=_TZ))
    assert monday.date().isoformat() == "2026-06-22"
    assert sunday.date().isoformat() == "2026-06-28"


# --------------------------------------------------------------------------- #
# _categorize
# --------------------------------------------------------------------------- #

def test_categorize_tracks_category_colors():
    events = [
        {"summary": "Deep work", "colorId": CATEGORY_COLORS["Work/LLC"][0]},
        {"summary": "1:1 with Sam", "colorId": CATEGORY_COLORS["Meetings"][0]},
        {"summary": "Dentist", "colorId": CATEGORY_COLORS["Appointments"][0]},
        {"summary": "AARP volunteer call", "colorId": CATEGORY_COLORS["Work/LLC"][0]},
        {"summary": "Morning run", "colorId": CATEGORY_COLORS["Fitness"][0]},
    ]
    buckets = wl._categorize(events)
    assert [e["summary"] for e in buckets["work"]] == ["Deep work"]
    assert [e["summary"] for e in buckets["meetings"]] == ["1:1 with Sam"]
    assert [e["summary"] for e in buckets["appointments"]] == ["Dentist"]
    # AARP is matched by summary before color, and fitness lands in no bucket.
    assert [e["summary"] for e in buckets["aarp"]] == ["AARP volunteer call"]


# --------------------------------------------------------------------------- #
# main() — the write-fails -> email fallback contract
# --------------------------------------------------------------------------- #

@pytest.fixture
def stubbed_run(monkeypatch):
    """Stub every collaborator for a happy-path run; tests then break the parts
    they're exercising. Returns the dict recording what got called."""
    seen = {"emails": [], "failures": [], "writes": []}
    monkeypatch.setattr(wl, "get_events_in_range", lambda *a, **k: {"events": []})
    monkeypatch.setattr(wl, "fetch_chrome_history", lambda *a, **k: {"sites": []})
    monkeypatch.setattr(wl, "fetch_liked_videos", lambda *a, **k: {"videos": []})
    monkeypatch.setattr(wl, "get_previous_entry_text", lambda: "")
    monkeypatch.setattr(wl, "complete_text", lambda **k: "## Strategic Weekly Review: draft")
    monkeypatch.setattr(wl, "write_weekly_entry",
                        lambda text, sunday: seen["writes"].append(text) or {"ok": True})
    monkeypatch.setattr(wl, "send_email",
                        lambda subject, body: seen["emails"].append((subject, body)) or {"message_id": "m1"})
    monkeypatch.setattr(wl, "notify_failure",
                        lambda name, detail, logger=None: seen["failures"].append(str(detail)))
    return seen


def test_happy_path_writes_and_sends_no_email(stubbed_run):
    assert wl.main() == 0
    assert stubbed_run["writes"] and not stubbed_run["emails"]
    assert stubbed_run["failures"] == []


def test_vault_write_failure_emails_the_draft(stubbed_run, monkeypatch):
    monkeypatch.setattr(wl, "write_weekly_entry",
                        lambda text, sunday: {"error": "drive not mounted"})
    assert wl.main() == 0  # the fallback preserved the draft; run still succeeds
    assert len(stubbed_run["emails"]) == 1
    subject, body = stubbed_run["emails"][0]
    assert "needs manual paste" in subject
    assert "Strategic Weekly Review" in body
    assert any("vault write failed" in f for f in stubbed_run["failures"])


def test_write_and_email_both_failing_is_a_failed_run(stubbed_run, monkeypatch):
    # send_email returns error dicts rather than raising — the run must check
    # it, alert, and exit nonzero instead of quietly reporting success.
    monkeypatch.setattr(wl, "write_weekly_entry",
                        lambda text, sunday: {"error": "drive not mounted"})
    monkeypatch.setattr(wl, "send_email",
                        lambda subject, body: {"error": "gmail 503"})
    assert wl.main() == 1
    assert any("email fallback" in f or "gmail 503" in f for f in stubbed_run["failures"])
