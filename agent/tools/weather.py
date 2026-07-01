"""Fetch OpenWeatherMap forecast for a given location.

Calls the 5-day / 3-hour forecast endpoint with cnt=8 (next 24 hours)
and parses it into a flat JSON blob.

Usage:
    python -m agent.tools.weather
    python -m agent.tools.weather --location "Boston,MA,US"
    python -m agent.tools.weather --location "London,GB" --units metric

Key resolution order: --api-key arg > config/.env file > OPENWEATHERMAP_API_KEY env var
"""

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen
from urllib.error import URLError, HTTPError

from dotenv import load_dotenv

_ENV_PATH = Path(__file__).resolve().parent.parent.parent / "config" / ".env"
load_dotenv(_ENV_PATH)

FORECAST_URL = "https://api.openweathermap.org/data/2.5/forecast"

TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "fetch_weather",
        "description": "Get the current weather and 24-hour forecast for a location.",
        "parameters": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "City,State,Country e.g. 'Newfields,NH,US'. Omit to use the default location.",
                },
                "units": {
                    "type": "string",
                    "enum": ["imperial", "metric"],
                },
            },
        },
    },
}


def fetch_forecast(location: str, units: str, api_key: str) -> dict:
    params = {"q": location, "appid": api_key, "units": units, "cnt": 8}
    url = f"{FORECAST_URL}?{urlencode(params)}"
    with urlopen(url, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def parse(raw: dict) -> dict:
    entries = raw.get("list", [])
    if not entries:
        raise ValueError("forecast list empty")

    current = entries[0]
    temps = [e["main"]["temp"] for e in entries]
    pops = [e.get("pop", 0) for e in entries]
    any_precip = any(("rain" in e) or ("snow" in e) for e in entries)

    high = round(max(temps))
    low = round(min(temps))
    max_pop_pct = round(max(pops) * 100)
    desc = current["weather"][0]["description"]

    summary_parts = [f"{desc.capitalize()}."]
    if any_precip and max_pop_pct > 0:
        summary_parts.append(f"Up to {max_pop_pct}% chance of precipitation.")
    summary_parts.append(f"High {high}, low {low}.")
    summary = " ".join(summary_parts)

    city = raw.get("city", {})
    location_str = ", ".join(p for p in [city.get("name"), city.get("country")] if p)

    return {
        "location": location_str,
        "as_of": datetime.fromtimestamp(current["dt"]).isoformat(),
        "current": {
            "temp_f": round(current["main"]["temp"]),
            "feels_like_f": round(current["main"]["feels_like"]),
            "description": desc,
            "humidity_pct": current["main"].get("humidity"),
            "wind_mph": round(current.get("wind", {}).get("speed", 0)),
        },
        "next_24h": {
            "high_f": high,
            "low_f": low,
            "max_precip_pct": max_pop_pct,
            "any_precip": any_precip,
            "summary": summary,
        },
    }


def _normalize_location(location: str) -> str:
    """OpenWeatherMap's city lookup wants 'City,ST,US' with no spaces. Models
    tend to pass loosely formatted strings like 'Newfields, NH' — coerce those
    into the strict format rather than relying on the model to get it right."""
    parts = [p.strip() for p in location.split(",") if p.strip()]
    if len(parts) == 2 and len(parts[1]) == 2 and parts[1].isalpha():
        parts.append("US")
    return ",".join(parts)


def fetch_weather(location: str = None, units: str = "imperial", api_key: str = None) -> dict:
    """Callable entrypoint used by the agent loop's tool dispatcher."""
    api_key = api_key or os.getenv("OPENWEATHERMAP_API_KEY")
    if not api_key:
        return {"error": "OPENWEATHERMAP_API_KEY not set (checked arg, config/.env, env var)"}

    location = location or os.getenv("DEFAULT_LOCATION", "Newfields,NH,US")
    location = _normalize_location(location)

    try:
        raw = fetch_forecast(location, units, api_key)
    except HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.reason}"}
    except URLError as e:
        return {"error": f"network error: {e.reason}"}
    except Exception as e:
        return {"error": f"fetch error: {e}"}

    try:
        return parse(raw)
    except Exception as e:
        return {"error": f"parse error: {e}"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--location", default="Newfields,NH,US")
    parser.add_argument("--units", default="imperial")
    parser.add_argument("--api-key", dest="api_key", default=None)
    args = parser.parse_args()

    result = fetch_weather(args.location, args.units, args.api_key)
    print(json.dumps(result, indent=2))
    return 1 if "error" in result else 0


if __name__ == "__main__":
    sys.exit(main())
