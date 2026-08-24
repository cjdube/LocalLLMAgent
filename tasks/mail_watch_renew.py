"""Re-register Gmail's push notification for the watched label. Non-interactive —
run by launchd once a day.

**Gmail drops a watch after 7 days and tells nobody.** The mailbox simply stops
publishing, the watcher goes quiet, and a stopped watcher looks exactly like a
quiet inbox — the silent degrade CLAUDE.md calls worse than a crash. So there are
three layers, deliberately: this job renews daily (so one missed run costs
nothing), it pushes an alert when the stored expiry is inside ALERT_HOURS, and
tasks/log_inspector.py flags the job in its morning rollup if it stopped running
altogether.

Renewing is idempotent — calling users.watch() again just moves the expiry out.

Usage:
    python -m tasks.mail_watch_renew
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from agent.tools import gmail_read, mail_state
from tasks._common import notify_failure, setup_logger

_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / "config" / ".env")

# Below this many hours to expiry, push an alert even though the renewal
# succeeded. A fresh watch is ~168 hours out, so anything under 48 means at
# least two daily runs did not take effect and the next silence is imminent.
ALERT_HOURS = 48


def topic_name() -> str:
    """The fully-qualified Pub/Sub topic Gmail publishes to. Assembled here
    rather than stored whole, so the project id stays one value shared with the
    subscriber."""
    project = os.getenv("MAIL_PUBSUB_PROJECT", "")
    topic = os.getenv("MAIL_PUBSUB_TOPIC", "wren-mail")
    return f"projects/{project}/topics/{topic}"


def main() -> int:
    logger = setup_logger("mail_watch_renew")

    try:
        if not os.getenv("MAIL_PUBSUB_PROJECT"):
            raise RuntimeError(
                "MAIL_PUBSUB_PROJECT is not set in config/.env — see docs/mail-watch.md")

        label = gmail_read.label_id()
        if "error" in label:
            raise RuntimeError(f"label lookup failed: {label['error']}")
        logger.info(f"watching label {label['name']!r} (id {label['label_id']})")

        # The label is not passed to users.watch — the watch covers the whole
        # mailbox, and tasks/mail_watcher.py decides per thread (see
        # gmail_read._thread_is_watched). It is still resolved above because
        # nothing works without it, and failing here says so once a day rather
        # than leaving the watcher quietly matching nothing.
        result = gmail_read.register_watch(topic_name())
        if "error" in result:
            # A 403 here is nearly always the publisher grant, not the code.
            raise RuntimeError(
                f"users.watch failed: {result['error']} — if this is a 403 about "
                "the topic not being accessible, gmail-api-push@system."
                "gserviceaccount.com has lost the Pub/Sub Publisher role on it")

        mail_state.record_watch(result["expiration"], result["history_id"])
        hours = mail_state.watch_expires_in_hours()
        logger.info(
            f"watch renewed — expires in {hours:.1f}h, history id {result['history_id']}")

        if hours is not None and hours < ALERT_HOURS:
            # Renewal *succeeded* and the expiry is still close, which means the
            # daily runs are not landing. Alert on the success path, because the
            # failure path would never fire for this.
            notify_failure(
                "mail_watch_renew",
                f"Gmail watch expires in {hours:.0f}h even after renewing — mail "
                "notifications are about to stop silently",
                logger,
            )
        return 0
    except Exception as e:
        logger.exception(f"Gmail watch renewal failed: {e}")
        notify_failure("mail_watch_renew", e, logger)
        return 1


if __name__ == "__main__":
    sys.exit(main())
