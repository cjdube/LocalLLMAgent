"""Send email via Gmail API (used for the morning brief).

Usage:
    python -m agent.tools.email --subject "Morning Brief" --body "Hello"
"""

import argparse
import base64
import json
import os
import sys
from email.mime.text import MIMEText
from pathlib import Path

from dotenv import load_dotenv
from googleapiclient.discovery import build

from agent.tools.google_auth import get_credentials

_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(_ROOT / "config" / ".env")

TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "send_email",
        "description": "Send an email to the user (e.g. the morning brief).",
        "parameters": {
            "type": "object",
            "properties": {
                "subject": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["subject", "body"],
        },
    },
}


_SERVICE = None


def _service():
    # Built once per process; the underlying credentials refresh themselves.
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = build("gmail", "v1", credentials=get_credentials())
    return _SERVICE


def send_email(subject: str, body: str, to: str = None, html: bool = False) -> dict:
    to = to or os.getenv("BRIEF_TO_EMAIL")
    if not to:
        return {"error": "BRIEF_TO_EMAIL not set in config/.env"}

    message = MIMEText(body, "html" if html else "plain")
    message["to"] = to
    message["subject"] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

    try:
        sent = _service().users().messages().send(userId="me", body={"raw": raw}).execute()
    except Exception as e:
        return {"error": str(e)}

    return {"message_id": sent.get("id")}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--subject", required=True)
    parser.add_argument("--body", required=True)
    parser.add_argument("--to", default=None)
    args = parser.parse_args()

    result = send_email(args.subject, args.body, args.to)
    print(json.dumps(result, indent=2))
    return 1 if "error" in result else 0


if __name__ == "__main__":
    sys.exit(main())
