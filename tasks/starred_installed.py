"""Resolve the installed version of each starred repo the user actually has, for
the /starred view's "Installed" column. Non-interactive — run by launchd daily.

The user lists the repos they have installed in config/starred_installed.json
(hand-edited), keyed by repo full_name. Each entry is one of:

    "rtk-ai/rtk":        {"version_cmd": "rtk --version"}   # run it, read the version
    "mattpocock/skills": {"plugin": "mattpocock-skills@claude-plugins-official"}
    "some/thing":        {"version": "v1.1.0"}              # a version he maintains by hand

A version_cmd entry runs the command (locally, with a timeout) and extracts the
version from its output; a plugin entry reads the version Claude Code recorded
when it installed that plugin; a static entry takes the value as given — the last
resort, for anything with no version command and no installer record. Results
are cached in config/starred_installed_versions.json keyed by full_name, so
/api/starred reads a plain store and never runs a subprocess on the page's
request path — the same off-path posture as the releases cache.

A check that fails keeps the version the last successful run measured, flagged
`"stale": true` alongside the error, because the cache is rewritten whole each
run and a transient failure (a tool missing from PATH, say) must not wipe every
known-good version.

The command strings come from the user's own config file, not from any model or web
content, and run with a timeout and no shell. The whole cache is rewritten from
the source each run, so a repo removed from the source is pruned.

Usage:
    python -m tasks.starred_installed
"""

import os
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

# Claude Code's own record of every installed plugin and the version it fetched.
# CLAUDE_CONFIG_DIR is Claude Code's variable, not one of ours, so it is read
# rather than declared in config/.env.example.
PLUGINS_PATH = (
    Path(os.getenv("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))
    / "plugins"
    / "installed_plugins.json"
)


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


def _plugin_version(name: str):
    """(version, error) for a Claude Code plugin, read from the installer's own
    record. `name` is the fully qualified "<plugin>@<marketplace>" key that file
    uses. Nothing is executed and no PATH is involved, so this is the
    deterministic option wherever a repo is consumed as a plugin.

    Every step is shape-checked: this file belongs to another program, and a
    future schema change must degrade to an error string, not a traceback."""
    data = load_json(PLUGINS_PATH, None)
    if not isinstance(data, dict):
        return None, f"no plugin record at {PLUGINS_PATH}"
    entries = (data.get("plugins") or {}).get(name) if isinstance(data.get("plugins"), dict) else None
    if not isinstance(entries, list) or not entries:
        return None, f"plugin {name!r} not installed"
    # A plugin can be installed at more than one scope (user, project). Report
    # the first recorded version rather than guessing which scope a given
    # session resolves to.
    for entry in entries:
        if isinstance(entry, dict) and entry.get("version"):
            return str(entry["version"]), None
    return None, f"plugin {name!r} has no recorded version"


def _resolve(spec) -> dict:
    """One source entry -> {version, source, error}. A version_cmd is run, a
    plugin is looked up in Claude Code's installer record, a static 'version' is
    taken as-is. A malformed entry degrades to an error string (and a blank
    cell) rather than raising."""
    _KEYS = "'version_cmd', 'plugin' or 'version'"
    if not isinstance(spec, dict):
        return {"version": None, "source": "manual",
                "error": f"entry must be an object with {_KEYS}"}
    cmd = spec.get("version_cmd")
    if cmd:
        version, error = _run_version_cmd(cmd)
        return {"version": version, "source": "cmd", "error": error}
    plugin = spec.get("plugin")
    if plugin:
        version, error = _plugin_version(str(plugin))
        return {"version": version, "source": "plugin", "error": error}
    static = spec.get("version")
    if static:
        return {"version": str(static), "source": "manual", "error": None}
    return {"version": None, "source": "manual",
            "error": f"no {_KEYS} in entry"}


def main() -> int:
    logger = setup_logger("starred_installed")
    logger.info("Starting starred installed-version run")
    try:
        source = load_json(SOURCE_PATH, {})
        if not isinstance(source, dict):
            logger.error("starred_installed.json is not a JSON object; nothing to resolve")
            source = {}

        # A failed check must not erase a version we already measured: the whole
        # cache is rewritten each run, so a run where the command is simply not
        # on PATH would otherwise replace every good value with a blank cell.
        previous = load_json(INSTALLED_PATH, {})
        if not isinstance(previous, dict):
            previous = {}

        checked_at = datetime.now(timezone.utc).isoformat()
        cache = {}
        for full_name, spec in source.items():
            resolved = _resolve(spec)
            resolved["checked_at"] = checked_at
            if resolved["error"]:
                logger.warning("%s: %s", full_name, resolved["error"])
                prior = previous.get(full_name)
                prior_version = prior.get("version") if isinstance(prior, dict) else None
                if prior_version:
                    # Carried over, and marked stale so the page can say the
                    # number is the last good one rather than a fresh reading.
                    resolved["version"] = prior_version
                    resolved["stale"] = True
                    logger.warning(
                        "%s: kept last known version %s", full_name, prior_version
                    )
            cache[full_name] = resolved

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
