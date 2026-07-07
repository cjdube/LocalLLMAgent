"""Shared boilerplate for the HTTP-backed tool modules.

weather.py, web_search.py, github_starred.py and strava.py all repeat the same
handful of things: locate config/.env and load it, resolve an API key from
arg > .env > environment, funnel request exceptions into a uniform
{"error": ...} shape, and a main() that prints the JSON result and exits
non-zero on error. These helpers hold that shared logic in one place so each
tool stays short and a new tool has less to copy.
"""

import json
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

# config/.env lives at the repo root, three levels up from agent/tools/.
ENV_PATH = Path(__file__).resolve().parent.parent.parent / "config" / ".env"


def load_env() -> None:
    """Load config/.env so os.getenv() sees keys from the file and the env."""
    load_dotenv(ENV_PATH)


def resolve_key(name: str, arg: str | None = None) -> str | None:
    """Resolve a credential: an explicit arg wins, else config/.env / env var.

    load_env() folds .env into the process environment, so a single os.getenv()
    covers both the file and a real environment variable.
    """
    return arg or os.getenv(name)


def missing_key_error(name: str) -> dict:
    """The uniform error dict returned when a required key can't be resolved."""
    return {"error": f"{name} not set (checked arg, config/.env, env var)"}


def http_error(exc: Exception, phase: str = "fetch") -> dict:
    """Map a requests exception (or any other) onto the uniform error dict the
    tool entrypoints return. Reproduces the messages the tools used inline:
    HTTP status when available, "network error" for other request failures,
    and "<phase> error" as the catch-all."""
    if isinstance(exc, requests.exceptions.HTTPError):
        resp = exc.response
        status = resp.status_code if resp is not None else "?"
        return {"error": f"HTTP {status}: {exc}"}
    if isinstance(exc, requests.exceptions.RequestException):
        return {"error": f"network error: {exc}"}
    return {"error": f"{phase} error: {exc}"}


def print_result(result: dict) -> int:
    """Print a tool result as pretty JSON; return a non-zero exit code if it
    carries an error. Shared by every tool module's main()."""
    print(json.dumps(result, indent=2))
    return 1 if "error" in result else 0
