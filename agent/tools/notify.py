"""Push a short notification to Craig's phone via a self-hosted ntfy server.

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

import requests

from agent.tools._http import http_error, load_env, print_result

# ntfy headers must be ASCII (title/message travel as HTTP headers/body); the
# body is sent UTF-8 encoded, but keep titles plain to avoid header issues.
_TIMEOUT_S = 10
_MAX_MESSAGE_CHARS = 500


def notify(message: str, title: str | None = None, priority: str | None = None) -> dict:
    """POST a notification to the configured ntfy topic. Returns {"ok": True}
    on success or {"error": ...} — never raises."""
    load_env()
    url = os.getenv("NTFY_URL")
    if not url:
        return {"error": "NTFY_URL not set in config/.env"}

    headers = {}
    token = os.getenv("NTFY_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if title:
        headers["Title"] = title
    if priority:
        headers["Priority"] = priority

    body = message[:_MAX_MESSAGE_CHARS].encode("utf-8")
    try:
        resp = requests.post(url, data=body, headers=headers, timeout=_TIMEOUT_S)
        resp.raise_for_status()
    except Exception as e:
        return http_error(e, phase="notify")

    return {"ok": True}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--message", required=True)
    parser.add_argument("--title", default=None)
    parser.add_argument("--priority", default=None)
    args = parser.parse_args()

    return print_result(notify(args.message, args.title, args.priority))


if __name__ == "__main__":
    sys.exit(main())
