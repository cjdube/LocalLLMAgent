"""Fetch yesterday's Strava activities and log them to Google Calendar.
Non-interactive — run by launchd.

Usage:
    python -m tasks.daily_log
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.loop import run_agent
from agent.tools.calendar import LOG_TOOL_SCHEMA as CALENDAR_LOG_SCHEMA, log_calendar_event
from agent.tools.strava import TOOL_SCHEMA as STRAVA_SCHEMA, fetch_strava
from tasks._common import setup_logger

SYSTEM_PROMPT = """You are Craig's Strava-to-calendar logging assistant, running \
unattended with no human available to answer questions.

1. Call fetch_strava with date="yesterday".
2. For every activity returned, call log_calendar_event once per activity using:
   - summary: the activity name
   - start / end: build ISO 8601 datetimes from the activity's date + start_time/end_time
   - description: include distance_km, duration_minutes, and elevation_gain_m
   - color_id: "4" (Flamingo — all Strava activities use this)
   - source_id: the activity's strava_id field, always — this prevents duplicate \
     calendar events if this task ever runs more than once for the same day
3. If fetch_strava returns zero activities, do not call log_calendar_event at all — \
   just report that there was nothing to log.

Do not ask any questions. Do not wait for confirmation. If a tool call errors, note it \
and continue with any remaining activities.
"""


def main() -> int:
    logger = setup_logger("daily_log")
    logger.info("Starting daily log run")

    try:
        result = run_agent(
            system_prompt=SYSTEM_PROMPT,
            user_prompt="Log yesterday's Strava activities to the calendar.",
            tools=[STRAVA_SCHEMA, CALENDAR_LOG_SCHEMA],
            dispatch={
                "fetch_strava": fetch_strava,
                "log_calendar_event": log_calendar_event,
            },
            logger=logger,
        )
        logger.info(f"Agent final response: {result}")
        logger.info("Daily log run complete")
        return 0
    except Exception as e:
        logger.exception(f"Daily log run failed: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
