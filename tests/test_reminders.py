"""Tests for the reminders store/tools. _STORE_PATH is redirected to a tmp file,
so nothing touches the real config/reminders.json."""

from datetime import datetime, timedelta

import pytest
from zoneinfo import ZoneInfo

from agent.dates import local_timezone
from agent.tools import reminders

_TZ = ZoneInfo(local_timezone())


@pytest.fixture(autouse=True)
def _isolate_store(tmp_path, monkeypatch):
    monkeypatch.setattr(reminders, "_STORE_PATH", tmp_path / "reminders.json")


def test_set_reminder_persists_and_returns_id():
    out = reminders.set_reminder("in 2 hours", "Call the dentist")
    assert "id" in out and out["message"] == "Call the dentist"
    listed = reminders.list_reminders()
    assert listed["count"] == 1
    assert listed["reminders"][0]["id"] == out["id"]


def test_set_reminder_rejects_empty_message():
    assert "error" in reminders.set_reminder("in 1 hour", "   ")


def test_set_reminder_rejects_unparseable_time():
    out = reminders.set_reminder("whenever", "x")
    assert "error" in out and "whenever" in out["error"]


def test_list_reminders_sorted_by_due():
    reminders.set_reminder("in 3 hours", "later")
    reminders.set_reminder("in 1 hour", "sooner")
    listed = reminders.list_reminders()["reminders"]
    assert [r["message"] for r in listed] == ["sooner", "later"]


def test_cancel_reminder():
    rid = reminders.set_reminder("in 1 hour", "drop me")["id"]
    assert reminders.cancel_reminder(rid) == {"cancelled": True, "id": rid}
    assert reminders.list_reminders()["count"] == 0


def test_cancel_unknown_id():
    out = reminders.cancel_reminder("deadbeef")
    assert out["cancelled"] is False


def test_get_due_only_returns_past_due():
    reminders.set_reminder("in 5 minutes", "soon")
    reminders.set_reminder("in 3 hours", "far")
    # 1 hour from now: the 5-minute reminder is due, the 3-hour one isn't.
    due = reminders.get_due(now=datetime.now(_TZ) + timedelta(hours=1))
    assert [r["message"] for r in due] == ["soon"]


def test_complete_removes_fired():
    r1 = reminders.set_reminder("in 1 minute", "a")["id"]
    reminders.set_reminder("in 2 hours", "b")
    assert reminders.complete([r1]) == 1
    remaining = reminders.list_reminders()["reminders"]
    assert [r["message"] for r in remaining] == ["b"]
