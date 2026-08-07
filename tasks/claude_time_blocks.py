"""Log yesterday's Claude Code working time as Google Calendar blocks.
Non-interactive — run by launchd every morning, covering the day that just ended.

The point is a calendar that records how the day actually went without anyone
remembering to fill it in. Claude Code already timestamps every event it writes to
~/.claude/projects/<slug>/<uuid>.jsonl, so the hours are on disk; this task only has
to decide where one stretch of work ends and the next begins.

It pools EVERY session's events for the day and splits that single timeline on idle
gaps, rather than making one event per session. Sessions overlap constantly — one
measured day had eight session-days whose spans summed to ~19 hours against a real
~5 — so per-session events would triple-book the calendar. Pooling makes the
result non-overlapping by construction; a block that spans several repos names them
all.

Python owns the timeline, the rounding, the titles' structure and the descriptions;
the model only writes the phrase that says what the stretch was about.

Companion to tasks/ai_chat_learnings.py, not part of it: the learnings review is
worth having whether or not you track time.

Usage:
    python -m tasks.claude_time_blocks                    # yesterday
    python -m tasks.claude_time_blocks --dry-run          # show the blocks, write nothing
    python -m tasks.claude_time_blocks --date 2026-08-05  # one specific day
    python -m tasks.claude_time_blocks --backfill 7       # each of the last 7 days
"""

import argparse
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import prefs
from agent.loop import complete_text, resolve_backend, warm_model
from agent.tools.calendar import (
    SESSION_BLOCK_SOURCE_PREFIX,
    _local_timezone,
    log_calendar_event,
)
from agent.tools.notify import notify
from tasks._chat_transcripts import _compact, fetch_session_activity
from tasks._common import notify_failure, setup_logger
from tasks._learnings_common import prior_day

# Idle gap that ends a block. Tuned against six real days: 10 minutes fragments a
# working morning into a dozen entries, 30 swallows a coffee break and a commute
# alike, 20 reproduces the days as they were actually lived (2-6 blocks, 1-5 hours).
DEFAULT_GAP_MINUTES = 20
# Below this, a block is a glance at something, not a working session worth a
# calendar entry — measured against the raw span, before the rounding below.
DEFAULT_MIN_MINUTES = 10
# Bounds the blurb prompt, per the small-local-model rule. Smaller than the
# learnings task's 12000: this call produces one line, not a whole summary.
DEFAULT_MAX_CHARS = 6000

# Blocks start and end on the event that happened to be logged, which is a minute
# or so inside the real stretch. Round out to 5-minute edges so the calendar reads
# 13:40-15:35 rather than 13:41-15:31.
_ROUND_MINUTES = 5

# Session blocks are Craig's own build time; the fallback keeps a cloner whose
# preferences.json renames the category from breaking (see docs/preferences.md).
_COLOR_ID = prefs.category_color_by_role("work", "1")

BLURB_SYSTEM_PROMPT = f"""You are {prefs.user_name()}'s assistant. You are given the transcript of \
ONE stretch of work {prefs.user_name()} did with an AI coding agent, and the name(s) of the \
project(s) it touched. Write the title of the calendar entry for that stretch: what the work was \
about. You are running unattended — infer everything from the transcript given.

Rules:
- ONE line, at most 60 characters. No trailing period.
- Say what was worked on, e.g. "fixed the login redirect loop" or "added weekly digest email".
- Do not name the project (it is already in the title) and do not mention the AI, the agent, or \
the session itself.
- Base it ONLY on the transcript. Do not invent work that isn't there.
- No preamble, no quotes, no markdown.

Output ONLY that one line, nothing else.
"""

_FALLBACK_BLURB = "working session"


def _round_down(ts: datetime) -> datetime:
    return ts.replace(minute=ts.minute - ts.minute % _ROUND_MINUTES, second=0, microsecond=0)


def _round_up(ts: datetime) -> datetime:
    floor = _round_down(ts)
    return floor if floor == ts else floor + timedelta(minutes=_ROUND_MINUTES)


def segment(events: list[dict], gap_minutes: int = DEFAULT_GAP_MINUTES,
            min_minutes: int = DEFAULT_MIN_MINUTES) -> list[dict]:
    """Split one day's pooled events (from fetch_session_activity) into
    non-overlapping blocks of working time, as [{"start", "end", "events"}].

    A gap longer than `gap_minutes` between consecutive events ends a block —
    an idle session logs nothing at all, so silence is the only signal that the
    user stepped away. Blocks whose real span is under `min_minutes` are dropped
    before rounding, so the floor means what it says."""
    if not events:
        return []

    groups, current = [], []
    for event in sorted(events, key=lambda e: e["ts"]):
        if current and (event["ts"] - current[-1]["ts"]).total_seconds() > gap_minutes * 60:
            groups.append(current)
            current = []
        current.append(event)
    groups.append(current)

    blocks = []
    for group in groups:
        if (group[-1]["ts"] - group[0]["ts"]).total_seconds() < min_minutes * 60:
            continue
        blocks.append({
            "start": _round_down(group[0]["ts"]),
            "end": _round_up(group[-1]["ts"]),
            "events": group,
        })
    return blocks


def block_projects(block: dict) -> list[str]:
    """The projects worked on during a block, in the order they were first
    touched — de-duplicated, since a project usually appears in many events."""
    seen = []
    for event in block["events"]:
        if event["project"] not in seen:
            seen.append(event["project"])
    return seen


def block_summary(block: dict, blurb: str) -> str:
    return f"AI · {', '.join(block_projects(block))} — {blurb}"


def block_description(block: dict) -> str:
    """One line per session in the block — its own span, project, and Claude
    Code's own slug for the conversation — so the entry says which sessions made
    it up even when several ran at once. Built here rather than asked of the
    model, so the times and names are exact."""
    spans = {}
    for event in block["events"]:
        span = spans.get(event["session"])
        if span is None:
            spans[event["session"]] = {"first": event["ts"], "last": event["ts"],
                                       "project": event["project"], "slug": event["slug"]}
        else:
            span["last"] = event["ts"]

    lines = []
    for span in sorted(spans.values(), key=lambda s: s["first"]):
        label = f"{span['project']} · {span['slug']}" if span["slug"] else span["project"]
        lines.append(f"{label} — {span['first']:%-I:%M} to {span['last']:%-I:%M %p}")
    lines.append("")
    lines.append("Logged by Wren from Claude Code's local session logs.")
    return "\n".join(lines)


def _blurb(block: dict, backend, max_chars: int, logger) -> str:
    """The model's one-line description of a block, or a fixed fallback.

    Falls back loudly: an empty response is what a model that spent its whole
    budget thinking returns, and a block silently titled "working session" would
    look like an ordinary quiet day rather than a broken prompt."""
    turns = [e["text"] for e in block["events"] if e["text"]]
    if not turns:
        logger.warning(f"Block at {block['start']:%H:%M} has no user/assistant text "
                       f"({len(block['events'])} events) — using the fallback title")
        return _FALLBACK_BLURB

    user_prompt = (f"projects: {', '.join(block_projects(block))}\n\n"
                   f"transcript:\n{_compact(turns, max_chars)}\n")
    raw = complete_text(
        system_prompt=BLURB_SYSTEM_PROMPT, user_prompt=user_prompt,
        logger=logger, backend=backend, think=False,
    )
    blurb = next((line.strip().strip('"').strip("*- ") for line in raw.splitlines()
                  if line.strip()), "")
    if not blurb:
        logger.warning(f"No usable blurb for the block at {block['start']:%H:%M} "
                       f"(raw response was {len(raw)} chars) — using the fallback title")
        return _FALLBACK_BLURB
    return blurb[:60].rstrip(" .")


def _log_block(block: dict, day, blurb: str, logger) -> dict:
    """Create (or re-find) the calendar event for one block. source_id is derived
    from the block's start, so a re-run or a backfill over the same day finds the
    event it already made instead of duplicating it."""
    source_id = f"{SESSION_BLOCK_SOURCE_PREFIX}{day:%Y-%m-%d}:{block['start']:%H%M}"
    result = log_calendar_event(
        summary=block_summary(block, blurb),
        start=block["start"].replace(tzinfo=None).isoformat(),
        end=block["end"].replace(tzinfo=None).isoformat(),
        description=block_description(block),
        color_id=_COLOR_ID,
        source_id=source_id,
    )
    logger.info(f"log_calendar_event({source_id}) -> {result}")
    return result


def _hours(blocks: list[dict]) -> float:
    return sum((b["end"] - b["start"]).total_seconds() for b in blocks) / 3600


def _run_for_day(start, end, day, gap, min_minutes, max_chars, backend,
                 warm, dry_run, logger) -> list[dict]:
    """Build and log one day's blocks. Returns the blocks that were created
    (empty on a quiet day, or on a dry run)."""
    events = fetch_session_activity(start, end)
    blocks = segment(events, gap, min_minutes)
    logger.info(f"{len(events)} Claude event(s) on {day} -> {len(blocks)} block(s), "
                f"{_hours(blocks):.1f}h")
    if not blocks:
        return []

    warm()
    logged = []
    for block in blocks:
        blurb = _blurb(block, backend, max_chars, logger)
        summary = block_summary(block, blurb)
        if dry_run:
            logger.info(f"[dry run] {block['start']:%H:%M}-{block['end']:%H:%M}  {summary}")
            continue
        result = _log_block(block, day, blurb, logger)
        if "error" in result:
            # One failed insert shouldn't cost the rest of the day.
            logger.warning(f"Could not log the block at {block['start']:%H:%M}: {result['error']}")
            continue
        logged.append(block)
    return logged


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default=None, help="log a single day, YYYY-MM-DD")
    parser.add_argument("--backfill", type=int, default=0,
                        help="log each of the last N days; default 0 = just yesterday")
    parser.add_argument("--dry-run", action="store_true",
                        help="show the blocks (titles included) without touching the calendar")
    args = parser.parse_args()

    logger = setup_logger("claude_time_blocks")
    logger.info("Starting claude time blocks run")

    try:
        gap = int(os.getenv("WREN_SESSION_BLOCK_GAP_MINUTES", DEFAULT_GAP_MINUTES))
        min_minutes = int(os.getenv("WREN_SESSION_BLOCK_MIN_MINUTES", DEFAULT_MIN_MINUTES))
        max_chars = int(os.getenv("WREN_SESSION_BLOCK_MAX_CHARS", DEFAULT_MAX_CHARS))
        backend = resolve_backend("claude_time_blocks")

        # Loaded lazily and once: a quiet day shouldn't pay for a ~17GB model load,
        # and a backfill shouldn't pay for it per day.
        warmed = []

        def warm():
            if not warmed:
                warm_model(logger=logger, backend=backend)
                warmed.append(True)

        def run(start, end, day, dry_run=args.dry_run):
            return _run_for_day(start, end, day, gap, min_minutes, max_chars,
                                backend, warm, dry_run, logger)

        if args.date:
            tz = ZoneInfo(_local_timezone())
            start = datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=tz)
            end = start.replace(hour=23, minute=59, second=59)
            logger.info(f"Single day: {start.date()}")
            run(start, end, start.date())
        elif args.backfill > 0:
            tz = ZoneInfo(_local_timezone())
            today = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
            for k in range(args.backfill, 0, -1):
                start, end, day = prior_day(today - timedelta(days=k - 1))
                logger.info(f"Backfill day {day}")
                run(start, end, day)
        else:
            start, end, day = prior_day()
            logger.info(f"Day: {day}")
            logged = run(start, end, day)
            # One push for the day, not one per block: this is a record of what
            # happened, not something to act on.
            if logged and not args.dry_run:
                notify(
                    message=f"{len(logged)} block(s), {_hours(logged):.1f}h logged for {day:%b %-d}",
                    title="Wren: AI time logged",
                )

        logger.info("Claude time blocks run complete")
        return 0
    except Exception as e:
        logger.exception(f"Claude time blocks run failed: {e}")
        notify_failure("claude_time_blocks", e, logger)
        return 1


if __name__ == "__main__":
    sys.exit(main())
