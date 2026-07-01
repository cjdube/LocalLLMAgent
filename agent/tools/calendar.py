"""Read upcoming events / log activities to Google Calendar.

Usage:
    python -m agent.tools.calendar list --max-results 10
    python -m agent.tools.calendar log --summary "Morning Workout" --start "2026-06-30T08:52:00" --end "2026-06-30T09:24:00"
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from googleapiclient.discovery import build

from agent.tools.google_auth import get_credentials

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


def _service():
    creds = get_credentials()
    return build("calendar", "v3", credentials=creds)


def get_upcoming_events(hours_ahead: int = 24) -> dict:
    calendar_id = os.getenv("GOOGLE_CALENDAR_ID", "primary")
    now = datetime.utcnow()
    time_min = now.isoformat() + "Z"
    time_max = (now + timedelta(hours=hours_ahead)).isoformat() + "Z"

    try:
        service = _service()
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
        events.append({"summary": e.get("summary", "(no title)"), "start": start, "end": end})

    return {"event_count": len(events), "events": events}


def get_events_in_range(time_min: str, time_max: str) -> dict:
    """List events between two ISO 8601 datetimes (inclusive), with colorId —
    used by tasks/weekly_learnings.py to categorize a past week's events."""
    calendar_id = os.getenv("GOOGLE_CALENDAR_ID", "primary")

    try:
        service = _service()
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
            "summary": e.get("summary", "(no title)"),
            "start": start,
            "end": end,
            "colorId": e.get("colorId"),
        })

    return {"event_count": len(events), "events": events}


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
        service = _service()

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
