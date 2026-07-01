"""Fetch Strava activities via Composio.

Usage:
    python -m agent.tools.strava --date today
    python -m agent.tools.strava --date yesterday  (default)
    python -m agent.tools.strava --date 2026-05-01

Key resolution order: --arg > config/.env file > env var
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

_ENV_PATH = Path(__file__).resolve().parent.parent.parent / "config" / ".env"
load_dotenv(_ENV_PATH)

from composio import Composio

TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "fetch_strava",
        "description": "Get Strava activities for a specific date.",
        "parameters": {
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "Date as YYYY-MM-DD, 'today', or 'yesterday'.",
                },
            },
            "required": ["date"],
        },
    },
}


def _get_activities(
    composio_api_key: Optional[str],
    strava_user_id: Optional[str],
    strava_connected_account_id: Optional[str],
    days_back: int = 30,
) -> list:
    api_key = composio_api_key or os.getenv("COMPOSIO_API_KEY")
    user_id = strava_user_id or os.getenv("STRAVA_USER_ID")
    connected_account_id = strava_connected_account_id or os.getenv("STRAVA_CONNECTED_ACCOUNT_ID")

    client = Composio(api_key=api_key)
    result = client.tools.execute(
        slug="STRAVA_LIST_ATHLETE_ACTIVITIES",
        arguments={},
        connected_account_id=connected_account_id,
        user_id=user_id,
        dangerously_skip_version_check=True,
    )

    raw_activities = result.get("data", {}).get("details", [])
    cutoff_date = (datetime.now() - timedelta(days=days_back)).date()

    formatted_activities = []
    for activity in raw_activities:
        start_date_str = activity.get("start_date", "")
        elapsed_seconds = activity.get("elapsed_time", 0)
        start_dt = activity_date = end_dt = None
        if start_date_str:
            try:
                start_dt = datetime.fromisoformat(start_date_str.replace("Z", "+00:00")).astimezone()
                activity_date = start_dt.date()
                end_dt = start_dt + timedelta(seconds=elapsed_seconds)
            except ValueError:
                pass

        if activity_date and activity_date < cutoff_date:
            continue

        formatted_activities.append(
            {
                "id": activity.get("id"),
                "name": activity.get("name", "Unknown Activity"),
                "type": activity.get("type", "Unknown"),
                "date": activity_date.isoformat() if activity_date else start_date_str,
                "start_time": start_dt.strftime("%H:%M") if start_dt else None,
                "end_time": end_dt.strftime("%H:%M") if end_dt else None,
                "distance_km": round(activity.get("distance", 0) / 1000, 2),
                "duration_minutes": elapsed_seconds // 60,
                "elevation_gain_m": activity.get("total_elevation_gain", 0),
            }
        )

    return formatted_activities


def fetch_strava(
    date: str = "yesterday",
    composio_api_key: str = None,
    strava_user_id: str = None,
    strava_connected_account_id: str = None,
) -> dict:
    """Callable entrypoint used by the agent loop's tool dispatcher."""
    target_date = date
    if target_date == "today":
        target_date = datetime.now().strftime("%Y-%m-%d")
    elif target_date == "yesterday":
        target_date = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    try:
        activities = _get_activities(composio_api_key, strava_user_id, strava_connected_account_id)
    except Exception as e:
        return {"activity_count": 0, "activities": [], "error": str(e)}

    target_activities = [a for a in activities if a.get("date", "").startswith(target_date)]

    formatted = []
    for activity in target_activities:
        formatted.append(
            {
                "strava_id": activity.get("id"),
                "name": activity.get("name"),
                "type": activity.get("type"),
                "date": activity.get("date"),
                "start_time": activity.get("start_time"),
                "end_time": activity.get("end_time"),
                "distance_km": activity.get("distance_km"),
                "duration_minutes": activity.get("duration_minutes"),
                "elevation_gain_m": activity.get("elevation_gain_m"),
                "category": "Fitness",
                "colorId": 4,  # Flamingo
            }
        )

    return {"date": target_date, "activity_count": len(formatted), "activities": formatted}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", type=str, default="yesterday")
    parser.add_argument("--composio-api-key", dest="composio_api_key", default=None)
    parser.add_argument("--strava-user-id", dest="strava_user_id", default=None)
    parser.add_argument("--strava-connected-account-id", dest="strava_connected_account_id", default=None)
    args = parser.parse_args()

    result = fetch_strava(
        args.date, args.composio_api_key, args.strava_user_id, args.strava_connected_account_id
    )
    print(json.dumps(result, indent=2))
    return 1 if "error" in result else 0


if __name__ == "__main__":
    sys.exit(main())
