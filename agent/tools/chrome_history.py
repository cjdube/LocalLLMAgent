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

from agent.dates import DATE_ARG_GUIDANCE, local_timezone, resolve_date

if platform.system() == "Darwin":
    HISTORY_PATH = Path.home() / "Library/Application Support/Google/Chrome/Default/History"
else:
    HISTORY_PATH = Path.home() / "AppData/Local/Google/Chrome/User Data/Default/History"

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
            "boundaries use Craig's local timezone."
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


def _extract_domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
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


def _filter_and_group(rows: list) -> list:
    by_domain: dict = {}
    for row in rows:
        domain = _extract_domain(row["url"])
        if not domain or domain in NOISE_DOMAINS:
            continue
        if domain not in by_domain or row["visits"] > by_domain[domain]["visits"]:
            by_domain[domain] = {
                "domain": domain,
                "title": row["title"],
                "url": row["url"],
                "visits": row["visits"],
            }
    return sorted(by_domain.values(), key=lambda x: x["visits"], reverse=True)


def fetch_chrome_history(start: str = None, end: str = None, days_ago: int = None) -> dict:
    """Callable entrypoint used by the agent loop's tool dispatcher.

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

    sites = _filter_and_group(rows)
    return {"range": f"{start_date} to {end_date}", "sites": sites, "total_meaningful_visits": len(sites)}


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
