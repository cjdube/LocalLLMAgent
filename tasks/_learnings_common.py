"""Shared helpers for the daily learnings tasks (daily_chrome_learnings,
daily_youtube_learnings).

Both tasks follow the same shape — resolve the prior day, compact the fetched
data to bound the prompt for the small local model, draft with the model, then
persist to the Obsidian vault (emailing the draft if the vault write fails so an
entry is never silently lost). This module holds the pieces they share.
"""

from datetime import datetime, timedelta
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from agent import prefs
from agent.tools.calendar import _local_timezone
from agent.tools.email import send_email
from agent.tools.learnings_file import write_entry
from tasks._common import notify_failure


def prior_day(now: datetime | None = None) -> tuple[datetime, datetime, "datetime.date"]:
    """Return (start, end, day) for yesterday in local tz: start at 00:00:00 and
    end at 23:59:59 of the day before today, plus the date itself (for the output
    filename). `now` is injectable for tests. Anchored to "the calendar day before
    today", so a 5am launchd run covers all of yesterday."""
    tz = ZoneInfo(_local_timezone())
    today = (now or datetime.now(tz)).replace(hour=0, minute=0, second=0, microsecond=0)
    start = today - timedelta(days=1)
    end = start.replace(hour=23, minute=59, second=59)
    return start, end, start.date()


# Prompt-bounding caps. Daily volume is much smaller than the weekly run, so
# these rarely bind — but they keep a heavy browsing day (or a link-dump video
# description) from blowing past the local model's context window.
# Lowered from 40 when per-site page paths were added: each site now costs ~5x
# the prompt space, and at 40 the draft got long enough that the small model
# started dropping whole template sections. The tail was single-visit noise
# anyway — the sites worth reviewing are at the top of the visit ordering.
MAX_CHROME_SITES = 15
MAX_YOUTUBE_VIDEOS = 25
MAX_YOUTUBE_DESC_CHARS = 500

# Page paths kept per site. The paths are what let the review say more than the
# tab title did — "/gemini-api/docs/models" plus "/gemini-api/docs/pricing" is a
# comparison, where the title alone is just "Gemini API".
MAX_PAGES_PER_SITE = 4

# Domains Craig doesn't want reviewed (volunteer-admin portals, M365). Scoped to
# the learnings tasks on purpose: chrome_history.NOISE_DOMAINS would also blind
# the fetch_chrome_history tool in chat, and he still wants to be able to ask
# about these sites there.
_EXCLUDED_DOMAINS = [
    d.lower() for d in prefs.section("learnings").get("excluded_domains", [])
    if isinstance(d, str) and d
]


def _is_excluded(domain: str) -> bool:
    """True if `domain` is an excluded domain or a subdomain of one, so a single
    "sharepoint.com" entry covers every tenant. The port is stripped first:
    Chrome's netloc carries one for local servers ("127.0.0.1:8420")."""
    host = (domain or "").lower().split(":")[0]
    return any(host == d or host.endswith("." + d) for d in _EXCLUDED_DOMAINS)


def compact_sites(sites: list) -> list:
    """Drop excluded domains, trim to the top visited sites, and replace the full
    `url` (long, and mostly tracking query strings) with the site's top page
    paths. Excluding before the cap means MAX_CHROME_SITES budgets reviewable
    sites rather than being spent on filtered ones.

    `pages` is present only when the caller asked fetch_chrome_history for
    pages_per_domain > 1, so a site without it degrades to domain+title."""
    kept = [s for s in sites if not _is_excluded(s.get("domain", ""))]
    top = sorted(kept, key=lambda s: s.get("visits", 0), reverse=True)[:MAX_CHROME_SITES]
    out = []
    for s in top:
        entry = {"domain": s.get("domain"), "title": s.get("title"), "visits": s.get("visits")}
        paths = [p.get("path") for p in (s.get("pages") or [])[:MAX_PAGES_PER_SITE] if p.get("path")]
        if paths:
            entry["pages"] = paths
        out.append(entry)
    return out


def compact_videos(videos: list) -> list:
    """Keep only the fields the model needs from each liked video and truncate
    the description, bounding the prompt the same way compact_sites does."""
    return [
        {
            "title": v.get("title"),
            "channel": v.get("channel"),
            "description": (v.get("description") or "")[:MAX_YOUTUBE_DESC_CHARS],
            "url": v.get("url"),
        }
        for v in videos[:MAX_YOUTUBE_VIDEOS]
    ]


def safe_url(url: str) -> str:
    """Return url only if it's an http(s) link, else "". A video's URL is built
    by agent.tools.youtube from the API's videoId, but the fields are still
    externally sourced — scheme-validate before rendering into the Markdown
    (same guard as tasks/morning_brief._safe_url)."""
    try:
        return url if urlparse(url).scheme in ("http", "https") else ""
    except (ValueError, AttributeError):
        return ""


def videos_section(videos: list) -> str:
    """Deterministic Markdown section listing every video Liked, with a link to
    each. Built in Python (not asked of the model) so the titles and URLs are
    exact and every link is scheme-validated. Titles keep their raw text; only a
    bad-scheme URL is dropped (the title then renders unlinked)."""
    lines = ["### Videos Liked"]
    if not videos:
        lines.append("- **None:** [No videos Liked this day]")
        return "\n".join(lines)
    for v in videos:
        title = (v.get("title") or "Untitled").strip()
        channel = (v.get("channel") or "").strip()
        url = safe_url(v.get("url") or "")
        label = f"[{title}]({url})" if url else title
        lines.append(f"- {label}{f' — {channel}' if channel else ''}")
    return "\n".join(lines)


def has_substantive_content(text: str) -> bool:
    """True if the draft has at least one real bullet — i.e. a bullet that isn't
    the template's "**None:**" empty-section marker. Lets a task skip writing a
    log whose every section came back empty rather than save an all-"None" file."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("- ") and "**None:**" not in stripped:
            return True
    return False


def persist_or_email(content: str, prefix: str, day, subject: str,
                     task_name: str, logger) -> dict:
    """Write `content` to the vault as <prefix>-<day>.md; if the write fails
    (e.g. drive unmounted), email the draft instead so it's never silently lost.
    Both paths failing is a hard failure (alert + raise), matching the contract
    the retired weekly task established."""
    write_result = write_entry(content, prefix, day)
    logger.info(f"write_entry -> {write_result}")
    if "error" in write_result:
        logger.warning("File write failed — emailing the draft so it isn't lost")
        notify_failure(task_name, "vault write failed — draft emailed instead", logger)
        email_result = send_email(subject=subject, body=content)
        logger.info(f"send_email -> {email_result}")
        if "error" in email_result:
            # send_email returns error dicts rather than raising, so check it:
            # both persistence paths failing must surface as a failed run.
            raise RuntimeError(
                "vault write AND email fallback both failed: "
                f"{email_result['error']}"
            )
    return write_result
