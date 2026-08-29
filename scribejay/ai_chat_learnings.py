"""Compose and write a daily "AI Chat Learnings" review to the user's Obsidian vault
(one file per day). Non-interactive — run by launchd every morning, covering the
prior day's AI-agent chats.

For each Claude Code or top-level Codex Desktop session that was new or revisited
yesterday (and each new file in the Gemini drop folder), the local model writes
a brief Accomplished / Learned summary — not the back-and-forth, just what got
done and what was learned. Python owns the day math, section headers, and file
assembly; the model only writes the bullets. A day with no chats writes nothing
(keeps the vault clean).

The sources are what lands on disk: Claude Code and Codex Desktop's local session
logs, and a folder the user drops Gemini exports into (see scribejay/transcripts.py).

Usage:
    python -m scribejay.ai_chat_learnings                 # yesterday (all sources)
    python -m scribejay.ai_chat_learnings --backfill 14   # each of the last 14 days
"""

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.loop import complete_text, warm_model
from scribejay import config
from scribejay.model import backend as scribejay_backend, log_backend
from agent.store import atomic_write_json, load_json, locked
from agent.tools.calendar import _local_timezone
from scribejay.transcripts import (
    DEFAULT_MAX_CHARS,
    fetch_claude_sessions,
    fetch_codex_sessions,
    fetch_gemini_chats,
)
from tasks._common import notify_failure, setup_logger
from agent.activity_log import persist_or_email, prior_day

# Gemini dedup lives here so a re-run never re-summarizes the same drop file.
STATE_PATH = Path(__file__).resolve().parent.parent / "config" / "ai_chat_learnings_state.json"

SESSION_SYSTEM_PROMPT = f"""You are {config.user_name()}'s assistant. You are given ONE past chat \
session {config.user_name()} had with an AI agent. Summarize it into a brief, skimmable log entry — \
focus ONLY on what was accomplished and what was learned, NOT the back-and-forth or the reasoning \
behind each decision. You are running unattended — infer everything from the transcript given.

Use EXACTLY this template (no extra sections, do not include the literal brackets):

**Accomplished**
- [a concrete outcome, deliverable, or decision from the session]
**Learned**
- [a durable insight, gotcha, or fact worth remembering later]

Rules:
- 1-4 Accomplished bullets, 0-3 Learned bullets. Be terse — one line each.
- If nothing was genuinely accomplished, write a single "- None" under Accomplished.
- If there was no real learning, write a single "- None" under Learned.
- Base everything ONLY on the transcript. Do not invent work that isn't there.
- Never repeat a word or phrase; no preamble.

Output ONLY the filled-in template, nothing else.
"""


def _has_real_content(text: str) -> bool:
    """True if the summary has at least one bullet that isn't the "- None"
    empty-section marker — lets us skip a session the model found nothing in."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("- ") and stripped[2:].strip().lower() != "none":
            return True
    return False


def _session_header(source: str, session: dict) -> str:
    parts = [source, session["project"]]
    if session["slug"]:
        parts.append(session["slug"])
    parts.append(f"{session['started_at']:%-I:%M %p}")
    return " · ".join(parts)


def _summarize(text: str, source: str, logger, backend) -> str:
    user_prompt = f"source: {source}\n\ntranscript:\n{text}\n"
    summary = complete_text(
        system_prompt=SESSION_SYSTEM_PROMPT, user_prompt=user_prompt,
        logger=logger, backend=backend, think=False,
    )
    logger.info(f"Summarized a {source} chat:\n{summary}")
    return summary


def _run_for_day(start, end, day, include_gemini, backend, max_chars, logger) -> None:
    """Build and persist one day's file. include_gemini is False for backfill
    runs (the drop folder has no reliable per-day dates, so it's only folded into
    the normal "yesterday" run)."""
    claude = fetch_claude_sessions(start, end, max_chars)
    codex = fetch_codex_sessions(start, end, max_chars, logger=logger)
    logger.info(f"{len(claude)} Claude session(s) active on {day}")
    logger.info(f"{len(codex)} Codex session(s) active on {day}")
    sessions = sorted(
        [("Claude", session) for session in claude]
        + [("Codex", session) for session in codex],
        key=lambda item: item[1]["started_at"],
    )

    gemini, processed = [], {}
    if include_gemini:
        processed = load_json(STATE_PATH, {"gemini_processed": {}}).get("gemini_processed", {})
        gemini = fetch_gemini_chats(processed, max_chars)
        logger.info(f"{len(gemini)} new Gemini chat(s) in the drop folder")

    if not sessions and not gemini:
        logger.info(f"No chats for {day}; nothing to write")
        return

    sections = []
    for source, session in sessions:
        summary = _summarize(session["text"], source, logger, backend)
        if _has_real_content(summary):
            sections.append(f"### {_session_header(source, session)}\n{summary}")

    newly_processed = {}
    for chat in gemini:
        summary = _summarize(chat["text"], "Gemini", logger, backend)
        # Mark processed whether or not it was useful, so we never re-summarize it.
        newly_processed[chat["name"]] = chat["mtime"]
        if _has_real_content(summary):
            sections.append(f"### Gemini · {Path(chat['name']).stem}\n{summary}")

    if newly_processed:
        with locked(STATE_PATH):
            state = load_json(STATE_PATH, {"gemini_processed": {}})
            state.setdefault("gemini_processed", {}).update(newly_processed)
            atomic_write_json(STATE_PATH, state)

    if not sections:
        logger.info(f"No substantive chat summaries for {day}; nothing to write")
        return

    body = f"## AI Chat Learnings: {day:%B %-d, %Y}\n\n" + "\n\n".join(sections) + "\n"
    persist_or_email(
        body, "AI-Chat-Learnings", day,
        subject=f"AI Chat Learnings (needs manual paste) - {day:%Y-%m-%d}",
        task_name="ai_chat_learnings", logger=logger,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default=None,
                        help="summarize a single day YYYY-MM-DD (Claude + Codex) — "
                             "one process per day is the gentle way to backfill history")
    parser.add_argument("--backfill", type=int, default=0,
                        help="summarize each of the last N days as a separate run "
                             "(Claude + Codex); default 0 = just yesterday")
    args = parser.parse_args()

    logger = setup_logger("ai_chat_learnings")
    logger.info("Starting ai chat learnings run")

    try:
        max_chars = int(config.getenv("AI_CHAT_LEARNINGS_MAX_CHARS", DEFAULT_MAX_CHARS))
        backend = scribejay_backend("ai_chat_learnings")
        log_backend(logger, "ai_chat_learnings", backend)
        warm_model(logger=logger, backend=backend)

        if args.date:
            tz = ZoneInfo(_local_timezone())
            start = datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=tz)
            end = start.replace(hour=23, minute=59, second=59)
            day = start.date()
            logger.info(f"Single day: {day}")
            _run_for_day(start, end, day, include_gemini=False,
                         backend=backend, max_chars=max_chars, logger=logger)
        elif args.backfill > 0:
            tz = ZoneInfo(_local_timezone())
            today = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
            # Oldest day first, so the log reads chronologically as files land.
            for k in range(args.backfill, 0, -1):
                start, end, day = prior_day(today - timedelta(days=k - 1))
                logger.info(f"Backfill day {day}")
                _run_for_day(start, end, day, include_gemini=False,
                             backend=backend, max_chars=max_chars, logger=logger)
        else:
            start, end, day = prior_day()
            logger.info(f"Day: {day}")
            _run_for_day(start, end, day, include_gemini=True,
                         backend=backend, max_chars=max_chars, logger=logger)

        logger.info("AI chat learnings run complete")
        return 0
    except Exception as e:
        logger.exception(f"AI chat learnings run failed: {e}")
        notify_failure("ai_chat_learnings", e, logger)
        return 1


if __name__ == "__main__":
    sys.exit(main())
