"""Push a short notification to the user's phone via a self-hosted ntfy server.

This is Wren's one outbound push channel — used by the scheduled tasks to
alert on a failed run, since a browser tab (chat) can't reach out and an email
failure notice gets buried. Delivery is best-effort: notify() funnels every
error into the uniform {"error": ...} shape rather than raising, so a push
outage can never mask the task failure it's trying to report.

Config (config/.env):
    NTFY_URL   full topic URL, e.g. http://mac-mini.tailnet.ts.net:2586/wren-alerts
    NTFY_TOKEN publish token for that topic (Bearer). Optional but expected,
               since the self-hosted server runs auth-default-access: deny-all.

Usage:
    python -m agent.tools.notify --message "6am brief failed" --title "Wren"
"""

import argparse
import os
import sys
from urllib.parse import urlsplit

import requests

from agent.tools import push_log
from agent.tools._http import http_error, load_env, print_result

# The title travels as an HTTP header and the message as the body; the body is
# sent UTF-8 encoded, and the title goes through _header_safe() below.
_TIMEOUT_S = 10
_MAX_MESSAGE_CHARS = 500

# Titles that came from outside. mail_watcher puts a Gmail subject here, and a
# sender chooses that text — so it routinely contains an emoji. A title travels
# as an HTTP header, http.client encodes header values as latin-1, and an emoji
# raises UnicodeEncodeError there. That aborted the whole POST: the alert was
# lost, not just its emoji. Drop what latin-1 cannot carry and send the rest.
def _header_safe(title: str) -> str:
    """`title` reduced to characters an HTTP header can carry.

    latin-1 rather than ASCII on purpose — it covers the accented Latin letters
    that show up in real names and subjects, so "Café" survives intact and only
    the genuinely unencodable characters go."""
    cleaned = title.encode("latin-1", "ignore").decode("latin-1")
    # An all-emoji or all-CJK subject reduces to nothing, and a blank Title
    # header is worse than none: ntfy then shows the topic name instead.
    return " ".join(cleaned.split())


# Priority is a word in the plaintext header path but a 1-5 int in the JSON
# publish path (used when action buttons are attached).
_PRIORITY_INT = {"max": 5, "urgent": 5, "high": 4, "default": 3, "low": 2, "min": 1}

# The push channel probe has to be an ACTIVE check, not a log scan. July 2026:
# ntfy was down for four days and not one line was logged about it, because
# nothing happened to need pushing — no task failed, no reminder came due. A
# dead channel is invisible until you try to use it, and by then the alert is
# the thing being lost. So ask it directly.
_HEALTH_TIMEOUT_S = 5


def _fallback_email(message: str, title: str | None, error: str) -> dict:
    """Best-effort email for a push that didn't send. Same "don't lose it" shape
    as activity_log.persist_or_email's failed-vault-write fallback.

    Imported locally so notify() stays importable (and cheap) without pulling in
    the Google client stack on the overwhelmingly common success path."""
    from agent.tools.email import send_email

    try:
        return send_email(
            subject=f"[push failed] {title or 'Wren alert'}",
            body=f"{message}\n\n--\nntfy did not deliver this push: {error}",
        )
    except Exception as e:  # never let the fallback mask the original failure
        return {"error": str(e)}


def notify(
    message: str,
    title: str | None = None,
    priority: str | None = None,
    actions: list | None = None,
    email_fallback: bool = False,
) -> dict:
    """POST a notification to the configured ntfy topic. Returns {"ok": True}
    on success or {"error": ...} — never raises.

    When `actions` (ntfy action buttons) are given, publishes as JSON to the
    server's base URL (the only form that carries buttons) rather than posting
    the plaintext body to the topic URL.

    `email_fallback` emails the message if the push fails, so an ntfy outage
    can't silently swallow an alert. It is opt-in per call, NOT the default, and
    that asymmetry is deliberate: reminder_sweep retries a failed push every 60s,
    so a blanket fallback would have sent thousands of emails per pending
    reminder during the four-day July 2026 outage. Turn it on for one-shot
    alerts that are lost if they don't land (notify_failure, the log inspector's
    rollup); leave it off for anything retried or anything whose value is in the
    action buttons an email can't carry (bg_worker's push-to-approve)."""
    load_env()
    url = os.getenv("NTFY_URL")
    if not url:
        # Deliberately no fallback: an unset NTFY_URL means push is switched
        # off on purpose (see README), not that delivery failed.
        return {"error": "NTFY_URL not set in config/.env"}

    token = os.getenv("NTFY_TOKEN")
    auth = {"Authorization": f"Bearer {token}"} if token else {}
    body = message[:_MAX_MESSAGE_CHARS]

    try:
        if actions:
            parts = urlsplit(url)
            payload = {"topic": parts.path.strip("/"), "message": body, "actions": actions}
            # JSON, so this path carries UTF-8 fine — no sanitizing needed.
            if title:
                payload["title"] = title
            if priority:
                payload["priority"] = _PRIORITY_INT.get(priority, 3)
            resp = requests.post(
                f"{parts.scheme}://{parts.netloc}", json=payload, headers=auth, timeout=_TIMEOUT_S)
        else:
            headers = dict(auth)
            safe_title = _header_safe(title) if title else ""
            if safe_title:
                headers["Title"] = safe_title
            if priority:
                headers["Priority"] = priority
            resp = requests.post(url, data=body.encode("utf-8"), headers=headers, timeout=_TIMEOUT_S)
        resp.raise_for_status()
    except Exception as e:
        result = http_error(e, phase="notify")
        if email_fallback:
            result["email_fallback"] = _fallback_email(body, title, result["error"])
        return result

    # Delivered — keep a copy so Wren can answer "what did you send me?" in chat
    # (agent/tools/push_log.py). Only the success path is logged: reminder_sweep
    # retries a failed push every 60s, so logging attempts would have written
    # tens of thousands of rows during the four-day July 2026 outage.
    #
    # `body`, not `message`: the log should say what actually reached the phone.
    #
    # Never allowed to raise. A push that landed must report {"ok": True} even if
    # the bookkeeping behind it failed, or a full disk would turn a delivered
    # alert into a reported failure — the exact inversion notify()'s
    # never-raise contract exists to prevent.
    try:
        push_log.record(body, title, priority)
    except Exception as e:
        print(f"warning: push delivered but not logged: {e}", file=sys.stderr)

    return {"ok": True}


def ntfy_health() -> dict:
    """Is the push channel up? -> {"state": "ok"|"down"|"off", "error": str|None}.

    Hits ntfy's /v1/health rather than publishing: a probe that pushed would
    either alert the phone on every check or need a throwaway topic.

    This proves the ntfy SERVER is reachable — not that NTFY_TOKEN is still
    valid for the topic. The server runs auth-default-access: deny-all, so a
    revoked token reports "ok" here and 403s on every real publish. Testing
    auth means publishing, which buzzes the phone; callers must not word this
    as "push works".

    "off" (NTFY_URL unset) is not a fault — push is switched off on purpose
    (see README) — so `error` is None for both "off" and "ok". Callers that
    only report faults can read `error` and ignore `state`.
    """
    load_env()
    url = os.getenv("NTFY_URL")
    if not url:
        return {"state": "off", "error": None}

    parts = urlsplit(url)
    try:
        resp = requests.get(f"{parts.scheme}://{parts.netloc}/v1/health",
                            timeout=_HEALTH_TIMEOUT_S)
        resp.raise_for_status()
        if not resp.json().get("healthy"):
            return {"state": "down", "error": "ntfy reachable but reports unhealthy"}
    except Exception as e:
        return {"state": "down", "error": f"ntfy unreachable: {e}"}
    return {"state": "ok", "error": None}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--message", required=True)
    parser.add_argument("--title", default=None)
    parser.add_argument("--priority", default=None)
    args = parser.parse_args()

    return print_result(notify(args.message, args.title, args.priority))


if __name__ == "__main__":
    sys.exit(main())
