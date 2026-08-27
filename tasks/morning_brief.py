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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from agent import prefs
from agent.dates import local_timezone
from agent.loop import complete_text, resolve_backend, warm_model
from agent.store import atomic_write_json, load_json
from agent.tools.calendar import get_upcoming_events
from agent.tools.clickup import backlog_digest
from agent.tools.email import send_email
from agent.tools.github_starred import fetch_starred_repos
from agent.tools.google_tasks import get_tasks_due_soon
from agent.tools.sports import fetch_scores
from agent.tools.weather import fetch_weather
from tasks._common import notify_failure, setup_logger, today_str
from tasks._urls import safe_url

_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / "config" / ".env")

# Env wins; otherwise the location from config/preferences.json.
DEFAULT_LOCATION = os.getenv("DEFAULT_LOCATION") or prefs.PREFS.get("location", "")
# How far ahead the Calendar section looks, from config/preferences.json.
CALENDAR_HOURS_AHEAD = prefs.brief_calendar_hours()
STARRED_STATE_PATH = _ROOT / "config" / "github_starred_state.json"
CLICKUP_STATE_PATH = _ROOT / "config" / "clickup_state.json"

# The user's name, for the model-facing send_morning_brief description below.
_NAME = prefs.user_name()

GLANCE_SYSTEM_PROMPT = """You write a single short "Today at a Glance" blurb for a \
personal morning brief email. You are given the weather, today's calendar events \
(events_today), and a separate list of events on later days (events_later). The two \
lists are already sorted for you and each event carries the day it falls on — never \
describe an event from events_later as happening today. Write 1-2 plain sentences: \
first the day's weather, then today's events. If events_today is empty, say there is \
nothing scheduled today. Only if something in events_later is worth a heads-up, add \
one short clause that names its day. No markdown, no headers, no quotes around the \
output — just the sentences themselves. Be concise and friendly."""

STARRED_REPOS_SYSTEM_PROMPT = """You write a single short intro sentence for the "Starred \
Repos" section of a personal morning brief email. Given a JSON list of GitHub repos the \
user has starred that were pushed to recently (name, description, pushed_at), write 1 \
plain sentence naming the most notable update. No markdown, no links, no quotes around \
the output — just the sentence itself. Be concise and neutral."""


def _read_starred_state() -> Optional[str]:
    # Single-writer state (only the brief run writes it), so no locked() —
    # atomic writes alone make lock-free reads safe.
    return load_json(STARRED_STATE_PATH, {}).get("last_checked")


def _write_starred_state(last_checked: str) -> None:
    # Atomic write via agent.store — a crash mid-write can't leave a truncated
    # state file behind.
    atomic_write_json(STARRED_STATE_PATH, {"last_checked": last_checked})


def _read_clickup_state() -> Optional[int]:
    # Same single-writer, atomic-write shape as the starred cursor above. Unix
    # milliseconds, because that is the unit ClickUp's date_updated_gt takes —
    # stored in the API's own unit so nothing has to convert it back.
    value = load_json(CLICKUP_STATE_PATH, {}).get("last_checked_ms")
    return int(value) if value else None


def _write_clickup_state(last_checked_ms: int) -> None:
    atomic_write_json(CLICKUP_STATE_PATH, {"last_checked_ms": int(last_checked_ms)})

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


def _events_html(events: list, hours_ahead: int = None) -> str:
    if not events:
        hours = CALENDAR_HOURS_AHEAD if hours_ahead is None else hours_ahead
        return f'<span class="empty">Nothing on the calendar in the next {hours} hours.</span>'
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


def _glance_buckets(events: list, today: date = None) -> tuple[list, list]:
    """Split upcoming events into today's and later days', each event compacted
    to {"when": <rendered day/time>, "summary": ...} for the glance prompt.

    The calendar window is wider than a day, so without this the model would
    have to work out both "is this today?" and "which day is it?" from ISO
    timestamps — date math it gets wrong. Starts are UTC-offset (Google) or a
    bare date for all-day events; both are read in the local zone, since an
    evening event is the case where the UTC day and the local day disagree.
    An unparseable start sinks into `later`: the failure worth avoiding is
    announcing something as today's when it isn't."""
    tz = ZoneInfo(local_timezone())
    today = today or datetime.now(tz).date()
    todays, later = [], []
    for e in events:
        summary = e.get("summary", "(no title)")
        try:
            dt = datetime.fromisoformat(e.get("start", ""))
        except ValueError:
            later.append({"when": "date unknown", "summary": summary})
            continue
        # All-day events parse to a naive datetime (a bare date), timed ones to
        # an aware one — so tzinfo is what tells the two apart.
        if dt.tzinfo is None:
            day, time_str = dt.date(), ""
        else:
            local = dt.astimezone(tz)
            day, time_str = local.date(), f" {local.strftime('%-I:%M %p')}"
        if day == today:
            label = "today"
        elif day == today + timedelta(days=1):
            label = "tomorrow"
        else:
            label = day.strftime("%A, %b %-d")
        (todays if day == today else later).append(
            {"when": f"{label}{time_str}", "summary": summary}
        )
    return todays, later


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


def _score_line(game: dict) -> str:
    """One game as HTML. Everything here is a fact with a fixed layout, so it is
    assembled in Python — the model is never shown a score to restate."""
    team = html.escape(game.get("team", "?"))
    opponent = html.escape(game.get("opponent", "?"))
    prefix = f"Game {game['game_number']}: " if game.get("games_that_day", 1) > 1 else ""
    at = "vs" if game.get("home_away") == "home" else "at"

    team_score, opponent_score = game.get("team_score"), game.get("opponent_score")
    if game.get("final") and team_score is not None and opponent_score is not None:
        verb = {"W": "beat", "L": "lost to", "T": "tied"}.get(game.get("result"), "played")
        body = (
            f"<strong>{team} {team_score}, {opponent} {opponent_score}</strong> — "
            f"{team} {verb} {opponent}"
        )
    else:
        # Postponed, suspended, or still in progress. Rendering the status text
        # rather than a blank score keeps "we don't have a result" distinct from
        # a real one — inventing a score here would be the worst failure.
        status = html.escape(game.get("status") or "no result")
        body = f"{team} {at} {opponent} — {status}"

    url = safe_url(game.get("url", ""))
    suffix = f' <a href="{html.escape(url)}">box score</a>' if url else ""
    return f"<li>{html.escape(prefix)}{body}{suffix}</li>"


def _scores_html(games: list, errors: dict = None) -> str:
    """The Scores section body, or "" when there is nothing to say.

    An empty string means render_brief_html drops the section entirely: the NFL
    is dark about six months a year, so "no games" is the normal state rather
    than the exception, and a permanent empty section reads like a bug. A fetch
    error is not silence, though — it gets a line, because "we couldn't tell"
    and "nobody played" must not look the same."""
    if not games and not errors:
        return ""
    items = [_score_line(g) for g in games]
    items += [
        f'<li class="empty">{html.escape(league.upper())} scores unavailable: '
        f"{html.escape(message)}</li>"
        for league, message in sorted((errors or {}).items())
    ]
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
        url = safe_url(r.get("html_url", ""))
        # Prefer a summary of what actually changed (release notes / recent
        # commit subjects) over the repo's static description, which is what
        # the user asked for — the description alone doesn't say what's new.
        # recent_changes is already sized (including its "+N more" count
        # suffix) by the tool, so only the raw description needs cleaning —
        # re-truncating recent_changes here would chop the suffix mid-word.
        changes = r.get("recent_changes") or _clean_snippet(r.get("description") or "")
        changes_html = f" — {html.escape(changes)}" if changes else ""
        name_html = f'<a href="{html.escape(url)}">{name}</a>' if url else name
        items.append(f"<li>{name_html}{changes_html}</li>")
    intro_html = f'<p class="intro">{html.escape(intro_text)}</p>' if intro_text else ""
    return intro_html + "<ul>" + "".join(items) + "</ul>"


def _backlog_html(digest: dict, error: str = None) -> str:
    """Two stacked lists: what moved in ClickUp since the last brief, then what
    is in flight right now. Both, because they answer different questions and
    each is thin on its own — "what moved" is silent on a quiet day, and "in
    flight" reads the same every morning until something changes.

    No model call. The lists are facts and the labels are written here, so a
    quiet day produces a short section rather than a paraphrase of nothing."""
    if error:
        return f'<span class="empty">Backlog unavailable: {html.escape(error)}</span>'

    moved = digest.get("moved") or []
    in_flight = digest.get("in_flight") or []
    if not moved and not in_flight:
        return '<span class="empty">Nothing in flight and nothing moved since yesterday.</span>'

    parts = []
    if moved:
        rows = "".join(
            f'<li>{html.escape(r.get("title", ""))} — '
            f'<strong>{html.escape(r.get("change", ""))}</strong> '
            f'<span class="empty">({html.escape(r.get("area", ""))})</span></li>'
            for r in moved
        )
        extra = digest.get("moved_total", len(moved)) - len(moved)
        more = f'<p class="intro empty">+{extra} more moved.</p>' if extra > 0 else ""
        parts.append(f'<p class="intro">Moved since yesterday:</p><ul>{rows}</ul>{more}')

    if in_flight:
        rows = "".join(
            f'<li>{html.escape(r.get("title", ""))} — '
            f'{html.escape(r.get("status", ""))} '
            f'<span class="empty">({html.escape(r.get("area", ""))})</span></li>'
            for r in in_flight
        )
        extra = digest.get("in_flight_total", len(in_flight)) - len(in_flight)
        more = f'<p class="intro empty">+{extra} more in flight.</p>' if extra > 0 else ""
        parts.append(f'<p class="intro">In flight:</p><ul>{rows}</ul>{more}')

    stalest = digest.get("stalest")
    if stalest:
        days = stalest.get("days_since_update")
        parts.append(
            f'<p class="intro overdue">Untouched longest: '
            f'{html.escape(stalest.get("title", ""))} ({days} days).</p>'
        )
    return "".join(parts)


def render_brief_html(
    weather: dict,
    events: list,
    tasks: list,
    glance_text: str,
    starred_repos: list,
    starred_intro: str,
    starred_error: str = None,
    tasks_error: str = None,
    scores: list = None,
    scores_errors: dict = None,
    backlog: dict = None,
    backlog_error: str = None,
) -> str:
    date_str = datetime.now().strftime("%A, %B %-d")
    # Scores sit with Tasks (what happened) rather than Calendar (what's coming),
    # and are the one section that disappears when it has nothing — see
    # _scores_html for why.
    scores_body = _scores_html(scores or [], scores_errors)
    sections = (
        _section("☀️", "Today at a Glance", html.escape(glance_text) or "No summary available.")
        + _section("\U0001F4C5", "Calendar", _events_html(events))
        + _section("✅", "Tasks Due Soon", _tasks_html(tasks, tasks_error))
        + _section("\U0001F5C2️", "Backlog", _backlog_html(backlog or {}, backlog_error))
        + (_section("\U0001F3C6", "Scores", scores_body) if scores_body else "")
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
            f"Build and send {_NAME}'s morning brief email right now (weather, "
            "calendar, tasks due soon, yesterday's scores, starred repo updates) "
            "in the same HTML "
            f"layout as the scheduled one. Use whenever {_NAME} asks to send or "
            "resend the morning brief — do NOT compose it yourself with "
            "send_email."
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

        events_result = get_upcoming_events(hours_ahead=CALENDAR_HOURS_AHEAD)
        if logger:
            logger.info(f"get_upcoming_events -> {events_result}")
        events = events_result.get("events", [])

        tasks_result = get_tasks_due_soon(hours_ahead=48)
        if logger:
            logger.info(f"get_tasks_due_soon -> {tasks_result}")
        tasks_error = tasks_result.get("error")
        tasks = [] if tasks_error else tasks_result.get("tasks", [])

        # Yesterday's finals for the teams in config/preferences.json. No model
        # call: scores are facts with a fixed layout, so Python owns them end to
        # end and the glance prompt below is never shown them to restate.
        scores_result = fetch_scores(day="yesterday")
        if logger:
            logger.info(f"fetch_scores -> {scores_result}")
        scores = scores_result.get("games", [])
        scores_errors = scores_result.get("errors")

        brief_backend = resolve_backend("morning_brief")
        # daily_synthesis runs 15 minutes earlier, but on a no-overlap day (its
        # documented common case) it returns without calling the model at all —
        # so the 6am brief finds a cold model on exactly those mornings, and the
        # load stacks on top of prefill inside the streamed call's read timeout.
        warm_model(logger=logger, backend=brief_backend)
        # The calendar window runs past today, so the split happens in Python —
        # the model is told which events are today's rather than deriving it.
        events_today, events_later = _glance_buckets(events)
        if logger:
            logger.info(f"glance buckets -> today={events_today} later={events_later}")
        glance_text = complete_text(
            system_prompt=GLANCE_SYSTEM_PROMPT,
            user_prompt=(
                f"weather: {weather}\n"
                f"events_today: {events_today}\n"
                f"events_later: {events_later}"
            ),
            backend=brief_backend,
            think=False,
            logger=logger,
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
                backend=brief_backend,
                think=False,
                logger=logger,
            )
            if logger:
                logger.info(f"starred repos intro -> {starred_intro}")

        # Same cursor shape as the starred window above: the first ever run has
        # no state, so it looks back 24h rather than reporting the whole
        # backlog's history as "moved yesterday".
        backlog_since = _read_clickup_state() or int(
            (datetime.now(timezone.utc) - timedelta(hours=24)).timestamp() * 1000
        )
        backlog = backlog_digest(since_ms=backlog_since)
        if logger:
            logger.info(f"backlog_digest(since_ms={backlog_since}) -> {backlog}")
        backlog_error = backlog.get("error")

        body_html = render_brief_html(
            weather,
            events,
            tasks,
            glance_text,
            starred_repos,
            starred_intro,
            starred_error,
            tasks_error,
            scores,
            scores_errors,
            backlog,
            backlog_error,
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
        # Advanced independently of the starred cursor: a GitHub outage must not
        # skip a day of ClickUp activity, or the other way round.
        if "error" not in result and not backlog_error and backlog.get("checked_ms"):
            _write_clickup_state(backlog["checked_ms"])

        if logger:
            logger.info("Morning brief run complete")
        return result
    except Exception as e:
        if logger:
            logger.exception(f"Morning brief run failed: {e}")
        return {"error": str(e)}


def brief_dispatch(logger: Optional[logging.Logger] = None):
    """A send_morning_brief dispatch callable bound to `logger` (and ignoring any
    stray kwargs the model emits). The default toolset registry, the chat server,
    and the background worker each need this same wrapper differing only in the
    logger they bind, so defining it once here keeps the three in sync."""
    def _send(**_) -> dict:
        return build_and_send_brief(logger=logger)
    return _send


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
