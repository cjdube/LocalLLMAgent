"""Write a daily record of who was written to, into CORRESPONDENCE_DIR.

Non-interactive — run by launchd every morning, covering the prior day. Reads
Gmail's SENT metadata (headers only, never bodies) via
`agent.tools.gmail_read.fetch_sent_metadata`, drops mail Wren sent the user
himself, groups the rest by conversation, and writes the page in Python.

**No model call.** With no bodies there is nothing to summarize that the subject
line does not already say, and a richer sentence would be invented — see
scribejay/correspondence.py. `scribejay/strava_download.py` is the other task
shaped this way.

**Not the vault's raw/.** These pages name people and the companies they work
for; the ingest queue would turn them into asserted wiki pages. See
`_correspondence_dir`.

Usage:
    python -m scribejay.daily_correspondence                 # yesterday
    python -m scribejay.daily_correspondence --date 2026-08-21
    python -m scribejay.daily_correspondence --backfill 14   # each of the last 14 days
"""

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.activity_log import persist_or_email, prior_day
from agent.tools.calendar import _local_timezone
from agent.tools.gmail_read import fetch_sent_metadata, my_address
from scribejay.correspondence import (
    _correspondence_dir,
    filter_noise,
    group_threads,
    render_page,
)
from tasks._common import notify_failure, setup_logger


def _run_for_day(start, end, day, me, logger) -> None:
    """Build and persist one day's page. Raises on failure — main() owns the run
    boundary and the alert, so a backfill is one run in the dashboard's history
    rather than N half-runs."""
    result = fetch_sent_metadata(start, end)
    if "error" in result:
        # A source that fails reads as an empty day to its caller (AGENTS.md),
        # but never silently: this is the line that says the page is missing
        # because Gmail failed, not because he wrote to nobody.
        logger.warning(f"fetch_sent_metadata failed for {day}: {result['error']}")
        return
    logger.info(f"fetch_sent_metadata -> {result['count']} sent message(s) on {day}")

    rows = filter_noise(result["messages"], me, logger=logger)
    if not rows:
        logger.info(f"No correspondence on {day}; nothing to write")
        return

    threads = group_threads(rows, me)
    logger.info(f"{len(threads)} conversation(s) from {len(rows)} message(s)")

    persist_or_email(
        render_page(threads, day), "Correspondence", day,
        subject=f"Correspondence (needs manual paste) - {day:%Y-%m-%d}",
        task_name="daily_correspondence", logger=logger,
        directory=_correspondence_dir(),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default=None, help="write a single day YYYY-MM-DD")
    parser.add_argument("--backfill", type=int, default=0,
                        help="write each of the last N days; default 0 = just yesterday")
    args = parser.parse_args()

    logger = setup_logger("daily_correspondence")
    logger.info("Starting daily correspondence run")

    try:
        # Identity, resolved once: every filter and every people list is "everyone
        # who is not him". An empty answer would make the whole day self-addressed
        # and drop it, so it is a hard failure rather than a quiet empty page.
        me = my_address()
        if not me:
            raise RuntimeError("could not read the mailbox owner's address from Gmail")
        logger.info(f"mailbox: {me}")

        if args.date:
            tz = ZoneInfo(_local_timezone())
            start = datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=tz)
            end = start.replace(hour=23, minute=59, second=59)
            logger.info(f"Single day: {start.date()}")
            _run_for_day(start, end, start.date(), me, logger)
        elif args.backfill > 0:
            tz = ZoneInfo(_local_timezone())
            today = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
            # Oldest day first, so the log reads chronologically as files land.
            for k in range(args.backfill, 0, -1):
                start, end, day = prior_day(today - timedelta(days=k - 1))
                logger.info(f"Backfill day {day}")
                _run_for_day(start, end, day, me, logger)
        else:
            start, end, day = prior_day()
            logger.info(f"Day: {day}")
            _run_for_day(start, end, day, me, logger)

        logger.info("Daily correspondence run complete")
        return 0
    except Exception as e:
        logger.exception(f"Daily correspondence run failed: {e}")
        notify_failure("daily_correspondence", e, logger)
        return 1


if __name__ == "__main__":
    sys.exit(main())
