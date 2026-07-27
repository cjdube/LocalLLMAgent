"""Resolve the installed version of each starred repo the user actually has, for
the /starred view's "Installed" column. Non-interactive — run by launchd daily.

The user lists the repos they have installed in config/starred_installed.json
(hand-edited), keyed by repo full_name. Each entry is one of:

    "rtk-ai/rtk":        {"version_cmd": "rtk --version"}   # run it, read the version
    "mattpocock/skills": {"version": "v1.1.0"}              # a version he maintains by hand

A version_cmd entry runs the command (locally, with a timeout) and extracts the
version from its output; a static entry takes the value as given — the only
option for file-based skills or hosted services with no version command. Results
are cached in config/starred_installed_versions.json keyed by full_name, so
/api/starred reads a plain store and never runs a subprocess on the page's
request path — the same off-path posture as the releases cache.

The command strings come from the user's own config file, not from any model or web
content, and run with a timeout and no shell. The whole cache is rewritten from
the source each run, so a repo removed from the source is pruned.

Usage:
    python -m tasks.starred_installed
"""

import re
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from agent.store import atomic_write_json, load_json, locked
from tasks._common import notify_failure, setup_logger

_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / "config" / ".env")

SOURCE_PATH = _ROOT / "config" / "starred_installed.json"
INSTALLED_PATH = _ROOT / "config" / "starred_installed_versions.json"

# Bound a hung or interactive `--version` so one bad command can't stall the run.
CMD_TIMEOUT = 10

# First version-looking token in a tool's --version output, e.g. "rtk 0.43.0".
_VERSION_TOKEN = re.compile(r"\d+\.\d+(?:\.\d+)?(?:[-.\w]+)?")


def _run_version_cmd(cmd: str):
    """(version, error) from running `cmd` locally. Never raises: a missing
    binary, a timeout, or version-less output becomes an error string, so one
    broken command degrades to a blank cell rather than failing the run. No
    shell — the string is split with shlex and executed directly, since it comes
    from the user's own config and needs no shell features."""
    try:
        proc = subprocess.run(
            shlex.split(cmd), capture_output=True, text=True, timeout=CMD_TIMEOUT
        )
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        return None, f"{type(e).__name__}: {e}"
    out = f"{proc.stdout or ''}\n{proc.stderr or ''}"  # some tools print version to stderr
    m = _VERSION_TOKEN.search(out)
    if not m:
        snippet = " ".join(out.split())[:80]
        return None, f"no version in output ({snippet or f'exit {proc.returncode}'})"
    return m.group(0), None


def _resolve(spec) -> dict:
    """One source entry -> {version, source, error}. A version_cmd is run; a
    static 'version' is taken as-is. A malformed entry degrades to an error
    string (and a blank cell) rather than raising."""
    if not isinstance(spec, dict):
        return {"version": None, "source": "manual",
                "error": "entry must be an object with 'version_cmd' or 'version'"}
    cmd = spec.get("version_cmd")
    if cmd:
        version, error = _run_version_cmd(cmd)
        return {"version": version, "source": "cmd", "error": error}
    static = spec.get("version")
    if static:
        return {"version": str(static), "source": "manual", "error": None}
    return {"version": None, "source": "manual",
            "error": "no 'version_cmd' or 'version' in entry"}


def main() -> int:
    logger = setup_logger("starred_installed")
    logger.info("Starting starred installed-version run")
    try:
        source = load_json(SOURCE_PATH, {})
        if not isinstance(source, dict):
            logger.error("starred_installed.json is not a JSON object; nothing to resolve")
            source = {}

        checked_at = datetime.now(timezone.utc).isoformat()
        cache = {}
        for full_name, spec in source.items():
            resolved = _resolve(spec)
            resolved["checked_at"] = checked_at
            cache[full_name] = resolved
            if resolved["error"]:
                logger.warning("%s: %s", full_name, resolved["error"])

        with locked(INSTALLED_PATH):
            atomic_write_json(INSTALLED_PATH, cache)
        logger.info("Resolved %d installed versions; run complete", len(cache))
        return 0
    except Exception as e:
        logger.exception("Starred installed-version run failed: %s", e)
        notify_failure("starred_installed", e, logger)
        return 1


if __name__ == "__main__":
    sys.exit(main())
