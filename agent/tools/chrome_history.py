"""Fetch Chrome browsing history for a date range.

Opens Chrome's SQLite History database in read-only immutable mode (works
while Chrome is running) and returns meaningful site visits as JSON.

Day boundaries are interpreted in the system's local timezone (not UTC).

Usage:
    python -m agent.tools.chrome_history --days-ago 7
    python -m agent.tools.chrome_history --start 2026-06-22 --end 2026-06-28
"""

import argparse
import json
import platform
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from agent import prefs
from agent.dates import DATE_ARG_GUIDANCE, local_timezone, resolve_date

if platform.system() == "Darwin":
    HISTORY_PATH = Path.home() / "Library/Application Support/Google/Chrome/Default/History"
else:
    HISTORY_PATH = Path.home() / "AppData/Local/Google/Chrome/User Data/Default/History"

# The user's name, for the model-facing tool descriptions below. From
# config/preferences.json; falls back to "the user".
_NAME = prefs.user_name()

NOISE_DOMAINS = {
    "google.com", "www.google.com",
    "gmail.com", "mail.google.com",
    "calendar.google.com",
    "docs.google.com", "drive.google.com",
    "accounts.google.com", "myaccount.google.com",
    "youtube.com", "www.youtube.com",
    "facebook.com", "www.facebook.com",
    "twitter.com", "www.twitter.com", "x.com", "www.x.com",
    "instagram.com", "www.instagram.com",
    "amazon.com", "www.amazon.com",
    "localhost", "127.0.0.1",
}

TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "fetch_chrome_history",
        "description": (
            "Get meaningful (non-noise) Chrome browsing history. Pass 'days_ago' "
            "for a recent window, or an explicit 'start'/'end' range. Day "
            f"boundaries use {_NAME}'s local timezone."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "days_ago": {
                    "type": "integer",
                    "description": (
                        "Days back to look, e.g. 7 for 'the last week' — resolved "
                        "in Python, don't compute a date yourself. Omit if giving "
                        "start/end."
                    ),
                },
                "start": {"type": "string", "description": "Start date (use with 'end'). " + DATE_ARG_GUIDANCE},
                "end": {"type": "string", "description": "End date. " + DATE_ARG_GUIDANCE},
            },
        },
    },
}


def _chrome_ts_to_datetime(chrome_ts: int) -> datetime:
    unix_us = chrome_ts - 11644473600 * 1_000_000
    return datetime.fromtimestamp(unix_us / 1_000_000, tz=timezone.utc)


# Cap on a single page path in the `pages` list; long paths are usually ids or
# slugs whose tail carries no extra signal.
_MAX_PATH_CHARS = 120

# How many sites the chat path returns. Sites come back most-visited first, so
# the cap drops the long tail of one-off visits rather than the day's substance.
# One day of real browsing is ~90 sites and 8897 chars, over the loop's cap.
MAX_CHAT_SITES = 60

# And the budget that actually holds. A site row carries a title and up to
# pages_per_domain paths, so 60 of them ran 8916 chars — still over the cap the
# count was supposed to keep it under. Third time this pattern bit in one pass
# (see calendar.MAX_CHAT_EVENT_CHARS, opportunities._LIST_CHARS): a count cap
# bounds the number of rows, never their size.
MAX_CHAT_SITE_CHARS = 6000


def _extract_domain(url: str) -> str:
    """The host, without port or userinfo. Uses .hostname rather than .netloc:
    netloc keeps the port, which silently defeated the NOISE_DOMAINS match for
    local servers — "127.0.0.1:8420" never equalled "127.0.0.1", so the user's own
    Wren dashboard was reported as a site they'd browsed."""
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def _page_path(url: str) -> str:
    """Path only — no query or fragment, truncated. The path is the signal that
    collapsing to a bare domain throws away: "/gemini-api/docs/models" says what
    the domain and title can't. Query strings are mostly tracking, so drop them."""
    try:
        return (urlparse(url).path or "")[:_MAX_PATH_CHARS]
    except Exception:
        return ""


def _query_history(start: datetime, end: datetime) -> list:
    if not HISTORY_PATH.exists():
        raise FileNotFoundError(f"Chrome History not found at {HISTORY_PATH}")

    start_chrome = int((start.timestamp() + 11644473600) * 1_000_000)
    end_chrome = int((end.timestamp() + 11644473600) * 1_000_000)

    uri = HISTORY_PATH.as_uri() + "?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    try:
        cursor = conn.execute(
            """
            SELECT u.url, u.title, COUNT(v.id) as visit_count
            FROM visits v
            JOIN urls u ON v.url = u.id
            WHERE v.visit_time >= ? AND v.visit_time <= ?
            GROUP BY u.url
            ORDER BY visit_count DESC
            """,
            (start_chrome, end_chrome),
        )
        rows = cursor.fetchall()
    finally:
        conn.close()

    return [{"url": row[0], "title": row[1] or "", "visits": row[2]} for row in rows]


def _filter_and_group(rows: list, pages_per_domain: int = 1) -> list:
    """One entry per domain, carrying its most-visited page's title/url/visits.

    `pages_per_domain` > 1 adds a `pages` list of that domain's top paths — the
    detail the daily learnings review needs to say more than the tab title did.
    It defaults to 1 (no `pages` key) so the tool's result for chat is unchanged:
    a day's output is already near OLLAMA_MAX_TOOL_RESULT_CHARS and must not grow.
    """
    by_domain: dict = {}
    for row in rows:
        domain = _extract_domain(row["url"])
        if not domain or domain in NOISE_DOMAINS:
            continue
        entry = by_domain.get(domain)
        if entry is None:
            by_domain[domain] = {
                "domain": domain,
                "title": row["title"],
                "url": row["url"],
                "visits": row["visits"],
                "_rows": [row],
            }
            continue
        entry["_rows"].append(row)
        if row["visits"] > entry["visits"]:
            entry.update(title=row["title"], url=row["url"], visits=row["visits"])

    out = []
    for entry in by_domain.values():
        rows_for_domain = entry.pop("_rows")
        if pages_per_domain > 1:
            top = sorted(rows_for_domain, key=lambda r: r["visits"], reverse=True)
            # Dedupe by path: several urls differing only in query string collapse
            # to one path here, and repeats would otherwise eat the page budget.
            # A bare "/" or "" is the homepage — the domain already says that.
            pages: dict = {}
            for row in top:
                path = _page_path(row["url"])
                if path in ("", "/") or path in pages:
                    continue
                pages[path] = {"path": path, "visits": row["visits"]}
                if len(pages) == pages_per_domain:
                    break
            if pages:
                entry["pages"] = list(pages.values())
        out.append(entry)
    return sorted(out, key=lambda x: x["visits"], reverse=True)


def fetch_chrome_history(start: str = None, end: str = None, days_ago: int = None,
                         pages_per_domain: int = 1,
                         max_sites: int | None = MAX_CHAT_SITES) -> dict:
    """Callable entrypoint used by the agent loop's tool dispatcher.

    `pages_per_domain` is a Python-only argument, deliberately absent from
    TOOL_SCHEMA: the model never sees it and chat's result shape is unchanged.
    tasks/daily_chrome_learnings.py passes it to get each domain's top paths.

    `max_sites` is Python-only for the same reason, and defaults to capped
    rather than whole: a single day of browsing is 8897 chars, over the loop's
    8000-char tool-result cap, and the cut took `total_meaningful_visits` with
    it — the count is the LAST key, so the model got a short list and nothing
    saying it was short. The two batch callers (tasks/daily_chrome_learnings.py,
    tasks/daily_synthesis.py) pass max_sites=None: they summarize the whole day
    and have no context window to protect. Capped is the default so a new caller
    is safe by accident rather than broken by it.

    Day boundaries are interpreted in the system's local timezone, not UTC, so
    "June 22" means local June 22 rather than a window shifted by the UTC
    offset. Days are resolved in Python (via agent.dates) rather than trusting
    the model to compute a date — same pattern as fetch_strava /
    fetch_starred_repos. Pass either `days_ago` (a recent window) or an
    explicit `start`/`end` range."""
    tz = ZoneInfo(local_timezone())
    today = datetime.now(tz).date()

    if days_ago is not None:
        start_date = (today - timedelta(days=int(days_ago))).isoformat()
        end_date = today.isoformat()
    elif start and end:
        # prefer="past": browsing history only looks backward, so a bare "07-02"
        # resolves to the most recent occurrence, never a future one.
        start_date = resolve_date(start, today=today, prefer="past")
        end_date = resolve_date(end, today=today, prefer="past")
    else:
        return {"error": "provide either days_ago, or both start and end"}

    try:
        start_dt = datetime.fromisoformat(start_date).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=tz)
        end_dt = datetime.fromisoformat(end_date).replace(hour=23, minute=59, second=59, microsecond=999999, tzinfo=tz)
    except ValueError as e:
        return {"error": f"invalid date format: {e}"}

    try:
        rows = _query_history(start_dt, end_dt)
    except FileNotFoundError as e:
        return {"error": str(e)}
    except Exception as e:
        return {"error": f"sqlite error: {e}"}

    sites = _filter_and_group(rows, pages_per_domain=pages_per_domain)
    if max_sites is None:
        shown = sites
    else:
        shown, used = [], 0
        for site in sites[:max_sites]:
            used += len(str(site))
            if used > MAX_CHAT_SITE_CHARS and shown:
                break
            shown.append(site)
    # total_meaningful_visits leads now. It used to be the last key, so the one
    # value that could have told the model its list was short was the first
    # thing a trim removed.
    out = {"range": f"{start_date} to {end_date}",
           "total_meaningful_visits": len(sites),
           "sites_shown": len(shown)}
    if len(shown) < len(sites):
        out["partial"] = (
            f"Only the {len(shown)} most-visited of {len(sites)} sites are listed. "
            "Do not describe this as everything he browsed."
        )
    out["sites"] = shown
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default=None)
    parser.add_argument("--end", default=None)
    parser.add_argument("--days-ago", dest="days_ago", type=int, default=None,
                        help="Recent window instead of an explicit --start/--end range.")
    args = parser.parse_args()

    result = fetch_chrome_history(args.start, args.end, args.days_ago)
    print(json.dumps(result, indent=2))
    return 1 if "error" in result else 0


if __name__ == "__main__":
    sys.exit(main())
