"""Wren's own scheduled tasks — the automated jobs launchd runs on a timer
(morning brief, the daily learnings, the weekly digests, …). This is the same
data the dashboard's Scheduled-tasks table shows, exposed to the chat model so
Wren can answer "what do you run?" / "what's next?" about herself.

It is deliberately distinct from the user's Google Tasks
(agent/tools/google_tasks.py) and their reminders (agent/tools/reminders.py):
those are the user's to-dos; this is Wren's own operating schedule.

Read-only. The schedule/run data is parsed from launchd/*.plist and logs/*.log
by chat.insights (imported lazily so the tool layer doesn't pull in the
dashboard data layer at import time). Degrades to an empty list on any error.

Usage:
    python -m agent.tools.schedule
"""

import json
import sys
from datetime import datetime

from agent import prefs

# The user's name, for the model-facing tool description below. From
# config/preferences.json; falls back to "the user".
_NAME = prefs.user_name()


LIST_SCHEDULED_TASKS_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "list_scheduled_tasks",
        "description": "List your OWN scheduled tasks — the automated jobs you run on a timer "
        "(e.g. the morning brief, the daily Chrome/YouTube learnings, the weekly opportunity "
        "digest and starred-repo blurbs). Each entry gives its schedule, next run time, and "
        f"the status of its last run. Use this when {_NAME} asks what tasks you run, what's "
        f"scheduled, or when something next runs. This is about your own schedule, NOT {_NAME}'s "
        "Google Tasks (get_tasks) or their reminders (list_reminders).",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}


def _humanize(iso: str | None) -> str | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso).strftime("%a %b %-d, %-I:%M %p")
    except (ValueError, TypeError):
        return iso


def list_scheduled_tasks() -> dict:
    """Every launchd-scheduled task with its schedule, next run, and last status,
    plus the names of the always-on daemons. Read-only; returns {"error": ...}
    if the schedule data can't be read (callers treat that as empty)."""
    # Lazy import: chat.insights is the dashboard data layer, kept out of the
    # tool layer's import graph so agent.toolset doesn't drag it in.
    try:
        from chat.insights import discover_tasks, next_run, parse_runs
    except Exception as e:  # pragma: no cover - import guard
        return {"error": f"schedule data unavailable: {e}"}

    scheduled, always_on = [], []
    for task in discover_tasks():
        if task["is_daemon"]:
            always_on.append(task["display_name"])
            continue
        runs = parse_runs(task["log_path"], limit=1)
        last = runs[0] if runs else None
        scheduled.append({
            "name": task["display_name"],
            "schedule": task["human_schedule"],
            "next_run": _humanize(next_run(task["schedule"])),
            "last_status": last["status"] if last else None,
            "last_run": _humanize(last["start"]) if last else None,
        })

    return {"tasks": scheduled, "always_on": always_on}


def main() -> int:
    print(json.dumps(list_scheduled_tasks(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
