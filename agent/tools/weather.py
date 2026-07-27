"""Fetch OpenWeatherMap forecast for a given location.

Calls the 5-day / 3-hour forecast endpoint, using cnt to pull back just the
next 24 hours (the default) or, if a longer range is requested, up to the
full 5 days the free tier covers. Parses it into a flat JSON blob.

Usage:
    python -m agent.tools.weather
    python -m agent.tools.weather --location "Boston,MA,US"
    python -m agent.tools.weather --location "London,GB" --units metric
    python -m agent.tools.weather --location "Montreal,QC,CA" --days 4

Key resolution order: --api-key arg > config/.env file > OPENWEATHERMAP_API_KEY env var
"""

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

import requests

from agent import prefs
from agent.tools._http import http_error, load_env, missing_key_error, print_result, resolve_key

load_env()

FORECAST_URL = "https://api.openweathermap.org/data/2.5/forecast"
MAX_DAYS = 5  # ceiling of OpenWeatherMap's free 5-day/3-hour forecast endpoint

TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "fetch_weather",
        "description": "Get the current weather and forecast (up to 5 days ahead) for a location.",
        "parameters": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "City,State,Country e.g. 'Portland,OR,US'. Omit to use the default location.",
                },
                "units": {
                    "type": "string",
                    "enum": ["imperial", "metric"],
                },
                "days": {
                    "type": "integer",
                    "description": "How many days ahead to forecast, 1-5 (default 1, meaning just the next 24 hours). OpenWeatherMap's free forecast only covers 5 days ahead.",
                },
            },
        },
    },
}


def _clamp_days(days: int) -> int:
    return max(1, min(int(days), MAX_DAYS))


def fetch_forecast(location: str, units: str, days: int, api_key: str) -> dict:
    params = {"q": location, "appid": api_key, "units": units, "cnt": _clamp_days(days) * 8}
    resp = requests.get(FORECAST_URL, params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()


def _day_summary(entries: list) -> dict:
    temps = [e["main"]["temp"] for e in entries]
    pops = [e.get("pop", 0) for e in entries]
    any_precip = any(("rain" in e) or ("snow" in e) for e in entries)
    high = round(max(temps))
    low = round(min(temps))
    max_pop_pct = round(max(pops) * 100)
    desc = entries[0]["weather"][0]["description"]

    summary_parts = [f"{desc.capitalize()}."]
    if any_precip and max_pop_pct > 0:
        summary_parts.append(f"Up to {max_pop_pct}% chance of precipitation.")
    summary_parts.append(f"High {high}, low {low}.")

    return {
        "high_f": high,
        "low_f": low,
        "max_precip_pct": max_pop_pct,
        "any_precip": any_precip,
        "summary": " ".join(summary_parts),
    }


def parse(raw: dict, days: int = 1) -> dict:
    entries = raw.get("list", [])
    if not entries:
        raise ValueError("forecast list empty")

    current = entries[0]
    next_24h = _day_summary(entries[:8])
    desc = current["weather"][0]["description"]

    city = raw.get("city", {})
    location_str = ", ".join(p for p in [city.get("name"), city.get("country")] if p)

    result = {
        "location": location_str,
        "as_of": datetime.fromtimestamp(current["dt"]).isoformat(),
        "current": {
            "temp_f": round(current["main"]["temp"]),
            "feels_like_f": round(current["main"]["feels_like"]),
            "description": desc,
            "humidity_pct": current["main"].get("humidity"),
            "wind_mph": round(current.get("wind", {}).get("speed", 0)),
        },
        "next_24h": next_24h,
    }

    if days > 1:
        # Bucket by the location's own calendar date, not the host machine's
        # local timezone — otherwise day boundaries are wrong for any
        # location that isn't in the same timezone as the server.
        tz = timezone(timedelta(seconds=city.get("timezone", 0)))
        buckets: dict[str, list] = {}
        for e in entries:
            local_date = datetime.fromtimestamp(e["dt"], tz=tz).date().isoformat()
            buckets.setdefault(local_date, []).append(e)
        result["daily_forecast"] = [
            {"date": date, **_day_summary(day_entries)} for date, day_entries in buckets.items()
        ]

    return result


def _normalize_location(location: str) -> str:
    """OpenWeatherMap's city lookup wants 'City,ST,US' with no spaces. Models
    tend to pass loosely formatted strings like 'Portland, OR' — coerce those
    into the strict format rather than relying on the model to get it right."""
    parts = [p.strip() for p in location.split(",") if p.strip()]
    if len(parts) == 2 and len(parts[1]) == 2 and parts[1].isalpha():
        parts.append("US")
    return ",".join(parts)


def fetch_weather(location: str = None, units: str = "imperial", days: int = 1, api_key: str = None) -> dict:
    """Callable entrypoint used by the agent loop's tool dispatcher."""
    api_key = resolve_key("OPENWEATHERMAP_API_KEY", api_key)
    if not api_key:
        return missing_key_error("OPENWEATHERMAP_API_KEY")

    # Env wins; otherwise the location from config/preferences.json.
    location = location or os.getenv("DEFAULT_LOCATION") or prefs.PREFS.get("location", "")
    location = _normalize_location(location)
    days = _clamp_days(days)

    try:
        raw = fetch_forecast(location, units, days, api_key)
    except Exception as e:
        return http_error(e)

    try:
        return parse(raw, days)
    except Exception as e:
        return {"error": f"parse error: {e}"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--location", default=None)
    parser.add_argument("--units", default="imperial")
    parser.add_argument("--days", type=int, default=1)
    parser.add_argument("--api-key", dest="api_key", default=None)
    args = parser.parse_args()

    result = fetch_weather(args.location, args.units, args.days, args.api_key)
    return print_result(result)


if __name__ == "__main__":
    sys.exit(main())
