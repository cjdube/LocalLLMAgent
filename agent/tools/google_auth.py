"""Shared Google OAuth helper for the calendar and email tools.

First run opens a browser for consent and caches a token at GOOGLE_TOKEN_PATH.
Subsequent runs (including unattended launchd runs) reuse/refresh that token
silently — no browser interaction required after the first authorization.

Setup (one-time, manual):
  1. https://console.cloud.google.com/ -> create/select a project
  2. Enable "Google Calendar API" and "Gmail API"
  3. OAuth consent screen -> External -> add your own email as a test user
  4. Credentials -> Create Credentials -> OAuth client ID -> Desktop app
  5. Download the JSON, save it as config/google_credentials.json
  6. Run: python -m agent.tools.google_auth   (opens browser once, caches token)
"""

import os
from pathlib import Path

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

_ROOT = Path(__file__).resolve().parent.parent.parent
_ENV_PATH = _ROOT / "config" / ".env"
load_dotenv(_ENV_PATH)

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/documents",
]


def get_credentials() -> Credentials:
    creds_path = _ROOT / os.getenv("GOOGLE_CREDENTIALS_PATH", "config/google_credentials.json")
    token_path = _ROOT / os.getenv("GOOGLE_TOKEN_PATH", "config/google_token.json")

    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not creds_path.exists():
                raise FileNotFoundError(
                    f"Missing {creds_path}. Download an OAuth client (Desktop app) JSON from "
                    "Google Cloud Console and save it there — see agent/tools/google_auth.py docstring."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json())

    return creds


if __name__ == "__main__":
    get_credentials()
    print("Google OAuth token cached successfully.")
