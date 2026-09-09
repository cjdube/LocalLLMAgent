"""Move the settings out of config/.env and config/preferences.json, once.

    .venv/bin/python -m agent.migrate_settings            # dry run, prints the plan
    .venv/bin/python -m agent.migrate_settings --apply    # does it

Dry run by default because this touches the only credential file on the
machine.

The environment outranks the settings document on purpose — 251
monkeypatch.setenv calls and every `WREN_X=... python -m ...` one-off depend on
that order (agent/config.py). The cost is that a key still assigned in
config/.env wins over the page forever: the field renders, accepts an edit,
saves it, and changes nothing. This script is what pays that cost off, and
config.STARTUP_WARNINGS names the keys still overlapping until it runs.

What moves, and what does not:

  * a non-secret key with a schema row moves, including the seven locked ones
    (see config.set_value's allow_locked). A locked row is locked against
    editing, not against living in the document.
  * a secret row stays. Ten keys, deliberately: the page is never told a secret
    value, only whether it is set, so moving one would buy nothing and put a
    credential in a second file.
  * a key with no row stays, reported as "not recognised". This bucket is real
    and it is not a bug: STRAVA_*, ANTHROPIC_API_KEY, OPEN_ROUTER_API_KEY and
    the SCRIBEJAY_* keys have no reader in this repo. Deleting a credential
    because the schema does not know it is not acceptable, and neither is a
    set/not-set row for a key nothing reads.

Everything is written through config.set_value, config.set_preference and
config.flush — never by hand — so this script and the /settings page produce
byte-identical documents. A value the schema refuses stays in .env and is
reported; it never aborts the rest.

Nothing is clobbered. A key already in settings.json keeps the value it has and
is reported as skipped, so a second run is safe and does no work.
"""

import argparse
import json
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from dotenv import dotenv_values

from agent import config, prefs, schema


@dataclass
class Plan:
    """What a run would do. Built without writing anything, printed by the dry
    run, and then executed as-is — so what you read is what happens."""

    values: dict[str, str] = field(default_factory=dict)
    sections: dict[str, dict] = field(default_factory=dict)
    stay_secret: list[str] = field(default_factory=list)
    stay_unrecognised: list[str] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    problems: list[tuple[str, str]] = field(default_factory=list)
    # Set when anything in config/preferences.json could not be moved. The file
    # is only renamed when this is False: renaming one that still holds a
    # refused section would hide that section behind a name nothing loads, and
    # the user's own persona would go missing without a message.
    preferences_blocked: bool = False

    @property
    def moves_anything(self) -> bool:
        return bool(self.values or self.sections)


# --------------------------------------------------------------------------- #
# Planning
# --------------------------------------------------------------------------- #

def _plan_env(plan: Plan, staged: dict) -> None:
    """Partition config/.env three ways, staging what moves.

    dotenv_values parses the file the same way agent/config.py's load_dotenv
    does, so a quoted or escaped value migrates as the value the resolver
    already sees, not as the raw characters on the line.
    """
    for key, value in dotenv_values(config.env_path()).items():
        if value is None or not value.strip():
            continue
        row = schema.by_key(key)
        if row is None:
            plan.stay_unrecognised.append(key)
            continue
        if row.secret:
            plan.stay_secret.append(key)
            continue
        if key in config.CONFIG["values"]:
            plan.skipped.append((key, "already in settings.json, left as it is"))
            continue
        try:
            config.set_value(staged, key, value, allow_locked=True)
        except config.ConfigError as e:
            # Stays in .env. A value the schema refuses is a value the page
            # could not have saved either, so putting it in the document would
            # make the document the broken one.
            plan.problems.append((key, str(e)))
            continue
        plan.values[key] = value


def _plan_preferences(plan: Plan, staged: dict) -> None:
    """Fold config/preferences.json's sections in whole, and rehome `location`.

    `location` was always a bare string, so it was never a section and could
    never become one. It is the DEFAULT_LOCATION row now — but only if config/
    .env has not already set it, because .env is being read in the same run and
    the environment would outrank whatever this wrote.
    """
    path = config.preferences_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
        plan.problems.append((str(path), f"could not be read: {e}"))
        plan.preferences_blocked = True
        return
    if not isinstance(raw, dict):
        plan.problems.append((str(path), "is not a JSON object"))
        plan.preferences_blocked = True
        return

    for name, value in raw.items():
        if name == "location":
            _plan_location(plan, staged, value)
            continue
        if name not in schema.PREFERENCE_SECTIONS:
            # "_comment" and anything else hand-added. Not an error: the file is
            # about to be renamed, not deleted, so nothing is lost.
            plan.skipped.append((name, "not a preference section, not moved"))
            continue
        if name in config.CONFIG["preferences"]:
            plan.skipped.append((name, "already in settings.json, left as it is"))
            continue
        problems = prefs.validate_section(name, value)
        if problems:
            # Same rule as a refused .env value: the page could not have saved
            # this section either.
            plan.problems.append((name, "; ".join(problems)))
            plan.preferences_blocked = True
            continue
        config.set_preference(staged, name, value)
        plan.sections[name] = value


def _plan_location(plan: Plan, staged: dict, value) -> None:
    if not isinstance(value, str) or not value.strip():
        plan.skipped.append(("location", "empty, not moved"))
        return
    if "DEFAULT_LOCATION" in plan.values or "DEFAULT_LOCATION" in staged["values"]:
        plan.skipped.append(
            ("location", "config/.env already sets DEFAULT_LOCATION, kept that"))
        return
    try:
        config.set_value(staged, "DEFAULT_LOCATION", value.strip())
    except config.ConfigError as e:
        plan.problems.append(("location", str(e)))
        plan.preferences_blocked = True
        return
    plan.values["DEFAULT_LOCATION"] = value.strip()


def build_plan() -> tuple[Plan, dict]:
    """The plan, and the staged document that produces it. Writes nothing."""
    plan = Plan()
    staged = config._staged()
    _plan_env(plan, staged)
    _plan_preferences(plan, staged)
    return plan, staged


# --------------------------------------------------------------------------- #
# Rewriting config/.env
# --------------------------------------------------------------------------- #

def rewrite_env(text: str, moved: set[str]) -> str:
    """`text` with each moved key's assignment gone, and the comment block glued
    directly above it gone with it.

    A comment block with no blank line between it and the key documents that
    key, and the schema row's `help` already carries the same explanation to the
    page — so leaving it behind would orphan it in the one file a person
    hand-edits. A comment followed by a blank line heads a section and stays.

    Pure and line-based on purpose: the backup is the safety net, but a function
    that takes text and returns text is one a test can drive over every shape in
    the real file without touching it.
    """
    lines = text.splitlines()
    drop = [False] * len(lines)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name = stripped.split("=", 1)[0].strip()
        if name.startswith("export "):
            name = name[len("export "):].strip()
        if name not in moved:
            continue
        drop[i] = True
        j = i - 1
        while j >= 0 and lines[j].strip().startswith("#"):
            drop[j] = True
            j -= 1

    kept = [line for i, line in enumerate(lines) if not drop[i]]
    return _collapse_blank_runs(kept)


def _collapse_blank_runs(lines: list[str]) -> str:
    """One blank line where a removal left several, and none at the ends. Purely
    cosmetic, but a file left with six blank lines in a row reads as damaged."""
    out: list[str] = []
    for line in lines:
        if not line.strip() and (not out or not out[-1].strip()):
            continue
        out.append(line.rstrip())
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out) + "\n" if out else ""


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #

def describe(plan: Plan) -> str:
    out: list[str] = []

    def block(title: str, rows: list[str]) -> None:
        out.append(f"\n{title} ({len(rows)})")
        out.extend(f"  {row}" for row in rows or ["none"])

    block("MOVE to config/settings.json",
          [f"{k} = {v}" for k, v in plan.values.items()])
    block("MOVE preference sections",
          [f"{name} ({len(value)} keys)" for name, value in plan.sections.items()])
    block("STAY in config/.env — secret, the page is only told set/not-set",
          plan.stay_secret)
    block("STAY in config/.env — no schema row, so nothing here reads it",
          plan.stay_unrecognised)
    block("SKIPPED — nothing to do",
          [f"{name}: {why}" for name, why in plan.skipped])
    block("REFUSED — stays where it is, fix it by hand",
          [f"{name}: {why}" for name, why in plan.problems])
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# Applying
# --------------------------------------------------------------------------- #

def apply(plan: Plan, staged: dict) -> list[str]:
    """Do it, in the order that stays safe if it stops halfway.

    The document is written first, so a crash before the rewrite leaves
    config/.env still holding every key and settings.json holding a superset —
    the resolver answers the same either way. The rewrite comes second and the
    rename last, and both are preceded by a backup.
    """
    done: list[str] = []
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    env_path = config.env_path()
    if plan.values and env_path.exists():
        backup = env_path.with_name(f".env.pre-settings-{stamp}")
        shutil.copy2(env_path, backup)
        done.append(f"backed up {env_path} to {backup}")

    # Only what this plan touched. flush() merges onto the document as it is on
    # disk right now, so a key the running chat server saved while this script
    # was building its plan survives instead of being written back over.
    config.flush(staged, list(plan.values), list(plan.sections))
    done.append(f"wrote {config.settings_path()}: "
                f"{len(plan.values)} values, {len(plan.sections)} sections")

    if plan.values and env_path.exists():
        text = env_path.read_text(encoding="utf-8")
        env_path.write_text(rewrite_env(text, set(plan.values)), encoding="utf-8")
        done.append(f"removed {len(plan.values)} assignments from {env_path}")

    prefs_path = config.preferences_path()
    if plan.sections and not plan.preferences_blocked and prefs_path.exists():
        # Renamed, never deleted: it is the only copy of sections a person wrote
        # by hand. The new name is what stops a stale loader from finding it.
        retired = prefs_path.with_name(prefs_path.name + ".migrated")
        prefs_path.replace(retired)
        done.append(f"renamed {prefs_path} to {retired}")

    return done


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agent.migrate_settings",
        description="Move settings out of config/.env and config/preferences.json "
                    "into config/settings.json, which the /settings page owns.")
    parser.add_argument("--apply", action="store_true",
                        help="actually do it; without this the run only prints "
                             "the plan and writes nothing")
    args = parser.parse_args(argv)

    plan, staged = build_plan()
    print(describe(plan))

    if not plan.moves_anything:
        print("\nNothing to move. Already migrated, or nothing here has a schema row.")
        return 0

    if not args.apply:
        print("\nDRY RUN — nothing written. Re-read the lists above, then run:")
        print("  .venv/bin/python -m agent.migrate_settings --apply")
        return 0

    print()
    for line in apply(plan, staged):
        print(f"  {line}")
    print("\nDone. Restart the chat server so it re-reads the document:")
    print(f"  {schema.RESTART_COMMAND}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
