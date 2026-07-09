"""Shared Google OAuth helper for the calendar and email tools.

First run opens a browser for consent and caches a token at GOOGLE_TOKEN_PATH.
Subsequent runs (including unattended launchd runs) reuse/refresh that token
silently — no browser interaction required after the first authorization.

Setup (one-time, manual):
  1. https://console.cloud.google.com/ -> create/select a project
  2. Enable "Google Calendar API", "Gmail API", and "Google Tasks API"
  3. OAuth consent screen -> External -> add your own email as a test user
  4. Credentials -> Create Credentials -> OAuth client ID -> Desktop app
  5. Download the JSON, save it as config/google_credentials.json
  6. Run: python -m agent.tools.google_auth   (opens browser once, caches token)

Adding a new scope to SCOPES below requires deleting the cached
config/google_token.json and re-running this module once — a cached token is
locked to the scopes it was originally consented to.
"""

import os
import threading
from pathlib import Path

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

_ROOT = Path(__file__).resolve().parent.parent.parent
_ENV_PATH = _ROOT / "config" / ".env"
load_dotenv(_ENV_PATH)

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/tasks",
]

# Cached for the life of the process so the always-on chat server and
# multi-call tasks don't re-read/parse the token file on every tool call.
_CACHED_CREDS: Credentials | None = None

# Serializes credential acquisition/refresh and service construction. The chat
# server runs Flask with threaded=True, so two requests can hit these globals
# at once — without the lock, concurrent token refreshes could race two writers
# on google_token.json, and two service builds could race the cache.
_LOCK = threading.Lock()

# Built Google API clients, cached per (api, version) — see build_service().
_SERVICES: dict[tuple[str, str], object] = {}


def get_credentials() -> Credentials:
    global _CACHED_CREDS
    # Held for the whole body so a concurrent refresh can't race two writers on
    # google_token.json (see _LOCK). The valid-cache fast path is cheap under it.
    with _LOCK:
        if _CACHED_CREDS and _CACHED_CREDS.valid:
            return _CACHED_CREDS

        creds_path = _ROOT / os.getenv("GOOGLE_CREDENTIALS_PATH", "config/google_credentials.json")
        token_path = _ROOT / os.getenv("GOOGLE_TOKEN_PATH", "config/google_token.json")

        creds = _CACHED_CREDS
        if creds is None and token_path.exists():
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
            # Contains a refresh token — keep it readable only by the owner.
            os.chmod(token_path, 0o600)

        _CACHED_CREDS = creds
        return creds


def build_service(api: str, version: str):
    """Build (and cache per (api, version)) a Google API client. Shared by the
    calendar, gmail and tasks tools so each no longer carries an identical
    _SERVICE singleton. Built once per process; the underlying credentials
    refresh themselves. get_credentials() is called outside the service lock so
    the two locks never nest."""
    key = (api, version)
    service = _SERVICES.get(key)
    if service is not None:
        return service
    creds = get_credentials()
    with _LOCK:
        service = _SERVICES.get(key)
        if service is None:
            service = build(api, version, credentials=creds)
            _SERVICES[key] = service
        return service


if __name__ == "__main__":
    get_credentials()
    print("Google OAuth token cached successfully.")
