"""Compose and write the weekly Strategic Weekly Review entry to the
Weekly Learning & Project Log Google Doc. Non-interactive — run by launchd
every Monday morning, covering the week that just ended (Mon-Sun).

Ported from ai-memory's weekly-learnings skill. The original is interactive
(confirms the week, asks Craig one question, waits for approval before
writing) — since this runs unattended with nobody to ask, this version
infers everything from calendar + Chrome history and writes directly,
falling back to emailing the draft if the doc write fails (never lose the
entry silently, matching the original skill's boundary).

Usage:
    python -m tasks.weekly_learnings
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.loop import complete_text
from agent.tools.calendar import _local_timezone, get_events_in_range
from agent.tools.chrome_history import fetch_chrome_history
from agent.tools.docs import get_previous_entry_text, insert_weekly_entry
from agent.tools.email import send_email
from tasks._common import setup_logger

DRAFT_SYSTEM_PROMPT = """You are Craig's personal executive assistant. You write a \
structured weekly retrospective entry for his Weekly Learning & Project Log, covering \
the week just completed. You are running unattended — there is no one to ask questions, \
so infer everything from the data given and write your best draft.

Use EXACTLY this template, filling in the bracketed parts (do not add extra sections,
do not include the literal brackets):

## Strategic Weekly Review: Week Ending [Day, Month Date, Year]

### Project Milestones & Strategic Vision
- **[Project Name]:** [What progressed and why it matters strategically]
- **[Project Name]:** [What progressed and why it matters strategically]

### Technical Evolution & Tooling
- **[Tool/Technology]:** [How it was used or encountered and the resulting capability or efficiency gain]
- **[Tool/Technology]:** [How it was used or encountered and the resulting capability or efficiency gain]

### Industry Insights & Core Learning
- **[Concept/Method]:** [What was learned and how it will shape future work]
- **[Concept/Method]:** [What was learned and how it will shape future work]

### Operational & Community Updates
- **[Entity]:** [Administrative or community-focused actions and their outcome]
- **[Entity]:** [Administrative or community-focused actions and their outcome]

Source data you'll receive:
- work_events: calendar events tagged as Work-related (colorId 1) — draft "Project Milestones" from these.
- meeting_events / appointment_events / aarp_events: draft "Operational & Community Updates" from these.
- chrome_sites: browsing history for the week — draft "Technical Evolution & Tooling" and \
  "Industry Insights & Core Learning" from any technical/developer/AI/product sites (tools, \
  APIs, platforms, documentation, frameworks). Ignore anything not genuinely technical.
- carry_forward: unresolved items from last week's entry — mention briefly at the top of \
  Project Milestones if still relevant, otherwise ignore.

Rules:
- Professional, analytical tone — senior technical leader voice. No casual language.
- Bold the project/tool/concept/entity name at the start of each bullet (use **name** markdown).
- 2-3 bullets per section minimum. Never leave a section with only one bullet.
- Explain significance, not just what happened — what it unlocks or proves, not just "did X".
- If a section has no real source data, use exactly one bullet: \
  "**None this week:** [No qualifying items found for this section]"
- NEVER include: fitness activities (runs, yoga, cycling, gym, walks), social activities \
  (book club, coffee, meals out), travel/trip planning, or personal/household tasks (yard \
  work, errands, meal prep) — even if they appear in the calendar data.
- Operational & Community Updates includes ONLY: business meetings with named external \
  stakeholders, administrative actions with real business consequence (LLC filings, EIN, \
  memberships, contracts), AARP volunteer commitments, and public-facing product/community \
  events (talks, demos, presentations). Skip anything else, even if tagged Meeting/Appointment.

Output ONLY the filled-in template text, nothing else — no preamble, no explanation.
"""


def _week_range() -> tuple[datetime, datetime]:
    """Return (monday, sunday) datetimes for the most recently completed full
    week, in local tz. Anchored to "most recent Sunday strictly before today"
    rather than assuming today is Monday, so this is correct even if run
    manually on an arbitrary day (not just via the Monday 5am launchd trigger)."""
    tz = ZoneInfo(_local_timezone())
    today = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    days_since_last_sunday = today.weekday() + 1  # Mon=0 -> 1, ..., Sun=6 -> 7
    last_sunday = today - timedelta(days=days_since_last_sunday)
    last_monday = last_sunday - timedelta(days=6)
    return last_monday, last_sunday.replace(hour=23, minute=59, second=59)


def _categorize(events: list) -> dict:
    work, meetings, appointments, aarp = [], [], [], []
    for e in events:
        color = e.get("colorId")
        summary = (e.get("summary") or "").lower()
        if "aarp" in summary:
            aarp.append(e)
        elif color == "1":
            work.append(e)
        elif color == "3":
            meetings.append(e)
        elif color == "6":
            appointments.append(e)
    return {"work": work, "meetings": meetings, "appointments": appointments, "aarp": aarp}


def main() -> int:
    logger = setup_logger("weekly_learnings")
    logger.info("Starting weekly learnings run")

    try:
        monday, sunday = _week_range()
        time_min = monday.isoformat()
        time_max = sunday.isoformat()
        logger.info(f"Week range: {monday.date()} to {sunday.date()}")

        events_result = get_events_in_range(time_min, time_max)
        logger.info(f"get_events_in_range -> {events_result}")
        events = events_result.get("events", [])
        buckets = _categorize(events)

        chrome_result = fetch_chrome_history(monday.strftime("%Y-%m-%d"), sunday.strftime("%Y-%m-%d"))
        logger.info(f"fetch_chrome_history -> {chrome_result}")
        chrome_sites = chrome_result.get("sites", [])

        carry_forward = get_previous_entry_text()
        logger.info(f"carry_forward -> {carry_forward[:200]!r}")

        user_prompt = (
            f"week_ending: {sunday.strftime('%A, %B %-d, %Y')}\n"
            f"work_events: {buckets['work']}\n"
            f"meeting_events: {buckets['meetings']}\n"
            f"appointment_events: {buckets['appointments']}\n"
            f"aarp_events: {buckets['aarp']}\n"
            f"chrome_sites: {chrome_sites}\n"
            f"carry_forward: {carry_forward or '(none)'}\n"
        )

        entry_text = complete_text(system_prompt=DRAFT_SYSTEM_PROMPT, user_prompt=user_prompt)
        logger.info(f"Drafted entry:\n{entry_text}")

        write_result = insert_weekly_entry(entry_text)
        logger.info(f"insert_weekly_entry -> {write_result}")

        if "error" in write_result:
            logger.warning("Doc write failed — emailing the draft so it isn't lost")
            send_email(
                subject=f"Weekly Log (needs manual paste) - week ending {sunday.strftime('%Y-%m-%d')}",
                body=entry_text,
            )

        logger.info("Weekly learnings run complete")
        return 0
    except Exception as e:
        logger.exception(f"Weekly learnings run failed: {e}")
        try:
            send_email(
                subject="Weekly Log run failed",
                body=f"tasks.weekly_learnings raised an exception:\n\n{e}",
            )
        except Exception:
            pass
        return 1


if __name__ == "__main__":
    sys.exit(main())
