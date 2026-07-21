"""List Craig's starred GitHub repos and flag which have been pushed to since
a given timestamp.

Usage:
    python -m agent.tools.github_starred
    python -m agent.tools.github_starred --days-ago 3
    python -m agent.tools.github_starred --since 2026-06-01T00:00:00Z

Key resolution order: --api-key arg > config/.env file > GITHUB_TOKEN env var
"""

import argparse
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests

from agent.tools._http import http_error, load_env, missing_key_error, print_result, resolve_key

load_env()

STARRED_URL = "https://api.github.com/user/starred"
API_ROOT = "https://api.github.com"
PER_PAGE = 100
MAX_PAGES = 10  # safety bound: 1000 starred repos is far more than any personal account needs
MAX_ENRICH = 15  # cap on repos we fetch changelogs for, to bound extra API calls per run

TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "fetch_starred_repos",
        "description": (
            "List Craig's starred GitHub repos. Pass 'days_ago' to only get repos "
            "pushed to in that many days (i.e. what's new), each with a "
            "'recent_changes' summary of its latest release notes or commits. Omit "
            "'days_ago' to list every starred repo, unfiltered."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "days_ago": {
                    "type": "integer",
                    "description": (
                        "Days back to look for repo updates, e.g. 3 — resolved in "
                        "Python, don't compute a date yourself. Omit to list all."
                    ),
                },
            },
        },
    },
}


def _list_starred(api_key: str) -> list:
    headers = {
        "Authorization": f"token {api_key}",
        "Accept": "application/vnd.github+json",
    }
    repos = []
    url = STARRED_URL
    params = {"per_page": PER_PAGE}
    for _ in range(MAX_PAGES):
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        resp.raise_for_status()
        repos.extend(resp.json())
        next_url = resp.links.get("next", {}).get("url")
        if not next_url:
            break
        url, params = next_url, None  # next_url already carries the query string
    return repos


def _parse_since(since: str) -> datetime:
    # Repo timestamps from GitHub are like "2026-06-01T00:00:00Z" — normalize
    # the trailing Z to +00:00 so fromisoformat() accepts it on Python <3.11.
    return datetime.fromisoformat(since.replace("Z", "+00:00"))


def _truncate(text: str, max_len: int = 200) -> str:
    text = " ".join(text.split())
    if len(text) > max_len:
        text = text[:max_len].rsplit(" ", 1)[0] + "…"
    return text


def _first_content_line(body: str) -> str:
    """Release notes conventionally open with a bare '## Changelog' heading
    before the actual bullet list — skip that and return the first real
    bullet/line so the summary isn't just the word 'Changelog'."""
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    bullet = next((ln.lstrip("-*").strip() for ln in lines if ln.startswith(("-", "*"))), None)
    if bullet:
        # Strip a leading commit-hash link, e.g. "`8b9cade` Fix the thing" -> "Fix the thing"
        return re.sub(r"^`?[0-9a-f]{7,40}`?\s+", "", bullet)
    non_heading = next((ln for ln in lines if not ln.lstrip("#").strip() == "" and not set(ln) <= {"#", " "}), None)
    return re.sub(r"^#+\s*", "", non_heading) if non_heading else ""


def _count_suffix(shown: int, total: int, has_more: bool, noun: str) -> str:
    """' (+N more releases/commits)' when there's more in the window than
    the one/three we're showing — including an honest '50+' when the fetch
    itself was capped, rather than silently implying that's the true total."""
    extra = total - shown
    if extra <= 0 and not has_more:
        return ""
    label = f"{extra}+" if has_more else str(extra)
    plural = noun if extra == 1 else f"{noun}s"
    return f" (+{label} more {plural})"


def _repo_changes(full_name: str, since_dt: datetime, headers: dict) -> str:
    """Best-effort one-to-two-line summary of what changed: prefer the latest
    release's notes if it falls in the window, else the subject lines of
    recent commits — with a '(+N more)' suffix when the window held more
    than what's shown, so a week of daily activity doesn't silently collapse
    into "just the newest thing" with no sign anything was cut. Never
    raises — a lookup failure just means no summary for that repo, not a
    failed overall fetch."""
    try:
        # per_page=20 (not 1) so a window with several releases can be
        # counted, not just silently reduced to the latest one.
        resp = requests.get(
            f"{API_ROOT}/repos/{full_name}/releases", headers=headers, params={"per_page": 20}, timeout=15
        )
        resp.raise_for_status()
        releases = resp.json()
        qualifying = [r for r in releases if r.get("published_at") and _parse_since(r["published_at"]) >= since_dt]
        if qualifying:
            release = qualifying[0]
            name = release.get("name") or release.get("tag_name") or "New release"
            body = (release.get("body") or "").strip()
            content_line = _first_content_line(body)
            text = _truncate(f"{name}: {content_line}" if content_line else name, 160)
            has_more = len(qualifying) == len(releases) and "next" in resp.links
            return text + _count_suffix(1, len(qualifying), has_more, "release")
    except (requests.exceptions.RequestException, ValueError):
        pass  # fall through to commit messages

    try:
        # per_page=50 so the "most recent 3" is chosen from a window-sized
        # pool, and so we can report a real count instead of always "5".
        resp = requests.get(
            f"{API_ROOT}/repos/{full_name}/commits",
            headers=headers,
            params={"since": since_dt.isoformat(), "per_page": 50},
            timeout=15,
        )
        resp.raise_for_status()
        commits = resp.json()
        subjects = [
            c.get("commit", {}).get("message", "").splitlines()[0] for c in commits if c.get("commit", {}).get("message")
        ]
        # Merge-commit subjects ("Merge pull request #123 from...") are noise
        # next to what a maintainer actually changed — prefer real subjects
        # when there are any, only falling back to merges if that's all there is.
        substantive = [s for s in subjects if not s.startswith(("Merge pull request", "Merge branch"))]
        chosen = substantive or subjects
        if chosen:
            shown = chosen[:3]
            text = _truncate("; ".join(shown), 160)
            has_more = len(commits) == 50 and "next" in resp.links
            return text + _count_suffix(len(shown), len(chosen), has_more, "commit")
    except requests.exceptions.RequestException:
        pass

    return ""


def fetch_readme(full_name: str, api_key: str = None) -> str:
    """Best-effort raw README text for a repo, or "" on any failure/404.

    Never raises: a missing or unreadable README just means no README-derived
    summary for that repo, not a failed run — same degrade-don't-crash posture
    as _repo_changes. Callers truncate the (potentially large) text themselves
    before feeding it to the model. `Accept: application/vnd.github.raw` returns
    the decoded file body directly, so no base64 decode is needed here."""
    api_key = resolve_key("GITHUB_TOKEN", api_key)
    if not api_key:
        return ""
    headers = {"Authorization": f"token {api_key}", "Accept": "application/vnd.github.raw"}
    try:
        resp = requests.get(f"{API_ROOT}/repos/{full_name}/readme", headers=headers, timeout=15)
        resp.raise_for_status()
        return resp.text
    except requests.exceptions.RequestException:
        return ""


def _parse(raw: list, since_dt: datetime = None) -> list:
    repos = []
    for r in raw:
        pushed_at = r.get("pushed_at")
        if since_dt is not None:
            if not pushed_at:
                continue
            pushed_dt = datetime.fromisoformat(pushed_at.replace("Z", "+00:00"))
            if pushed_dt < since_dt:
                continue
        repos.append(
            {
                "name": r.get("name", ""),
                "full_name": r.get("full_name", ""),
                "html_url": r.get("html_url", ""),
                "description": r.get("description") or "",
                "language": r.get("language"),
                "stargazers_count": r.get("stargazers_count", 0),
                "pushed_at": pushed_at,
            }
        )
    repos.sort(key=lambda r: r.get("pushed_at") or "", reverse=True)
    return repos


def fetch_starred_repos(since: str = None, days_ago: int = None, api_key: str = None) -> dict:
    """Callable entrypoint used by the agent loop's tool dispatcher.

    'days_ago' is the chat-facing knob — resolved to an absolute timestamp
    here rather than trusting the model to compute one itself, same pattern
    as agent/tools/calendar.py's get_events_by_date and strava.py's
    fetch_strava. 'since' (an exact ISO 8601 timestamp) stays available for
    direct Python callers like tasks/morning_brief.py, which compute it
    themselves and don't go through the model at all."""
    api_key = resolve_key("GITHUB_TOKEN", api_key)
    if not api_key:
        return missing_key_error("GITHUB_TOKEN")

    if days_ago is not None:
        since = (datetime.now(timezone.utc) - timedelta(days=int(days_ago))).isoformat()

    since_dt = None
    if since:
        try:
            since_dt = _parse_since(since)
        except ValueError as e:
            return {"error": f"invalid 'since' timestamp: {e}"}

    try:
        raw = _list_starred(api_key)
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else "?"
        if status == 403 and e.response is not None and e.response.headers.get("X-RateLimit-Remaining") == "0":
            reset = e.response.headers.get("X-RateLimit-Reset")
            reset_str = datetime.fromtimestamp(int(reset), tz=timezone.utc).isoformat() if reset else "unknown"
            return {"error": f"GitHub API rate limit exceeded, resets at {reset_str}"}
        return http_error(e)
    except Exception as e:
        return http_error(e)

    try:
        repos = _parse(raw, since_dt)
    except Exception as e:
        return {"error": f"parse error: {e}"}

    if since_dt is not None:
        headers = {"Authorization": f"token {api_key}", "Accept": "application/vnd.github+json"}
        to_enrich = repos[:MAX_ENRICH]
        # _repo_changes makes up to 2 sequential HTTP round-trips per repo and
        # never raises, so fanning the (bounded) enrich loop out over a small
        # pool cuts wall-clock on a busy day off the morning-brief critical path
        # without changing behaviour. map() preserves order and each summary
        # lands back on its own repo. Small pool: courteous to GitHub's rate
        # limiter and plenty for MAX_ENRICH repos.
        with ThreadPoolExecutor(max_workers=5) as pool:
            summaries = pool.map(lambda r: _repo_changes(r["full_name"], since_dt, headers), to_enrich)
        for repo, summary in zip(to_enrich, summaries):
            repo["recent_changes"] = summary

    return {"repos": repos, "total_starred": len(raw), "since": since}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", default=None)
    parser.add_argument("--days-ago", dest="days_ago", type=int, default=None)
    parser.add_argument("--api-key", dest="api_key", default=None)
    args = parser.parse_args()

    result = fetch_starred_repos(args.since, args.days_ago, args.api_key)
    return print_result(result)


if __name__ == "__main__":
    sys.exit(main())
