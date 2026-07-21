"""Tests for agent/tools/schedule.py — Wren's view of her own scheduled tasks.

The schedule data layer (chat.insights) is monkeypatched so these never depend
on the real launchd/*.plist files or logs/.
"""

import agent.tools.schedule as schedule


def _fake_insights(monkeypatch, tasks, runs_by_log=None):
    runs_by_log = runs_by_log or {}
    import chat.insights as insights

    monkeypatch.setattr(insights, "discover_tasks", lambda: tasks)
    monkeypatch.setattr(insights, "next_run", lambda sci, now=None: "2026-07-22T06:00")
    monkeypatch.setattr(insights, "parse_runs", lambda log_path, limit=None: runs_by_log.get(log_path, []))


def test_splits_scheduled_from_daemons(monkeypatch):
    tasks = [
        {"display_name": "Morning Brief", "human_schedule": "Daily 6:00 AM",
         "schedule": {"Hour": 6}, "log_path": "mb.log", "is_daemon": False},
        {"display_name": "Bg Worker", "human_schedule": "Every 30s (poll)",
         "schedule": None, "log_path": "bg.log", "is_daemon": True},
    ]
    runs = {"mb.log": [{"status": "ok", "start": "2026-07-21T06:09:00"}]}
    _fake_insights(monkeypatch, tasks, runs)

    out = schedule.list_scheduled_tasks()

    assert out["always_on"] == ["Bg Worker"]
    assert len(out["tasks"]) == 1
    task = out["tasks"][0]
    assert task["name"] == "Morning Brief"
    assert task["schedule"] == "Daily 6:00 AM"
    assert task["next_run"] == "Wed Jul 22, 6:00 AM"
    assert task["last_status"] == "ok"
    assert task["last_run"] == "Tue Jul 21, 6:09 AM"


def test_task_with_no_runs_reports_null_status(monkeypatch):
    tasks = [
        {"display_name": "Starred Blurbs", "human_schedule": "Sundays 8:00 PM",
         "schedule": {"Weekday": 0, "Hour": 20}, "log_path": "sb.log", "is_daemon": False},
    ]
    _fake_insights(monkeypatch, tasks, {})

    out = schedule.list_scheduled_tasks()

    assert out["tasks"][0]["last_status"] is None
    assert out["tasks"][0]["last_run"] is None


def test_humanize_handles_bad_input():
    assert schedule._humanize(None) is None
    assert schedule._humanize("not-a-date") == "not-a-date"
