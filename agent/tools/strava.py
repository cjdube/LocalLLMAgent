"""Fetch Strava activities directly from the Strava API.

Uses your own Strava API application (client id/secret) plus a long-lived
refresh token. On each run it exchanges the refresh token for a short-lived
access token, then lists the athlete's recent activities.

Library module, not a registered chat tool. Wren stopped carrying the capture
tools when journaling moved to ScribeJay (scribejay/__init__.py): the callers now are
ScribeJay's daily entries and Wren's tasks/daily_synthesis.py, which import the
function directly. There is deliberately no TOOL_SCHEMA here — if you add one
back, register it in agent/toolset.py or tests/test_toolset.py will fail.

Usage:
    python -m agent.tools.strava --date today
    python -m agent.tools.strava --date yesterday  (default)
    python -m agent.tools.strava --date 2026-05-01
    python -m agent.tools.strava --authorize        (one-time token setup)

Key resolution order: --arg > config/.env file > env var
"""

import argparse
import os
import sys
import urllib.parse
from datetime import datetime, timedelta
from typing import Optional

import requests

from agent.dates import resolve_date
from agent.tools._http import load_env, print_result, resolve_key

load_env()

_TOKEN_URL = "https://www.strava.com/oauth/token"
_AUTHORIZE_URL = "https://www.strava.com/oauth/authorize"
_ACTIVITIES_URL = "https://www.strava.com/api/v3/athlete/activities"
_REDIRECT_URI = "http://localhost"
_SCOPE = "activity:read_all"

def _get_access_token(client_id: str, client_secret: str, refresh_token: str) -> str:
    """Exchange the long-lived refresh token for a short-lived access token."""
    response = requests.post(
        _TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        timeout=30,
    )
    if not response.ok:
        raise RuntimeError(f"Strava token refresh failed ({response.status_code}): {response.text}")
    return response.json()["access_token"]


def _get_activities(
    strava_client_id: Optional[str],
    strava_client_secret: Optional[str],
    strava_refresh_token: Optional[str],
    days_back: int = 30,
) -> list:
    client_id = resolve_key("STRAVA_CLIENT_ID", strava_client_id)
    client_secret = resolve_key("STRAVA_CLIENT_SECRET", strava_client_secret)
    refresh_token = resolve_key("STRAVA_REFRESH_TOKEN", strava_refresh_token)

    if not (client_id and client_secret and refresh_token):
        raise RuntimeError(
            "Missing Strava credentials. Set STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET, and "
            "STRAVA_REFRESH_TOKEN in config/.env (run `python -m agent.tools.strava --authorize`)."
        )

    access_token = _get_access_token(client_id, client_secret, refresh_token)

    cutoff_date = (datetime.now() - timedelta(days=days_back)).date()
    after_epoch = int(datetime.combine(cutoff_date, datetime.min.time()).timestamp())

    response = requests.get(
        _ACTIVITIES_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        params={"after": after_epoch, "per_page": 100},
        timeout=30,
    )
    if not response.ok:
        raise RuntimeError(f"Strava API call failed ({response.status_code}): {response.text}")

    raw_activities = response.json()

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
    strava_client_id: str = None,
    strava_client_secret: str = None,
    strava_refresh_token: str = None,
) -> dict:
    """Callable entrypoint used by the agent loop's tool dispatcher."""
    target_date = resolve_date(date)

    try:
        activities = _get_activities(strava_client_id, strava_client_secret, strava_refresh_token)
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
            }
        )

    return {"date": target_date, "activity_count": len(formatted), "activities": formatted}


def _authorize() -> int:
    """One-time flow: obtain a refresh token to paste into config/.env."""
    client_id = os.getenv("STRAVA_CLIENT_ID")
    client_secret = os.getenv("STRAVA_CLIENT_SECRET")
    if not (client_id and client_secret):
        print(
            "Set STRAVA_CLIENT_ID and STRAVA_CLIENT_SECRET in config/.env first "
            "(from https://www.strava.com/settings/api).",
            file=sys.stderr,
        )
        return 1

    params = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": _REDIRECT_URI,
            "approval_prompt": "force",
            "scope": _SCOPE,
        }
    )
    print("1. Open this URL in your browser and click Authorize:\n")
    print(f"   {_AUTHORIZE_URL}?{params}\n")
    print(
        "2. Your browser will redirect to a 'localhost' URL that fails to load. Copy the\n"
        "   'code' value from that URL's query string (code=...&scope=...).\n"
    )
    code = input("3. Paste the code here: ").strip()

    response = requests.post(
        _TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    if not response.ok:
        print(f"Token exchange failed ({response.status_code}): {response.text}", file=sys.stderr)
        return 1

    refresh_token = response.json().get("refresh_token")
    print("\nSuccess. Add this line to config/.env:\n")
    print(f"   STRAVA_REFRESH_TOKEN={refresh_token}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", type=str, default="yesterday")
    parser.add_argument("--authorize", action="store_true", help="One-time refresh-token setup.")
    parser.add_argument("--strava-client-id", dest="strava_client_id", default=None)
    parser.add_argument("--strava-client-secret", dest="strava_client_secret", default=None)
    parser.add_argument("--strava-refresh-token", dest="strava_refresh_token", default=None)
    args = parser.parse_args()

    if args.authorize:
        return _authorize()

    result = fetch_strava(
        args.date, args.strava_client_id, args.strava_client_secret, args.strava_refresh_token
    )
    return print_result(result)


if __name__ == "__main__":
    sys.exit(main())
