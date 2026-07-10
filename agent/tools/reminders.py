"""Scheduled reminders — Wren stores a future time + message; a launchd sweeper
(tasks/reminder_sweep.py) fires it as a phone push via notify() when it comes due.

The chat model sets/lists/cancels reminders through the tools below; it passes
Craig's time expression verbatim and resolve_reminder_time() does the date math
in Python (the model can't be trusted to). Fired reminders are removed from the
store — the push itself is the record.

State lives in config/reminders.json, written atomically under a cross-process
file lock (agent/store.py) so the Flask chat server and the separate sweeper
process never read a half-written file or clobber each other's updates.

Usage:
    python -m agent.tools.reminders --list
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from agent.dates import REMINDER_WHEN_GUIDANCE, local_timezone, resolve_reminder_time
from agent.store import atomic_write_json, load_json, locked

_ROOT = Path(__file__).resolve().parent.parent.parent
_STORE_PATH = _ROOT / "config" / "reminders.json"


SET_REMINDER_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "set_reminder",
        "description": "Set a reminder that will push a notification to Craig's phone at a future "
        "time. Use this whenever Craig asks to be reminded of something later. The reminder fires "
        "once, then is cleared.",
        "parameters": {
            "type": "object",
            "properties": {
                "when": {"type": "string", "description": "When to fire the reminder. " + REMINDER_WHEN_GUIDANCE},
                "message": {
                    "type": "string",
                    "description": "What to remind Craig about, phrased as the reminder text he'll "
                    "see (e.g. 'Call the dentist').",
                },
            },
            "required": ["when", "message"],
        },
    },
}

LIST_REMINDERS_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "list_reminders",
        "description": "List Craig's pending reminders (soonest first), with their id and due time. "
        "Use this before cancelling one, or when Craig asks what reminders he has set.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

CANCEL_REMINDER_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "cancel_reminder",
        "description": "Cancel a pending reminder by its id (get the id from list_reminders first).",
        "parameters": {
            "type": "object",
            "properties": {
                "reminder_id": {"type": "string", "description": "The id of the reminder to cancel."},
            },
            "required": ["reminder_id"],
        },
    },
}


def _load() -> dict:
    return load_json(_STORE_PATH, {"reminders": []})


def _save(data: dict) -> None:
    # Atomic write via agent.store — the sweeper process never reads a
    # half-written store.
    atomic_write_json(_STORE_PATH, data)


def _remove_ids(ids: set) -> int:
    """Drop reminders whose id is in `ids`; return how many were removed."""
    with locked(_STORE_PATH):
        data = _load()
        kept = [r for r in data["reminders"] if r["id"] not in ids]
        removed = len(data["reminders"]) - len(kept)
        if removed:
            data["reminders"] = kept
            _save(data)
        return removed


def _human_due(due_iso: str) -> str:
    try:
        return datetime.fromisoformat(due_iso).strftime("%a %b %-d, %-I:%M %p")
    except ValueError:
        return due_iso


def set_reminder(when: str, message: str) -> dict:
    message = (message or "").strip()
    if not message:
        return {"error": "reminder message was empty"}
    due = resolve_reminder_time(when)
    if due is None:
        return {"error": f"couldn't understand the time {when!r} — try 'in 2 hours', "
                "'3pm', 'tomorrow 9am', or 'YYYY-MM-DD HH:MM'"}

    reminder = {
        "id": uuid4().hex[:8],
        "due": due.isoformat(),
        "message": message,
        "created": datetime.now(ZoneInfo(local_timezone())).isoformat(),
    }
    with locked(_STORE_PATH):
        data = _load()
        data["reminders"].append(reminder)
        _save(data)
    return {"id": reminder["id"], "message": message, "due": _human_due(reminder["due"])}


def list_reminders() -> dict:
    with locked(_STORE_PATH):
        reminders = sorted(_load()["reminders"], key=lambda r: r["due"])
    return {
        "count": len(reminders),
        "reminders": [
            {"id": r["id"], "message": r["message"], "due": _human_due(r["due"])}
            for r in reminders
        ],
    }


def cancel_reminder(reminder_id: str) -> dict:
    if _remove_ids({reminder_id}):
        return {"cancelled": True, "id": reminder_id}
    return {"cancelled": False, "error": f"no pending reminder with id {reminder_id!r}"}


def get_due(now: datetime = None) -> list:
    """Reminders whose due time has arrived (for the sweeper). Read-only —
    the sweeper removes them via complete() only after the push succeeds, so a
    failed push is retried on the next sweep."""
    now = now or datetime.now(ZoneInfo(local_timezone()))
    with locked(_STORE_PATH):
        reminders = _load()["reminders"]
    return [r for r in reminders if datetime.fromisoformat(r["due"]) <= now]


def complete(ids) -> int:
    """Remove reminders that have been successfully fired."""
    return _remove_ids(set(ids))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args()
    if args.list:
        print(json.dumps(list_reminders(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
