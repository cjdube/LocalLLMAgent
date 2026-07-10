"""Tests for the reminder sweeper. The reminders store is redirected to a tmp
file and notify() is stubbed, so no real push is sent."""

import pytest

from agent.tools import reminders
from tasks import reminder_sweep


@pytest.fixture(autouse=True)
def _isolate_store(tmp_path, monkeypatch):
    monkeypatch.setattr(reminders, "_STORE_PATH", tmp_path / "reminders.json")


def _stub_notify(monkeypatch, *, fail=False):
    """Capture notify() calls; return the list of captured (message, title)."""
    calls = []

    def fake_notify(message, title=None, priority=None):
        calls.append((message, title))
        return {"error": "down"} if fail else {"ok": True}

    monkeypatch.setattr(reminder_sweep, "notify", fake_notify)
    return calls


def test_fires_due_reminder_and_clears_it(monkeypatch):
    reminders.set_reminder("2020-01-01 00:00", "past due")  # already in the past
    calls = _stub_notify(monkeypatch)

    assert reminder_sweep.main() == 0
    assert calls == [("past due", "Reminder")]
    assert reminders.list_reminders()["count"] == 0  # cleared after firing


def test_leaves_future_reminder_untouched(monkeypatch):
    reminders.set_reminder("in 3 hours", "later")
    calls = _stub_notify(monkeypatch)

    assert reminder_sweep.main() == 0
    assert calls == []
    assert reminders.list_reminders()["count"] == 1


def test_failed_push_keeps_reminder_for_retry(monkeypatch):
    reminders.set_reminder("2020-01-01 00:00", "retry me")
    calls = _stub_notify(monkeypatch, fail=True)

    assert reminder_sweep.main() == 0
    assert calls == [("retry me", "Reminder")]
    assert reminders.list_reminders()["count"] == 1  # not cleared — will retry
