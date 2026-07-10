"""Fire any due reminders as phone pushes. Non-interactive — run by launchd on a
short StartInterval (every minute), so a reminder lands within ~a minute of its
time.

Each due reminder is pushed via notify(); only reminders whose push succeeds are
cleared, so a transient ntfy outage just means the reminder is retried on the
next sweep rather than being lost.

Usage:
    python -m tasks.reminder_sweep
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.tools import reminders
from agent.tools.notify import notify
from tasks._common import notify_failure, setup_logger


def main() -> int:
    logger = setup_logger("reminder_sweep")

    try:
        due = reminders.get_due()
        if not due:
            return 0

        logger.info(f"{len(due)} reminder(s) due")
        fired = []
        for r in due:
            result = notify(message=r["message"], title="Reminder")
            if "error" in result:
                logger.warning(f"reminder {r['id']} push failed, will retry: {result['error']}")
                continue
            logger.info(f"fired reminder {r['id']}: {r['message']!r}")
            fired.append(r["id"])

        if fired:
            reminders.complete(fired)
        return 0
    except Exception as e:
        logger.exception(f"Reminder sweep failed: {e}")
        notify_failure("reminder_sweep", e, logger)
        return 1


if __name__ == "__main__":
    sys.exit(main())
