"""Compose and write a daily activity & tech-learnings review to the user's
Obsidian vault (one file per day). Non-interactive — run by launchd every
morning, covering the prior day.

Draws on Chrome browsing so the review reads as "what I learned yesterday".
Small prompt by design so the local Ollama model produces a full draft (the
reason the old weekly run had been pushed to a cloud model). Falls back to
emailing the draft if the vault write fails, so an entry is never silently lost.

Usage:
    python -m tasks.daily_chrome_learnings
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.loop import complete_text, resolve_backend, warm_model
from agent import prefs
from agent.tools.chrome_history import fetch_chrome_history
from tasks._common import notify_failure, setup_logger
from tasks._learnings_common import (
    MAX_PAGES_PER_SITE,
    compact_sites,
    has_substantive_content,
    persist_or_email,
    prior_day,
)

DRAFT_SYSTEM_PROMPT = f"""You are {prefs.user_name()}'s personal executive assistant. You write a \
short daily log entry covering the day just completed, from the data given. You are running \
unattended — infer everything from the data and write your best draft.

Use EXACTLY this template, filling in the bracketed parts (do not add extra sections, do not
include the literal brackets):

## Daily Log: [Month Date, Year]

### Tools & Tech Encountered
- **[Tool/Technology]:** [How it was used or encountered and the resulting capability]

### Product & Strategy
- **[Topic/Concept]:** [What the reading covered and why it matters for product work]

Source data you'll receive:
- chrome_sites: yesterday's browsing. Draft two sections from it. "Tools & Tech Encountered" from \
genuinely technical/developer/AI/product sites (tools, APIs, platforms, docs, frameworks); "Product \
& Strategy" from product-management reading — product strategy, discovery, prioritization, \
roadmapping, positioning, growth, metrics, and PM best-practices articles or newsletters (this \
section is about ideas and practices, not tools). Ignore anything that fits neither section. \
Each site's "pages" lists the specific page paths visited: use them to say what was actually being \
looked into (several pages under /docs/pricing and /docs/models is a comparison, not just a visit). \
Never invent detail the paths and titles don't support.

Ranking chrome_sites — the list is ordered by visit count, which is NOT a measure of importance:
- Prefer focused engagement (a specific article read, a cluster of docs paths on one topic) over \
habitual navigation (sites opened daily out of routine — repo dashboards, mail, your own dashboards).
- A low "visits" number is never a reason to skip a site. One deliberately-read article is stronger \
signal than eight visits to a homepage.
- Prefer the subject matter over the tool that hosted it, so a topic researched across several sites \
reads as one line of inquiry rather than several unrelated bullets.

Rules:
- Professional, analytical tone. No casual language.
- Bold the tool or topic name at the start of each bullet (use **name** markdown).
- 2-4 bullets per section. Explain significance, not just what happened.
- If a section has no qualifying items, use exactly one bullet under it: \
"**None:** [No qualifying items for this section]".
- NEVER include fitness (runs, yoga, gym, walks), social (book club, coffee, meals), travel, \
or personal/household tasks — even if they appear in the data.

Output ONLY the filled-in template text, nothing else — no preamble, no explanation.
"""


def main() -> int:
    logger = setup_logger("daily_chrome_learnings")
    logger.info("Starting daily chrome learnings run")

    try:
        start, end, day = prior_day()
        logger.info(f"Day: {day}")

        chrome_result = fetch_chrome_history(start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"),
                                             pages_per_domain=MAX_PAGES_PER_SITE,
                                             max_sites=None)  # summarizes the whole day; no context window to protect
        logger.info(f"fetch_chrome_history -> {chrome_result}")
        chrome_sites = compact_sites(chrome_result.get("sites", []))
        logger.info(f"compacted chrome_sites to {len(chrome_sites)} of "
                    f"{chrome_result.get('total_meaningful_visits', 0)} for the prompt")

        # Nothing happened yesterday worth a log — no (non-noise) browsing — so
        # don't warm the model or write a file.
        if not chrome_sites:
            logger.info("No browsing yesterday; nothing to write")
            logger.info("Daily chrome learnings run complete")
            return 0

        user_prompt = (
            f"day: {day:%B %-d, %Y}\n"
            f"chrome_sites: {chrome_sites}\n"
        )

        backend = resolve_backend("daily_chrome_learnings")
        warm_model(logger=logger, backend=backend)
        entry_text = complete_text(
            system_prompt=DRAFT_SYSTEM_PROMPT, user_prompt=user_prompt, logger=logger,
            backend=backend, think=False,
        )
        logger.info(f"Drafted entry:\n{entry_text}")

        # If the model found nothing relevant (the section came back "None"),
        # skip the write rather than save an empty log.
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
