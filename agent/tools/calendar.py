"""Read upcoming events / log activities to Google Calendar.

Usage:
    python -m agent.tools.calendar list --max-results 10
    python -m agent.tools.calendar log --summary "Morning Workout" --start "2026-06-30T08:52:00" --end "2026-06-30T09:24:00"
"""

import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from agent import prefs
from agent.dates import DATE_ARG_GUIDANCE, local_timezone as _local_timezone, resolve_date
from agent.tools.google_auth import build_service

_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(_ROOT / "config" / ".env")

# source_id prefix for the events scribe/claude_time_blocks.py logs. It lives here,
# next to log_calendar_event (which owns source_id), so the writer and the
# colorizer that must leave those events alone can share it without either task
# importing the other.
SESSION_BLOCK_SOURCE_PREFIX = "claude-time:"

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
                    "description": "Stable external id (e.g. Strava activity id) used to avoid creating duplicate events.",
                },
            },
            "required": ["summary", "start", "end"],
        },
    },
}

# get_events_by_date's own budget. Only the chat path is capped;
# get_events_in_range stays whole for scribe/calendar_colorizer.py and
# scribe/daily_chrome_learnings.py, which need every event and have no context
# window to protect. Measured on the real calendar: ~3.2 events a day, so the
# old uncapped result blew the 8000-char tool-result cap at about ten days —
# a 7-week ask returned 181 events and 39KB, of which the model saw a fifth and
# reported as the whole calendar. 50 covers a fortnight whole.
MAX_CHAT_EVENTS = 50

# ...and a char budget beside it, because a count cap alone doesn't bound the
# result: event titles vary enough that the same 50 events ran 7827 chars over
# one range and 8849 over another. The count cap is the cheap guard; this is the
# one that actually holds. Same belt-and-braces pair as wiki.search_wiki.
MAX_CHAT_EVENT_CHARS = 6000

# Carried by get_events_in_range for the colorizer and the learnings task. Chat
# needs none of them, and at ~90 chars an event they were most of the overflow.
# `id` stays: scribe/calendar_colorizer.py patches events by id.
_TASK_ONLY_EVENT_FIELDS = ("colorId", "status", "source_id")

# Single source of truth for category -> (colorId, color name), defined in
# config/preferences.json. Also used by scribe/calendar_colorizer.py to build
# its classification prompt.
CATEGORY_COLORS = {
    c["name"]: (c["color_id"], c["color_name"]) for c in prefs.calendar_categories()
}

GET_BY_DATE_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "get_events_by_date",
        "description": "Get Google Calendar events (including colorId) between two dates, "
        "inclusive — for past or future dates, not just what's upcoming. The result's "
        "'range' field names the day(s) actually looked up: state that date in your "
        "reply rather than one you worked out yourself.",
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
    used by scribe/daily_chrome_learnings.py to categorize the prior day's events.

    Does NOT page: no maxResults, no pageToken loop, so Google's default page
    size of 250 is a silent ceiling — a wider range returns exactly 250 events
    and reports that as the total. Measured 2026-08-16, Jan 1 to Aug 16 returns
    exactly 250, so the real calendar is already sitting on it at ~7.5 months of
    history. Deferred deliberately, not missed: the callers that matter are all
    narrow (the colorizer does yesterday, the learnings task a day), and
    get_upcoming_events looks forward where the calendar is sparse. The exposure
    is a multi-month *past* range through get_events_by_date. Add the pageToken
    loop here when that question starts mattering — the chat-side caps above
    already bound what reaches the model, so nothing else has to change."""
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
            # The id log_calendar_event stamped on events Wren created, so a
            # caller can tell its own writes apart from the user's (None for a
            # hand-made event). scribe/calendar_colorizer.py uses it to skip the
            # session blocks, which arrive already colored.
            "source_id": e.get("extendedProperties", {}).get("private", {}).get("source_id"),
        })

    return {"event_count": len(events), "events": events}


def get_events_by_date(start: str, end: str) -> dict:
    """Chat-friendly wrapper over get_events_in_range() — takes dates in any
    form agent.dates.resolve_date() accepts ('today'/'tomorrow'/'yesterday', a
    weekday phrase like 'next tuesday', a bare 'MM-DD', or a full 'YYYY-MM-DD')
    and builds the full-day ISO 8601 range in local time, so the model never has
    to know the current year, do weekday arithmetic, or construct a
    timezone-aware datetime itself.

    Uses prefer="nearest" for bare month/day input: calendars are queried
    forward at least as often as backward, so "July 10th" asked on July 7th
    must resolve to *this* year's July 10th, not last year's (which would
    silently return no events).

    The result carries the resolved dates back (`resolved_start`/`resolved_end`
    and a human `range`) because the model otherwise narrates the day from its
    own memory: it once reported an empty "Tuesday, August 19th" for a lookup it
    had aimed at the 19th, a Wednesday. Echoing the tool's own date makes a
    mis-aimed lookup visible in the reply instead of self-consistent."""
    tz = ZoneInfo(_local_timezone())
    today = datetime.now(tz).date()
    start = resolve_date(start, today=today, prefer="nearest")
    end = resolve_date(end, today=today, prefer="nearest")
    try:
        start_dt = datetime.fromisoformat(start).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=tz)
        end_dt = datetime.fromisoformat(end).replace(hour=23, minute=59, second=59, microsecond=999999, tzinfo=tz)
    except ValueError:
        # An expression resolve_date() couldn't place. Degrade to an error the
        # model can relay rather than raising, which would 500 the chat turn.
        return {"error": f"couldn't understand the date range {start!r} to {end!r}"}

    # A backwards range returns nothing at all, which reads exactly like a free
    # day. Seen live: building "the week of next Monday" the model paired a
    # start of next Monday with an end of the *nearest* Sunday, the day before.
    if start_dt > end_dt:
        return {"error": f"the range runs backwards: {start} is after {end}"}

    result = get_events_in_range(start_dt.isoformat(), end_dt.isoformat())
    if "error" in result:
        return result

    events = result["events"]
    shown, total = [], 0
    for e in events[:MAX_CHAT_EVENTS]:
        lean = {k: v for k, v in e.items() if k not in _TASK_ONLY_EVENT_FIELDS}
        total += len(str(lean))
        if total > MAX_CHAT_EVENT_CHARS and shown:
            break
        shown.append(lean)
    out = {
        "resolved_start": start_dt.date().isoformat(),
        "resolved_end": end_dt.date().isoformat(),
        "range": _human_range(start_dt.date(), end_dt.date()),
        # The true total, ahead of the list: if anything downstream still trims,
        # the count is what has to survive.
        "event_count": result["event_count"],
        "events_shown": len(shown),
    }
    if len(shown) < len(events):
        # Events come back ordered by start time, so what's missing is the far
        # end of the range — which reads exactly like a free fortnight.
        out["partial"] = (
            f"Only the first {len(shown)} of {result['event_count']} events fit; "
            f"the last one shown starts {shown[-1]['start']}. The rest of the range "
            "was NOT checked — do not describe it as free. Ask about a narrower "
            "range to see the rest."
        )
    out["events"] = shown
    return out


def _human_range(start: date, end: date) -> str:
    """'Tuesday, August 18, 2026' for a single day, or 'Monday, August 17, 2026
    through Friday, August 21, 2026' for a span — the phrasing the model should
    repeat back."""
    fmt = "%A, %B %-d, %Y"
    if start == end:
        return start.strftime(fmt)
    return f"{start.strftime(fmt)} through {end.strftime(fmt)}"


def _human_when(start: str, end: str) -> str:
    """'Wednesday, August 19, 2026, 10:00 AM to 11:00 AM' for the ISO pair that
    was actually written, echoed back so the model's reply quotes the tool
    rather than restating the time from its own memory — and so it can see the
    write landed at all. Same role as _human_due in google_tasks.py.

    Falls back to 'start to end' for anything Google accepted but datetime
    can't parse; this is a display string, and a write must never fail on it."""
    try:
        s = datetime.fromisoformat(start)
        e = datetime.fromisoformat(end)
    except (ValueError, TypeError):
        return f"{start} to {end}"
    day = s.strftime("%A, %B %-d, %Y")
    if e.date() != s.date():
        return f"{day}, {s.strftime('%-I:%M %p')} to {e.strftime('%A, %B %-d, %Y')}, {e.strftime('%-I:%M %p')}"
    return f"{day}, {s.strftime('%-I:%M %p')} to {e.strftime('%-I:%M %p')}"


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

    # Echo the summary and the human time back: without them the result was
    # just two opaque ids, so the model had no evidence its event existed and
    # re-issued the write, drawing a second confirmation card (see
    # loop.MAX_GATED_PAUSES_PER_TURN). Additive — the task callers only test for
    # an "error" key.
    return {
        "created": True,
        "summary": summary,
        "when": _human_when(start, end),
        "event_id": created.get("id"),
        "html_link": created.get("htmlLink"),
    }


def set_event_color(event_id: str, color_id: str) -> dict:
    """Patch just the colorId of an existing event — used by
    scribe/calendar_colorizer.py to recolor yesterday's events by category."""
    calendar_id = os.getenv("GOOGLE_CALENDAR_ID", "primary")

    try:
        service = build_service("calendar", "v3")
        service.events().patch(
            calendarId=calendar_id, eventId=event_id, body={"colorId": color_id}
        ).execute()
    except Exception as e:
        return {"error": str(e)}

    return {"event_id": event_id, "color_id": color_id, "updated": True}


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
