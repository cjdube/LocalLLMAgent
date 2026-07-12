"""View and edit Google Tasks.

Craig keeps tasks spread across several named lists (e.g. Domestic, Travel,
AARP) rather than a single default list, so reads aggregate across every list
on the account by default — set GOOGLE_TASKLIST_ID in config/.env to scope
reads (and the default list new tasks land in) to just one list instead.
Because task ids are scoped per list in the Tasks API, any read result
carries a tasklist_id alongside each task's id, and writes to an existing
task require that tasklist_id back.

Usage:
    python -m agent.tools.google_tasks list --max-results 20
    python -m agent.tools.google_tasks due-soon --hours-ahead 48
    python -m agent.tools.google_tasks create --title "Renew registration" --due tomorrow [--list-name Travel]
    python -m agent.tools.google_tasks set-due --task-id <id> --tasklist-id <list-id> --due 07-15
    python -m agent.tools.google_tasks complete --task-id <id> --tasklist-id <list-id>
"""

import argparse
import json
import os
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from agent.dates import DATE_ARG_GUIDANCE, local_timezone as _local_timezone, resolve_date
from agent.tools.google_auth import build_service

_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(_ROOT / "config" / ".env")

GET_TASKS_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_tasks",
        "description": "Get all open Google Tasks across every task list, regardless of due date. "
        "Each task includes its list name ('list') and a tasklist_id — pass that back to "
        "update_task_due_date/complete_task later.",
        "parameters": {
            "type": "object",
            "properties": {
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of tasks to return per list (default 100).",
                },
            },
        },
    },
}

GET_TASKS_DUE_SOON_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_tasks_due_soon",
        "description": "Get open Google Tasks, across every task list, that are past due or due "
        "within the next N hours. Overdue tasks are always included, no matter how far in the "
        "past. Each returned task includes which list it's in and a tasklist_id.",
        "parameters": {
            "type": "object",
            "properties": {
                "hours_ahead": {"type": "integer", "description": "How many hours ahead to look (default 48)."},
            },
        },
    },
}

CREATE_TASK_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "create_task",
        "description": "Create a new Google Task.",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "notes": {"type": "string"},
                "due": {"type": "string", "description": "Due date, optional. " + DATE_ARG_GUIDANCE},
                "list_name": {
                    "type": "string",
                    "description": "Which task list to create it in, matched by name (the 'list' "
                    "field from a prior get_tasks result). Optional; defaults to the primary list.",
                },
            },
            "required": ["title"],
        },
    },
}

UPDATE_TASK_DUE_DATE_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "update_task_due_date",
        "description": "Change the due date of an existing Google Task.",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "The task's id, from get_tasks or get_tasks_due_soon."},
                "tasklist_id": {
                    "type": "string",
                    "description": "The task's tasklist_id, from the same get_tasks result — task "
                    "ids are only unique within their own list.",
                },
                "due": {"type": "string", "description": DATE_ARG_GUIDANCE},
                "task_title": {
                    "type": "string",
                    "description": "The task's title, echoed back — used only to describe this "
                    "action for confirmation.",
                },
            },
            "required": ["task_id", "tasklist_id", "due"],
        },
    },
}

COMPLETE_TASK_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "complete_task",
        "description": "Mark a Google Task as complete.",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "The task's id, from get_tasks or get_tasks_due_soon."},
                "tasklist_id": {
                    "type": "string",
                    "description": "The task's tasklist_id, from the same get_tasks result — task "
                    "ids are only unique within their own list.",
                },
                "task_title": {
                    "type": "string",
                    "description": "The task's title, echoed back — used only to describe this "
                    "action for confirmation.",
                },
            },
            "required": ["task_id", "tasklist_id"],
        },
    },
}


def _due_soon_cutoff(hours_ahead: int, now: datetime) -> str:
    """RFC 3339 UTC timestamp for the end of the local day that `now + hours_ahead`
    falls on — used as tasks().list()'s dueMax. Tasks' `due` field is
    date-granularity only (always UTC midnight), so rounding the cutoff up to
    end-of-day means "due within 48 hours" captures whole due *dates* (today,
    tomorrow, the day after) rather than an arbitrary mid-day instant that
    would cut off a task due later the same day. `now` must be tz-aware."""
    target_date = (now + timedelta(hours=hours_ahead)).date()
    end_of_day = datetime.combine(target_date, time(23, 59, 59, 999999), tzinfo=now.tzinfo)
    return end_of_day.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _all_tasklists() -> list[dict]:
    """Every Google Tasks list on the account, as [{"id", "title"}, ...],
    paginated. Used both for the default "read every list" behavior and to
    resolve a list name (e.g. "Travel") passed to create_task."""
    service = build_service("tasks", "v1")
    lists, page_token = [], None
    while True:
        result = service.tasklists().list(maxResults=100, pageToken=page_token).execute()
        lists.extend({"id": tl["id"], "title": tl.get("title", "")} for tl in result.get("items", []))
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return lists


def _read_tasklists() -> list[dict]:
    """Which list(s) get_tasks()/get_tasks_due_soon() read from — every list
    on the account by default (see module docstring for why), or just
    GOOGLE_TASKLIST_ID if that's set."""
    override = os.getenv("GOOGLE_TASKLIST_ID")
    if override:
        return [{"id": override, "title": override}]
    return _all_tasklists()


def _resolve_tasklist_id(list_name: str = None) -> str:
    """Which list create_task() should use. An explicit list_name (matched
    case-insensitively against real list titles) wins; otherwise falls back
    to GOOGLE_TASKLIST_ID if set, otherwise Google's '@default' list."""
    if not list_name:
        return os.getenv("GOOGLE_TASKLIST_ID") or "@default"
    for tl in _all_tasklists():
        if tl["title"].strip().lower() == list_name.strip().lower():
            return tl["id"]
    raise ValueError(f"no task list named {list_name!r} — use get_tasks to see existing list names")


def _list_tasks(show_completed: bool = False, due_max: str = None, max_results: int = 100) -> dict:
    """Raw wrapper around tasks().list() shared by get_tasks() and
    get_tasks_due_soon(), aggregated across _read_tasklists(). No dueMin is
    ever passed — a dueMax-only filter (due <= cutoff) already includes
    everything overdue, however far in the past, plus everything due within
    the window; adding a dueMin would incorrectly exclude overdue tasks.
    showCompleted defaults to False since Google's own default (True) would
    otherwise include finished tasks."""
    try:
        service = build_service("tasks", "v1")
        tasklists = _read_tasklists()

        tasks = []
        for tl in tasklists:
            kwargs = {"tasklist": tl["id"], "showCompleted": show_completed, "maxResults": max_results}
            if due_max:
                kwargs["dueMax"] = due_max
            result = service.tasks().list(**kwargs).execute()
            tasks.extend(
                {
                    "id": t.get("id"),
                    "title": t.get("title", "(no title)"),
                    "notes": t.get("notes", ""),
                    "due": t.get("due"),
                    "status": t.get("status"),
                    "tasklist_id": tl["id"],
                    "list": tl["title"],
                }
                for t in result.get("items", [])
            )
    except Exception as e:
        return {"error": str(e)}

    # The Tasks API has no reliable due-date ordering param (unlike Calendar's
    # orderBy), so sort client-side; undated tasks sink to the bottom.
    tasks.sort(key=lambda t: t["due"] or "9999-12-31T23:59:59Z")
    return {"task_count": len(tasks), "tasks": tasks}


def get_tasks(max_results: int = 100) -> dict:
    return _list_tasks(show_completed=False, max_results=max_results)


def get_tasks_due_soon(hours_ahead: int = 48) -> dict:
    """Past-due or due-within-N-hours open tasks — used directly by
    tasks/morning_brief.py and also chat-exposed."""
    tz = ZoneInfo(_local_timezone())
    now = datetime.now(tz)
    due_max = _due_soon_cutoff(hours_ahead, now)
    return _list_tasks(show_completed=False, due_max=due_max, max_results=100)


def create_task(title: str, notes: str = "", due: str = None, list_name: str = None) -> dict:
    try:
        tasklist_id = _resolve_tasklist_id(list_name)
    except ValueError as e:
        return {"error": str(e)}

    body = {"title": title}
    if notes:
        body["notes"] = notes
    if due:
        tz = ZoneInfo(_local_timezone())
        today = datetime.now(tz).date()
        due_date = resolve_date(due, today=today, prefer="future")
        body["due"] = f"{due_date}T00:00:00.000Z"

    try:
        service = build_service("tasks", "v1")
        created = service.tasks().insert(tasklist=tasklist_id, body=body).execute()
    except Exception as e:
        return {"error": str(e)}

    return {
        "task_id": created.get("id"),
        "tasklist_id": tasklist_id,
        "title": created.get("title"),
        "due": created.get("due"),
    }


def update_task_due_date(task_id: str, tasklist_id: str, due: str, task_title: str = "") -> dict:
    tz = ZoneInfo(_local_timezone())
    today = datetime.now(tz).date()
    due_date = resolve_date(due, today=today, prefer="future")

    try:
        service = build_service("tasks", "v1")
        updated = service.tasks().patch(
            tasklist=tasklist_id, task=task_id, body={"due": f"{due_date}T00:00:00.000Z"}
        ).execute()
    except Exception as e:
        return {"error": str(e)}

    return {"task_id": updated.get("id"), "due": updated.get("due")}


def complete_task(task_id: str, tasklist_id: str, task_title: str = "") -> dict:
    try:
        service = build_service("tasks", "v1")
        # Set both status and completed explicitly rather than relying on the
        # server to auto-stamp completed on a bare status patch — that
        # behavior isn't documented for the raw API (as opposed to the Tasks
        # web/mobile clients).
        updated = service.tasks().patch(
            tasklist=tasklist_id,
            task=task_id,
            body={
                "status": "completed",
                "completed": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            },
        ).execute()
    except Exception as e:
        return {"error": str(e)}

    return {"task_id": updated.get("id"), "status": updated.get("status")}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list")
    p_list.add_argument("--max-results", type=int, default=100)

    p_due = sub.add_parser("due-soon")
    p_due.add_argument("--hours-ahead", type=int, default=48)

    p_create = sub.add_parser("create")
    p_create.add_argument("--title", required=True)
    p_create.add_argument("--notes", default="")
    p_create.add_argument("--due", default=None)
    p_create.add_argument("--list-name", default=None)

    p_set_due = sub.add_parser("set-due")
    p_set_due.add_argument("--task-id", required=True)
    p_set_due.add_argument("--tasklist-id", required=True)
    p_set_due.add_argument("--due", required=True)

    p_complete = sub.add_parser("complete")
    p_complete.add_argument("--task-id", required=True)
    p_complete.add_argument("--tasklist-id", required=True)

    args = parser.parse_args()

    if args.cmd == "list":
        result = get_tasks(args.max_results)
    elif args.cmd == "due-soon":
        result = get_tasks_due_soon(args.hours_ahead)
    elif args.cmd == "create":
        result = create_task(args.title, args.notes, args.due, args.list_name)
    elif args.cmd == "set-due":
        result = update_task_due_date(args.task_id, args.tasklist_id, args.due)
    else:
        result = complete_task(args.task_id, args.tasklist_id)

    print(json.dumps(result, indent=2))
    return 1 if "error" in result else 0


if __name__ == "__main__":
    sys.exit(main())
