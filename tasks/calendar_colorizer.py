"""Recolor yesterday's Google Calendar events by category. Non-interactive —
run by launchd every day at 5pm, covering the day that just ended.

Usage:
    python -m tasks.calendar_colorizer
"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.loop import complete_text
from agent.tools.calendar import _local_timezone, get_events_in_range, set_event_color
from agent.tools.email import send_email
from tasks._common import setup_logger

VALID_COLOR_IDS = {"1", "9", "4", "10", "5", "3", "7", "6", "11"}

CLASSIFY_SYSTEM_PROMPT = """You are Craig's calendar color-coding assistant. You are given a \
JSON list of yesterday's calendar events, each with an "id" and a "summary" (title). For \
each event, decide which category it belongs to and return the matching Google Calendar \
colorId, using EXACTLY this mapping:

| Category | Color name | colorId |
|----------|-----------|---------|
| Work / LLC | Lavender | 1 |
| AARP | Blueberry | 9 |
| Fitness | Flamingo | 4 |
| Meal Prep | Basil | 10 |
| Domestic / Chores | Banana | 5 |
| Meetings with others | Grape | 3 |
| Travel / Vacation | Peacock | 7 |
| Appointments (doctor, dentist, car, etc.) | Tangerine | 6 |
| Uncategorized (only if genuinely unable to tell) | Tomato | 11 |

Best-guess every event from its title. Only use colorId "11" when you genuinely cannot tell \
what category an event belongs to — do not use it as a default.

Output ONLY a single JSON object mapping each event's "id" to its chosen colorId string, \
nothing else — no preamble, no explanation, no markdown code fences. Example:
{"abc123": "1", "def456": "6"}
"""


def _yesterday_range() -> tuple[datetime, datetime]:
    tz = ZoneInfo(_local_timezone())
    today = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday = today - timedelta(days=1)
    return yesterday, today - timedelta(microseconds=1)


def main() -> int:
    logger = setup_logger("calendar_colorizer")
    logger.info("Starting calendar colorizer run")

    try:
        start, end = _yesterday_range()
        logger.info(f"Yesterday's range: {start.isoformat()} to {end.isoformat()}")

        events_result = get_events_in_range(start.isoformat(), end.isoformat())
        logger.info(f"get_events_in_range -> {events_result}")
        if "error" in events_result:
            raise RuntimeError(f"get_events_in_range failed: {events_result['error']}")

        events = [
            e for e in events_result.get("events", [])
            if e.get("summary") and e.get("status") != "cancelled"
        ]

        if not events:
            logger.info("No events to color yesterday — nothing to do")
            return 0

        classify_input = [{"id": e["id"], "summary": e["summary"]} for e in events]
        raw_response = complete_text(
            system_prompt=CLASSIFY_SYSTEM_PROMPT,
            user_prompt=json.dumps(classify_input),
        )
        logger.info(f"Raw classification response: {raw_response}")

        try:
            classification = json.loads(raw_response)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Could not parse model response as JSON: {e}\nRaw: {raw_response}")

        updated, skipped = [], []
        for event in events:
            event_id = event["id"]
            color_id = classification.get(event_id)
            if color_id not in VALID_COLOR_IDS:
                logger.warning(f"No valid color for event {event_id} ({event['summary']!r}), skipping")
                skipped.append(event["summary"])
                continue

            result = set_event_color(event_id, color_id)
            logger.info(f"set_event_color({event_id}, {color_id}) -> {result}")
            if "error" in result:
                skipped.append(event["summary"])
            else:
                updated.append((event["summary"], color_id))

        logger.info(f"Colorizer run complete: {len(updated)} updated, {len(skipped)} skipped")
        return 0
    except Exception as e:
        logger.exception(f"Calendar colorizer run failed: {e}")
        try:
            send_email(
                subject="Calendar colorizer run failed",
                body=f"tasks.calendar_colorizer raised an exception:\n\n{e}",
            )
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
