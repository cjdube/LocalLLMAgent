"""Compose and write a daily activity & tech-learnings review to Craig's
Obsidian vault (one file per day). Non-interactive — run by launchd every
morning, covering the prior day.

Folds the day's work-calendar context together with Chrome browsing so the
review reads as "what I did and learned yesterday". Small prompt by design so
the local Ollama model produces a full draft (the reason the old weekly run had
been pushed to a cloud model). Falls back to emailing the draft if the vault
write fails, so an entry is never silently lost.

Usage:
    python -m tasks.daily_chrome_learnings
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.loop import complete_text, resolve_backend, warm_model
from agent import prefs
from agent.tools.calendar import get_events_in_range
from agent.tools.chrome_history import fetch_chrome_history
from tasks._common import notify_failure, setup_logger
from tasks._learnings_common import (
    MAX_PAGES_PER_SITE,
    compact_sites,
    has_substantive_content,
    is_excluded_text,
    persist_or_email,
    prior_day,
)

# Looked up by role, not category name, so renaming a category in
# config/preferences.json can't silently empty its bucket. Defaults are the
# shipped colorIds.
_WORK_COLOR = prefs.category_color_by_role("work", "1")
_MEETINGS_COLOR = prefs.category_color_by_role("meetings", "3")
_APPOINTMENTS_COLOR = prefs.category_color_by_role("appointments", "6")

DRAFT_SYSTEM_PROMPT = f"""You are {prefs.user_name()}'s personal executive assistant. You write a \
short daily log entry covering the day just completed, from the data given. You are running \
unattended — infer everything from the data and write your best draft.

Use EXACTLY this template, filling in the bracketed parts (do not add extra sections, do not
include the literal brackets):

## Daily Log: [Month Date, Year]

### What I Worked On
- **[Project/Task]:** [What progressed and why it matters]

### Tools & Tech Encountered
- **[Tool/Technology]:** [How it was used or encountered and the resulting capability]

### Operational & Community
- **[Entity]:** [Administrative or community action and its outcome]

Source data you'll receive:
- work_events: calendar events tagged Work-related (colorId {_WORK_COLOR}) — draft "What I Worked On".
- meeting_events / appointment_events: draft "Operational & Community".
- chrome_sites: yesterday's browsing — draft "Tools & Tech Encountered" from any genuinely \
technical/developer/AI/product sites (tools, APIs, platforms, docs, frameworks). Ignore the rest. \
Each site's "pages" lists the specific page paths visited: use them to say what was actually being \
looked into (several pages under /docs/pricing and /docs/models is a comparison, not just a visit). \
Never invent detail the paths and titles don't support.

Rules:
- Professional, analytical tone. No casual language.
- Bold the project/tool/entity name at the start of each bullet (use **name** markdown).
- 1-3 bullets per section. Explain significance, not just what happened.
- If a section has no real source data, use exactly one bullet: \
"**None:** [No qualifying items for this section]".
- NEVER include fitness (runs, yoga, gym, walks), social (book club, coffee, meals), travel, \
or personal/household tasks — even if they appear in the data.

Output ONLY the filled-in template text, nothing else — no preamble, no explanation.
"""


def _categorize(events: list) -> dict:
    """Bucket events by category colour, dropping any whose summary matches an
    excluded keyword. The drop is explicit rather than a fall-through: an
    excluded event that happens to be coloured Meetings would otherwise land in
    "Operational & Community", which is exactly where it must not appear."""
    work, meetings, appointments = [], [], []
    for e in events:
        if is_excluded_text(e.get("summary", "")):
            continue
        color = e.get("colorId")
        if color == _WORK_COLOR:
            work.append(e)
        elif color == _MEETINGS_COLOR:
            meetings.append(e)
        elif color == _APPOINTMENTS_COLOR:
            appointments.append(e)
    return {"work": work, "meetings": meetings, "appointments": appointments}


def main() -> int:
    logger = setup_logger("daily_chrome_learnings")
    logger.info("Starting daily chrome learnings run")

    try:
        start, end, day = prior_day()
        logger.info(f"Day: {day}")

        events_result = get_events_in_range(start.isoformat(), end.isoformat())
        logger.info(f"get_events_in_range -> {events_result}")
        buckets = _categorize(events_result.get("events", []))

        chrome_result = fetch_chrome_history(start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"),
                                             pages_per_domain=MAX_PAGES_PER_SITE)
        logger.info(f"fetch_chrome_history -> {chrome_result}")
        chrome_sites = compact_sites(chrome_result.get("sites", []))
        logger.info(f"compacted chrome_sites to {len(chrome_sites)} of "
                    f"{chrome_result.get('total_meaningful_visits', 0)} for the prompt")

        # Nothing happened yesterday worth a log — no work/meeting/community events
        # and no (non-noise) browsing — so don't warm the model or write a file.
        if not any(buckets.values()) and not chrome_sites:
            logger.info("No calendar events or browsing yesterday; nothing to write")
            logger.info("Daily chrome learnings run complete")
            return 0

        user_prompt = (
            f"day: {day:%B %-d, %Y}\n"
            f"work_events: {buckets['work']}\n"
            f"meeting_events: {buckets['meetings']}\n"
            f"appointment_events: {buckets['appointments']}\n"
            f"chrome_sites: {chrome_sites}\n"
        )

        backend = resolve_backend("daily_chrome_learnings")
        warm_model(logger=logger, backend=backend)
        entry_text = complete_text(
            system_prompt=DRAFT_SYSTEM_PROMPT, user_prompt=user_prompt, logger=logger,
            backend=backend,
        )
        logger.info(f"Drafted entry:\n{entry_text}")

        # If the model found nothing relevant (every section came back "None"),
        # skip the write rather than save an all-empty log.
        if not has_substantive_content(entry_text):
            logger.info("Draft had no qualifying items; nothing to write")
            logger.info("Daily chrome learnings run complete")
            return 0

        persist_or_email(
            entry_text, "Daily-Chrome", day,
            subject=f"Daily Log (needs manual paste) - {day:%Y-%m-%d}",
            task_name="daily_chrome_learnings", logger=logger,
        )
        logger.info("Daily chrome learnings run complete")
        return 0
    except Exception as e:
        logger.exception(f"Daily chrome learnings run failed: {e}")
        notify_failure("daily_chrome_learnings", e, logger)
        return 1


if __name__ == "__main__":
    sys.exit(main())
