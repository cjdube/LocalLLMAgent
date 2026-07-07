"""Read upcoming events / log activities to Google Calendar.

Usage:
    python -m agent.tools.calendar list --max-results 10
    python -m agent.tools.calendar log --summary "Morning Workout" --start "2026-06-30T08:52:00" --end "2026-06-30T09:24:00"
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from agent.dates import DATE_ARG_GUIDANCE, resolve_date
from agent.tools.google_auth import build_service

_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(_ROOT / "config" / ".env")

LIST_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_upcoming_events",
        "description": "Get upcoming Google Calendar events for the next N hours.",
        "parameters": {
            "type": "object",
            "properties": {
                "hours_ahead": {"type": "integer", "description": "How many hours ahead to look (default 24)."},
            },
        },
    },
}

LOG_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "log_calendar_event",
        "description": "Create a Google Calendar event, e.g. to log a Strava activity.",
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "start": {"type": "string", "description": "ISO 8601 local datetime"},
                "end": {"type": "string", "description": "ISO 8601 local datetime"},
                "description": {"type": "string"},
                "color_id": {"type": "string", "description": "Google Calendar colorId, e.g. '4' for Flamingo"},
                "source_id": {
                    "type": "string",
                    "description": "Stable external id (e.g. Strava activity id) used to avoid creating duplicate events if this tool is called more than once for the same activity.",
                },
            },
            "required": ["summary", "start", "end"],
        },
    },
}

# Single source of truth for category -> (colorId, color name). Also used by
# tasks/calendar_colorizer.py to build its classification prompt.
CATEGORY_COLORS = {
    "Work/LLC": ("1", "Lavender"),
    "AARP": ("9", "Blueberry"),
    "Fitness": ("4", "Flamingo"),
    "Meal Prep": ("10", "Basil"),
    "Domestic/Chores": ("5", "Banana"),
    "Meetings": ("3", "Grape"),
    "Travel": ("7", "Peacock"),
    "Appointments": ("6", "Tangerine"),
    "Uncategorized": ("11", "Tomato"),
}

RECOLOR_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "recolor_event",
        "description": "Recolor an existing Google Calendar event by category.",
        "parameters": {
            "type": "object",
            "properties": {
                "event_id": {
                    "type": "string",
                    "description": "The event's id, from get_upcoming_events or get_events_by_date.",
                },
                "category": {"type": "string", "enum": list(CATEGORY_COLORS.keys())},
            },
            "required": ["event_id", "category"],
        },
    },
}

GET_BY_DATE_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_events_by_date",
        "description": "Get Google Calendar events (including colorId) between two dates, "
        "inclusive — for past or future dates, not just what's upcoming.",
        "parameters": {
            "type": "object",
            "properties": {
                "start": {"type": "string", "description": "Start date. " + DATE_ARG_GUIDANCE},
                "end": {"type": "string", "description": "End date. " + DATE_ARG_GUIDANCE},
            },
            "required": ["start", "end"],
        },
    },
}


def get_upcoming_events(hours_ahead: int = 24) -> dict:
    now = datetime.now(timezone.utc)
    time_min = now.isoformat().replace("+00:00", "Z")
    time_max = (now + timedelta(hours=hours_ahead)).isoformat().replace("+00:00", "Z")

    # Delegates to get_events_in_range() and projects down to this tool's
    # narrower schema (just summary/start/end) — the underlying events().list
    # call is identical.
    result = get_events_in_range(time_min, time_max)
    if "error" in result:
        return result

    events = [
        {"summary": e["summary"], "start": e["start"], "end": e["end"]}
        for e in result["events"]
    ]
    return {"event_count": len(events), "events": events}


def get_events_in_range(time_min: str, time_max: str) -> dict:
    """List events between two ISO 8601 datetimes (inclusive), with colorId —
    used by tasks/weekly_learnings.py to categorize a past week's events."""
    calendar_id = os.getenv("GOOGLE_CALENDAR_ID", "primary")

    try:
        service = build_service("calendar", "v3")
        result = (
            service.events()
            .list(
                calendarId=calendar_id,
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
    except Exception as e:
        return {"error": str(e)}

    events = []
    for e in result.get("items", []):
        start = e.get("start", {}).get("dateTime", e.get("start", {}).get("date"))
        end = e.get("end", {}).get("dateTime", e.get("end", {}).get("date"))
        events.append({
            "id": e.get("id"),
            "summary": e.get("summary", "(no title)"),
            "start": start,
            "end": end,
            "colorId": e.get("colorId"),
            "status": e.get("status"),
        })

    return {"event_count": len(events), "events": events}


def get_events_by_date(start: str, end: str) -> dict:
    """Chat-friendly wrapper over get_events_in_range() — takes dates in any
    form agent.dates.resolve_date() accepts ('today'/'yesterday', a bare
    'MM-DD', or a full 'YYYY-MM-DD') and builds the full-day ISO 8601 range in
    local time, so the model never has to know the current year or construct a
    timezone-aware datetime itself.

    Uses prefer="nearest" for bare month/day input: calendars are queried
    forward at least as often as backward, so "July 10th" asked on July 7th
    must resolve to *this* year's July 10th, not last year's (which would
    silently return no events)."""
    tz = ZoneInfo(_local_timezone())
    today = datetime.now(tz).date()
    start = resolve_date(start, today=today, prefer="nearest")
    end = resolve_date(end, today=today, prefer="nearest")
    start_dt = datetime.fromisoformat(start).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=tz)
    end_dt = datetime.fromisoformat(end).replace(hour=23, minute=59, second=59, microsecond=999999, tzinfo=tz)
    return get_events_in_range(start_dt.isoformat(), end_dt.isoformat())


def _local_timezone() -> str:
    """Resolve the system's IANA timezone name (e.g. 'America/New_York').
    Google Calendar rejects abbreviations like 'EDT', so we read the real
    zoneinfo path via /etc/localtime rather than relying on tzinfo.__str__."""
    override = os.getenv("TIMEZONE")
    if override:
        return override
    try:
        resolved = Path("/etc/localtime").resolve()
        parts = resolved.parts
        idx = parts.index("zoneinfo")
        return "/".join(parts[idx + 1 :])
    except (OSError, ValueError):
        return "UTC"


def log_calendar_event(
    summary: str,
    start: str,
    end: str,
    description: str = "",
    color_id: str = None,
    source_id: str = None,
) -> dict:
    calendar_id = os.getenv("GOOGLE_CALENDAR_ID", "primary")
    tz = _local_timezone()

    try:
        service = build_service("calendar", "v3")

        if source_id:
            existing = (
                service.events()
                .list(
                    calendarId=calendar_id,
                    privateExtendedProperty=f"source_id={source_id}",
                    singleEvents=True,
                )
                .execute()
            )
            items = existing.get("items", [])
            if items:
                return {
                    "event_id": items[0]["id"],
                    "html_link": items[0].get("htmlLink"),
                    "skipped": "event already logged for this source_id",
                }

        body = {
            "summary": summary,
            "description": description,
            "start": {"dateTime": start, "timeZone": tz},
            "end": {"dateTime": end, "timeZone": tz},
        }
        if color_id:
            body["colorId"] = color_id
        if source_id:
            body["extendedProperties"] = {"private": {"source_id": source_id}}

        created = service.events().insert(calendarId=calendar_id, body=body).execute()
    except Exception as e:
        return {"error": str(e)}

    return {"event_id": created.get("id"), "html_link": created.get("htmlLink")}


def set_event_color(event_id: str, color_id: str) -> dict:
    """Patch just the colorId of an existing event — used by
    tasks/calendar_colorizer.py to recolor yesterday's events by category."""
    calendar_id = os.getenv("GOOGLE_CALENDAR_ID", "primary")

    try:
        service = build_service("calendar", "v3")
        service.events().patch(
            calendarId=calendar_id, eventId=event_id, body={"colorId": color_id}
        ).execute()
    except Exception as e:
        return {"error": str(e)}

    return {"event_id": event_id, "color_id": color_id, "updated": True}


def recolor_event(event_id: str, category: str) -> dict:
    """Recolor an existing event by category name (see CATEGORY_COLORS) —
    the chat-callable counterpart to set_event_color()."""
    entry = CATEGORY_COLORS.get(category)
    if entry is None:
        return {"error": f"unknown category '{category}', must be one of {list(CATEGORY_COLORS)}"}
    color_id, _ = entry
    return set_event_color(event_id, color_id)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list")
    p_list.add_argument("--hours-ahead", type=int, default=24)

    p_log = sub.add_parser("log")
    p_log.add_argument("--summary", required=True)
    p_log.add_argument("--start", required=True)
    p_log.add_argument("--end", required=True)
    p_log.add_argument("--description", default="")
    p_log.add_argument("--color-id", default=None)

    args = parser.parse_args()

    if args.cmd == "list":
        result = get_upcoming_events(args.hours_ahead)
    else:
        result = log_calendar_event(args.summary, args.start, args.end, args.description, args.color_id)

    print(json.dumps(result, indent=2))
    return 1 if "error" in result else 0


if __name__ == "__main__":
    sys.exit(main())
