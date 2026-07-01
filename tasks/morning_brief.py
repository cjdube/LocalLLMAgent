"""Compose and send the morning brief email. Non-interactive — run by launchd.

Weather and calendar data are fetched directly (deterministic), the local
LLM writes a short "at a glance" summary from that data, and the HTML is
assembled in Python — this keeps the layout/icons reliable regardless of
how well the small local model follows HTML formatting instructions.

Usage:
    python -m tasks.morning_brief
"""

import html
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from agent.loop import complete_text
from agent.tools.calendar import get_upcoming_events
from agent.tools.email import send_email
from agent.tools.weather import fetch_weather
from tasks._common import setup_logger, today_str

_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / "config" / ".env")

DEFAULT_LOCATION = os.getenv("DEFAULT_LOCATION", "Newfields,NH,US")

GLANCE_SYSTEM_PROMPT = """You write a single short "Today at a Glance" blurb for a \
personal morning brief email. Given the day's weather and calendar events as JSON, \
write 1-2 plain sentences summarizing the day. No markdown, no headers, no quotes \
around the output — just the sentences themselves. Be concise and friendly."""

_STYLE = """
  <style>
    body { margin: 0; padding: 0; background: #f4f4f5; }
    .wrap { max-width: 640px; margin: 0 auto; padding: 24px 20px; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif; color: #1f2328; }
    .card { background: #ffffff; border-radius: 12px; padding: 28px 32px; box-shadow: 0 1px 3px rgba(0,0,0,0.06); }
    h1 { font-size: 22px; margin: 0 0 4px; }
    .date { color: #6b7280; font-size: 14px; margin: 0 0 16px; }
    hr { border: none; border-top: 1px solid #e5e7eb; margin: 0 0 20px; }
    .section { margin-bottom: 22px; }
    .section:last-child { margin-bottom: 0; }
    .section-title { font-size: 15px; font-weight: 600; margin: 0 0 8px; }
    .section-body { font-size: 14px; line-height: 1.55; color: #374151; }
    ul { margin: 0; padding-left: 20px; }
    li { margin-bottom: 6px; }
    .empty { color: #9ca3af; font-style: italic; }
  </style>
"""


def _section(icon: str, title: str, body_html: str) -> str:
    return f"""
    <div class="section">
      <p class="section-title">{icon} {html.escape(title)}</p>
      <div class="section-body">{body_html}</div>
    </div>"""


def _events_html(events: list) -> str:
    if not events:
        return '<span class="empty">Nothing on the calendar in the next 24 hours.</span>'
    items = []
    for e in events:
        start = e.get("start", "")
        try:
            time_str = datetime.fromisoformat(start).strftime("%-I:%M %p")
        except ValueError:
            time_str = start
        summary = html.escape(e.get("summary", "(no title)"))
        items.append(f"<li><strong>{time_str}</strong> — {summary}</li>")
    return "<ul>" + "".join(items) + "</ul>"


def _weather_html(weather: dict) -> str:
    if "error" in weather:
        return f'<span class="empty">Weather unavailable: {html.escape(weather["error"])}</span>'
    current = weather.get("current", {})
    next_24h = weather.get("next_24h", {})
    loc = html.escape(weather.get("location", DEFAULT_LOCATION))
    return (
        f"<strong>{loc}</strong> — {html.escape(current.get('description', '').capitalize())}, "
        f"{current.get('temp_f')}&deg;F (feels like {current.get('feels_like_f')}&deg;F)<br>"
        f"High {next_24h.get('high_f')}&deg; / Low {next_24h.get('low_f')}&deg;"
        + (
            f", {next_24h.get('max_precip_pct')}% chance of precipitation"
            if next_24h.get("any_precip")
            else ""
        )
    )


def render_brief_html(weather: dict, events: list, glance_text: str) -> str:
    date_str = datetime.now().strftime("%A, %B %-d")
    sections = (
        _section("☀️", "Today at a Glance", html.escape(glance_text) or "No summary available.")
        + _section("\U0001F4C5", "Calendar", _events_html(events))
        + _section("\U0001F324️", "Weather", _weather_html(weather))
    )
    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8">{_STYLE}</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1>Morning Brief</h1>
      <p class="date">{html.escape(date_str)}</p>
      <hr>
      {sections}
    </div>
  </div>
</body>
</html>"""


def main() -> int:
    logger = setup_logger("morning_brief")
    logger.info("Starting morning brief run")

    try:
        weather = fetch_weather(location=DEFAULT_LOCATION)
        logger.info(f"fetch_weather -> {weather}")

        events_result = get_upcoming_events(hours_ahead=24)
        logger.info(f"get_upcoming_events -> {events_result}")
        events = events_result.get("events", [])

        glance_text = complete_text(
            system_prompt=GLANCE_SYSTEM_PROMPT,
            user_prompt=f"weather: {weather}\ncalendar_events: {events}",
        )
        logger.info(f"glance summary -> {glance_text}")

        body_html = render_brief_html(weather, events, glance_text)
        result = send_email(
            subject=f"Morning Brief - {today_str()}",
            body=body_html,
            html=True,
        )
        logger.info(f"send_email -> {result}")
        logger.info("Morning brief run complete")
        return 0 if "error" not in result else 1
    except Exception as e:
        logger.exception(f"Morning brief run failed: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
