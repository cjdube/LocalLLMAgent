"""Compose and write a daily "what I shipped" entry to the user's Obsidian vault
(one file per day). Non-interactive — run by launchd every morning, covering the
prior day.

Draws on the commits in the local checkouts under PROJECTS_DIR. The rest of the
record covers time spent and pages read; this is the only source that says what
was actually built. Falls back to emailing the draft if the vault write fails, so
an entry is never silently lost.

The commit subjects in these repos are written as sentences, so the model's job is
small on purpose: group related commits and say what the work was. The totals line
under the draft is arithmetic, and is computed in Python.

Usage:
    python -m scribejay.daily_commits
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.loop import complete_text, warm_model
from scribejay.model import backend as scribejay_backend, log_backend
from agent import prefs
from agent.activity_log import persist_or_email, prior_day
from scribejay.git_activity import collect_commits, compact_commits, render_commits
from scribejay.journal import commit_totals_line, has_substantive_content
from tasks._common import notify_failure, setup_logger

DRAFT_SYSTEM_PROMPT = f"""You are {prefs.user_name()}'s personal executive assistant. You write a \
short daily log of what he BUILT on the day just completed, from the commit data given. You are \
running unattended — infer everything from the data and write your best draft.

Use EXACTLY this template, filling in the bracketed parts (do not add extra sections, do not
include the literal brackets):

## Daily Commits: [Month Date, Year]

### What I Built
- **[Name of the change]:** [What it does and why it matters, in one or two sentences]

### Also
- **[Name of the change]:** [One line]

Source data you'll receive:
- commits: yesterday's commits, newest first. Each line is "[repo] subject (N files, +added/-removed)", \
and the paths changed are listed under it.

How to read it:
- The subject line is a complete sentence written by the author. Trust it — it is the best \
description of the change that exists. Do not contradict it and do not restate it verbatim.
- SEVERAL COMMITS ARE OFTEN ONE PIECE OF WORK. Commits an hour apart touching the same paths are \
one feature being finished, and belong in ONE bullet that says what was finished — not one bullet each.
- The paths are evidence about the SHAPE of the work, and are the main thing you can add that the \
subject line does not already say. Paths under a tests directory mean it was tested; paths under a \
docs directory or a README mean it was documented; a new file under a tools or a tasks directory \
means a new capability rather than a fix.
- The +added/-removed counts indicate size, not importance. A ten-line change that fixes a \
long-standing problem outranks a large mechanical rename.

Which section:
- "What I Built": the substantial work — a feature, a capability, a fix that mattered. 1-4 bullets.
- "Also": small or housekeeping changes worth recording but not explaining. At most 3 bullets, one \
line each.

Rules:
- Professional, analytical tone. No casual language.
- Bold the name of the change at the start of each bullet (use **name** markdown).
- Name the repository when there is more than one in the data.
- NEVER invent a detail the subjects and paths do not support. If you cannot tell what a commit \
did, say what it touched and stop.
- If a section has no qualifying items, use exactly one bullet under it: \
"**None:** [No qualifying items for this section]".

Output ONLY the filled-in template text, nothing else — no preamble, no explanation.
"""


def main() -> int:
    logger = setup_logger("daily_commits")
    logger.info("Starting daily commits run")

    try:
        start, end, day = prior_day()
        logger.info(f"Day: {day}")

        result = collect_commits(start, end, logger=logger)
        logger.info(f"collect_commits -> {result['total_commits']} commits across "
                    f"{len(result['repos'])} of {result['repos_scanned']} checkouts: {result['repos']}")

        # A day with no commits is an ordinary day, not a failure — don't warm the
        # model or write an empty file for it.
        if not result["commits"]:
            logger.info("No commits yesterday; nothing to write")
            logger.info("Daily commits run complete")
            return 0

        rows = compact_commits(result["commits"], logger=logger)
        commit_block = render_commits(rows, logger=logger)
        user_prompt = (
            f"day: {day:%B %-d, %Y}\n"
            f"commits:\n{commit_block}\n"
        )

        backend = scribejay_backend("daily_commits")
        log_backend(logger, "daily_commits", backend)
        warm_model(logger=logger, backend=backend)
        entry_text = complete_text(
            system_prompt=DRAFT_SYSTEM_PROMPT, user_prompt=user_prompt, logger=logger,
            backend=backend, think=False,
        )
        logger.info(f"Drafted entry:\n{entry_text}")

        # An all-"None" draft off a day that HAD commits is the model failing, not
        # an empty day — the empty day already returned above. Say so, because the
        # symptom otherwise is just a missing file nobody looks for.
        if not has_substantive_content(entry_text):
            logger.warning(f"Draft had no bullets despite {result['total_commits']} "
                           f"commits ({len(entry_text)} chars); nothing to write")
            logger.info("Daily commits run complete")
            return 0

        entry_text = f"{entry_text.rstrip()}\n\n{commit_totals_line(result['commits'])}\n"

        persist_or_email(
            entry_text, "Daily-Commits", day,
            subject=f"Daily Commits (needs manual paste) - {day:%Y-%m-%d}",
            task_name="daily_commits", logger=logger,
        )
        logger.info("Daily commits run complete")
        return 0
    except Exception as e:
        logger.exception(f"Daily commits run failed: {e}")
        notify_failure("daily_commits", e, logger)
        return 1


if __name__ == "__main__":
    sys.exit(main())
