"""Fetch yesterday's Strava activities and log them to Google Calendar.
Non-interactive — run by launchd.

This is a deterministic field-map: fetch_strava returns activities as dicts,
and each one maps directly onto a calendar event. There's no natural-language
composition here, so no model is involved — that keeps the task reliable (no
dropped activities or mangled datetimes) and leaves no un-gated model->write
path. Mirrors the fetch -> iterate -> write -> summarize shape of the sibling
task scribe/calendar_colorizer.py.

Usage:
    python -m scribe.strava_download
"""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import prefs
from agent.tools.calendar import log_calendar_event
from agent.tools.strava import fetch_strava
from tasks._common import notify_failure, setup_logger

# All Strava activities get the fitness category's color — looked up by role,
# not name, so renaming the category in config/preferences.json can't break it.
FITNESS_COLOR_ID = prefs.category_color_by_role("fitness", "4")


def _log_activity(activity: dict, logger) -> bool:
    """Map one Strava activity onto a calendar event and create it. Returns
    True if logged (or already present), False if skipped/errored."""
    date = activity.get("date")
    start_time = activity.get("start_time")
    end_time = activity.get("end_time")
    if not (date and start_time and end_time):
        logger.warning(
            f"Skipping activity {activity.get('strava_id')} "
            f"({activity.get('name')!r}): missing date/start_time/end_time"
        )
        return False

    start_iso = f"{date}T{start_time}:00"
    # An activity that crosses midnight has end_time (HH:MM) earlier than
    # start_time; roll the end date forward a day so end > start.
    end_date = date
    if end_time < start_time:
        end_date = (datetime.fromisoformat(date) + timedelta(days=1)).date().isoformat()
    end_iso = f"{end_date}T{end_time}:00"

    description = (
        f"Distance: {activity.get('distance_km')} km\n"
        f"Duration: {activity.get('duration_minutes')} min\n"
        f"Elevation gain: {activity.get('elevation_gain_m')} m"
    )
    source_id = str(activity.get("strava_id"))

    result = log_calendar_event(
        summary=activity.get("name"),
        start=start_iso,
        end=end_iso,
        description=description,
        color_id=FITNESS_COLOR_ID,
        source_id=source_id,
    )
    logger.info(
        f"log_calendar_event(summary={activity.get('name')!r}, source_id={source_id}) "
        f"-> {json.dumps(result)}"
    )
    return "error" not in result


def main() -> int:
    logger = setup_logger("strava_download")
    logger.info("Starting Strava download run")

    try:
        result = fetch_strava(date="yesterday")
        logger.info(f"fetch_strava(date=yesterday) -> {json.dumps(result)}")
        if result.get("error"):
            raise RuntimeError(f"fetch_strava failed: {result['error']}")

        activities = result.get("activities", [])
        if not activities:
            logger.info("No Strava activities yesterday — nothing to log")
            logger.info("Strava download run complete")
            return 0

        logged = 0
        for activity in activities:
            if _log_activity(activity, logger):
                logged += 1

        logger.info(f"Logged {logged} of {len(activities)} activities")
        if logged < len(activities):
            # A partial failure used to read as clean success, so a
            # persistently malformed activity was skipped forever with no
            # alert. Still exit 0 — the logged ones are done, and re-runs
            # can't duplicate them (source_id dedupe) — but push the miss.
            notify_failure(
                "strava_download",
                f"{len(activities) - logged} of {len(activities)} activities "
                "failed to log — see logs/strava_download.log",
                logger,
            )
        logger.info("Strava download run complete")
        return 0
    except Exception as e:
        logger.exception(f"Strava download run failed: {e}")
        notify_failure("strava_download", e, logger)
        return 1


if __name__ == "__main__":
    sys.exit(main())
