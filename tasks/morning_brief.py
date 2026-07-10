"""Compose and send the morning brief email. Non-interactive — run by launchd.

Weather and calendar data are fetched directly (deterministic), the local LLM
writes only short blurbs from that data (e.g. the "at a glance" line), and the
HTML is assembled in Python — this keeps the layout/icons reliable regardless
of how well the small local model follows HTML formatting instructions.

The whole pipeline lives in build_and_send_brief(), which is shared by two
callers: this scheduled task (main, below) and the chat server's
send_morning_brief tool (see SEND_BRIEF_TOOL_SCHEMA), so an on-request brief
from chat produces byte-for-byte the same email the scheduled run sends.

Usage:
    python -m tasks.morning_brief
"""

import html
import json
import logging
import os
import re
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from agent.dates import local_timezone
from agent.loop import complete_text
from agent.tools.calendar import get_upcoming_events
from agent.tools.email import send_email
from agent.tools.github_starred import fetch_starred_repos
from agent.tools.google_tasks import get_tasks_due_soon
from agent.tools.weather import fetch_weather
from tasks._common import notify_failure, setup_logger, today_str

_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / "config" / ".env")

DEFAULT_LOCATION = os.getenv("DEFAULT_LOCATION", "Newfields,NH,US")
STARRED_STATE_PATH = _ROOT / "config" / "github_starred_state.json"

GLANCE_SYSTEM_PROMPT = """You write a single short "Today at a Glance" blurb for a \
personal morning brief email. Given the day's weather and calendar events as JSON, \
write 1-2 plain sentences summarizing the day. No markdown, no headers, no quotes \
around the output — just the sentences themselves. Be concise and friendly."""

STARRED_REPOS_SYSTEM_PROMPT = """You write a single short intro sentence for the "Starred \
Repos" section of a personal morning brief email. Given a JSON list of GitHub repos the \
user has starred that were pushed to recently (name, description, pushed_at), write 1 \
plain sentence naming the most notable update. No markdown, no links, no quotes around \
the output — just the sentence itself. Be concise and neutral."""


def _read_starred_state() -> Optional[str]:
    try:
        return json.loads(STARRED_STATE_PATH.read_text()).get("last_checked")
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _write_starred_state(last_checked: str) -> None:
    # Atomic write: temp file in the same dir, then os.replace() — so a crash
    # mid-write can't leave a truncated state file behind.
    fd, tmp = tempfile.mkstemp(dir=STARRED_STATE_PATH.parent, prefix=".github_starred_state.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump({"last_checked": last_checked}, f)
        os.replace(tmp, STARRED_STATE_PATH)
    except BaseException:
        os.unlink(tmp)
        raise

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
    .overdue { color: #b91c1c; }
    .intro { margin: 0 0 8px; }
    a { color: #2563eb; text-decoration: none; }
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
            dt = datetime.fromisoformat(start)
            time_str = f"{dt.strftime('%b %-d')}, {dt.strftime('%-I:%M %p')}"
        except ValueError:
            time_str = start
        summary = html.escape(e.get("summary", "(no title)"))
        items.append(f"<li><strong>{time_str}</strong> — {summary}</li>")
    return "<ul>" + "".join(items) + "</ul>"


def _tasks_html(tasks: list, error: str = None, today: date = None) -> str:
    if error:
        return f'<span class="empty">Tasks unavailable: {html.escape(error)}</span>'
    if not tasks:
        return '<span class="empty">Nothing past due or due soon.</span>'
    today = today or datetime.now(ZoneInfo(local_timezone())).date()
    items = []
    for t in tasks:
        due_str = t.get("due") or ""
        try:
            due_date = datetime.fromisoformat(due_str.replace("Z", "+00:00")).date()
        except ValueError:
            due_date = None
        if due_date is None:
            label_html = ""
        else:
            if due_date < today:
                label_class, label_text = ' class="overdue"', "Overdue"
            elif due_date == today:
                label_class, label_text = "", "Today"
            else:
                label_class, label_text = "", due_date.strftime("%a %b %-d")
            label_html = f"<strong{label_class}>{html.escape(label_text)}</strong> — "
        title = html.escape(t.get("title", "(no title)"))
        list_name = t.get("list")
        list_html = f' <span style="color:#9ca3af;">({html.escape(list_name)})</span>' if list_name else ""
        items.append(f"<li>{label_html}{title}{list_html}</li>")
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


def _safe_url(url: str) -> str:
    """Return url only if it's an http(s) link, else "". Guards against
    javascript:/data: (or other) schemes in externally-sourced URLs —
    html.escape() alone does not neutralize a dangerous scheme."""
    try:
        return url if urlparse(url).scheme in ("http", "https") else ""
    except (ValueError, AttributeError):
        return ""


def _clean_snippet(text: str, max_len: int = 160) -> str:
    text = re.sub(r"#+\s*", "", text)  # strip markdown heading markers
    text = re.sub(r"\s+", " ", text).strip()  # collapse newlines/whitespace
    if len(text) > max_len:
        text = text[:max_len].rsplit(" ", 1)[0] + "…"
    return text


def _starred_repos_html(repos: list, intro_text: str, error: str = None) -> str:
    if error:
        return f'<span class="empty">Starred repos unavailable: {html.escape(error)}</span>'
    if not repos:
        return '<span class="empty">No updates to your starred repos since last check.</span>'
    items = []
    for r in repos:
        name = html.escape(r.get("full_name") or r.get("name", "(unnamed)"))
        safe_url = _safe_url(r.get("html_url", ""))
        # Prefer a summary of what actually changed (release notes / recent
        # commit subjects) over the repo's static description, which is what
        # Craig asked for — the description alone doesn't say what's new.
        # recent_changes is already sized (including its "+N more" count
        # suffix) by the tool, so only the raw description needs cleaning —
        # re-truncating recent_changes here would chop the suffix mid-word.
        changes = r.get("recent_changes") or _clean_snippet(r.get("description") or "")
        changes_html = f" — {html.escape(changes)}" if changes else ""
        name_html = f'<a href="{html.escape(safe_url)}">{name}</a>' if safe_url else name
        items.append(f"<li>{name_html}{changes_html}</li>")
    intro_html = f'<p class="intro">{html.escape(intro_text)}</p>' if intro_text else ""
    return intro_html + "<ul>" + "".join(items) + "</ul>"


def render_brief_html(
    weather: dict,
    events: list,
    tasks: list,
    glance_text: str,
    starred_repos: list,
    starred_intro: str,
    starred_error: str = None,
    tasks_error: str = None,
) -> str:
    date_str = datetime.now().strftime("%A, %B %-d")
    sections = (
        _section("☀️", "Today at a Glance", html.escape(glance_text) or "No summary available.")
        + _section("\U0001F4C5", "Calendar", _events_html(events))
        + _section("✅", "Tasks Due Soon", _tasks_html(tasks, tasks_error))
        + _section("\U0001F324️", "Weather", _weather_html(weather))
        + _section("⭐", "Starred Repos", _starred_repos_html(starred_repos, starred_intro, starred_error))
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


# Exposed so the chat agent can trigger the real deterministic pipeline
# on request instead of freehand-composing a brief-like email itself —
# see build_and_send_brief() below.
SEND_BRIEF_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "send_morning_brief",
        "description": (
            "Build and send Craig's morning brief email right now (weather, "
            "calendar, tasks due soon, starred repo updates), using the same "
            "polished HTML layout as the scheduled morning brief. Use this "
            "whenever Craig asks to send or resend the morning brief — do NOT "
            "compose that email yourself with send_email, since freehand text "
            "loses the formatting."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}


def build_and_send_brief(logger: Optional[logging.Logger] = None) -> dict:
    """Fetch weather/calendar/tasks/starred-repo data, render the HTML brief,
    and send it. Shared by the scheduled task (main, below) and the chat
    agent's send_morning_brief tool so both paths produce identical output."""
    if logger:
        logger.info("Starting morning brief run")

    try:
        weather = fetch_weather(location=DEFAULT_LOCATION)
        if logger:
            logger.info(f"fetch_weather -> {weather}")

        events_result = get_upcoming_events(hours_ahead=24)
        if logger:
            logger.info(f"get_upcoming_events -> {events_result}")
        events = events_result.get("events", [])

        tasks_result = get_tasks_due_soon(hours_ahead=48)
        if logger:
            logger.info(f"get_tasks_due_soon -> {tasks_result}")
        tasks_error = tasks_result.get("error")
        tasks = [] if tasks_error else tasks_result.get("tasks", [])

        glance_text = complete_text(
            system_prompt=GLANCE_SYSTEM_PROMPT,
            user_prompt=f"weather: {weather}\ncalendar_events: {events}",
        )
        if logger:
            logger.info(f"glance summary -> {glance_text}")

        # Capture "now" before the fetch so the window written to state after a
        # successful send never skips activity that happened during this run.
        starred_check_time = datetime.now(timezone.utc).isoformat()
        starred_since = _read_starred_state() or (
            datetime.now(timezone.utc) - timedelta(hours=24)
        ).isoformat()
        starred_result = fetch_starred_repos(since=starred_since)
        if logger:
            logger.info(f"fetch_starred_repos(since={starred_since}) -> {starred_result}")
        starred_error = starred_result.get("error")
        starred_repos = starred_result.get("repos", [])

        starred_intro = ""
        if starred_repos and not starred_error:
            starred_intro = complete_text(
                system_prompt=STARRED_REPOS_SYSTEM_PROMPT,
                user_prompt=f"starred_repo_updates: {starred_repos}",
            )
            if logger:
                logger.info(f"starred repos intro -> {starred_intro}")

        body_html = render_brief_html(
            weather,
            events,
            tasks,
            glance_text,
            starred_repos,
            starred_intro,
            starred_error,
            tasks_error,
        )
        result = send_email(
            subject=f"Morning Brief - {today_str()}",
            body=body_html,
            html=True,
        )
        if logger:
            logger.info(f"send_email -> {result}")

        if "error" not in result and not starred_error:
            _write_starred_state(starred_check_time)

        if logger:
            logger.info("Morning brief run complete")
        return result
    except Exception as e:
        if logger:
            logger.exception(f"Morning brief run failed: {e}")
        return {"error": str(e)}


def main() -> int:
    logger = setup_logger("morning_brief")
    result = build_and_send_brief(logger=logger)
    if "error" in result:
        # Push only from the scheduled run — the chat send_morning_brief tool
        # shares build_and_send_brief() but the user is present there to see
        # the error, so the alert lives here in main(), not in the shared path.
        notify_failure("morning_brief", result["error"], logger)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
