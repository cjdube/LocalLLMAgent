"""Inspect Wren's own operational logs and push one rollup when something's wrong.
Non-interactive — run by launchd.

Every task and the chat server already log their failures, and agent/loop.py
already logs the small model's strain signals (a prompt that overflowed num_ctx
and silently lost the system prompt off the front, a generation that hit
num_predict mid-repetition-loop). Nothing ever read them back, so a task that
failed at 5am — or one launchd never fired at all — stayed invisible until the
missing output was noticed days later. The instrumentation existed; this is the
reader.

Deliberately model-free. A health check that called the model couldn't report
that the model is down, which is the failure it most needs to report — so this
is plain Python over the log files, and stays the most reliable thing here.

Two signals, neither of which subsumes the other:
  A. Error/warning lines in the window (_scan_lines) — what went wrong.
  B. Run outcomes per scheduled task (_task_outcomes) — whether each task ran
     and finished. Catches what a line scan cannot see: a task that crashed
     before logging anything, or that launchd never started. Absence is the
     signal.

Quiet by default: silence means healthy. Findings push as a counts rollup
(ntfy truncates at 500 chars); per-line detail lands in this task's own log,
which the dashboard renders.

Usage:
    python -m tasks.log_inspector
"""

import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.tools.notify import notify
from chat.insights import _LINE_RE, _parse_ts, _read_lines, discover_tasks, parse_runs
from tasks import _common
from tasks._common import notify_failure, setup_logger

WINDOW_HOURS = 24

# A run still marked "running" this long after it started never logged an end —
# the process died without raising (SIGKILL, OOM), which no error line records.
# Generous on purpose: the last daily task starts at 7:30 and this runs at 8:00,
# so a slow-but-healthy run is never mistaken for a dead one.
STALL_HOURS = 1

# Benign lines that would otherwise drown the signal. Everything else at
# WARNING or above is reported — see _classify for why this is a denylist.
NOISE = (
    "result trimmed:",                          # the tool-result cap working as designed
    "login throttled",                          # the rate limiter working as designed
    "bg_resolve: rejected invalid or expired",  # expected: a stale ntfy button was tapped
    "push failed, will retry",                  # reminder_sweep retries on its own
)

# WARNING-level lines that are really outages, not warnings.
CRITICAL_WARNINGS = (
    "warm_model failed",  # Ollama unreachable — every model-using task is about to fail
)

# Recognised strain signals, purely so the rollup can name them. This is NOT the
# filter (see _classify) — an unrecognised warning still reports, verbatim.
STRAIN_LABELS = (
    ("reached num_ctx=", "context overflow"),
    ("reached num_predict=", "repetition loop"),
)


def _skip_log(name: str) -> bool:
    """Which files in logs/ the line scan ignores.

    - *.launchd.log: setup_logger attaches a RotatingFileHandler AND a stdout
      StreamHandler, and launchd captures that stdout into <task>.launchd.log.
      Every line exists in both files; reading both double-counts every error.
    - log_inspector.log: this task logs its findings, quoting the offending
      text. Since _classify matches on substrings, scanning our own log would
      re-detect yesterday's findings as today's problems, forever.

    (*.bak needs no rule — the *.log glob already excludes it. Rotated .log.1-.3
    are pulled in by _read_lines, not the glob, so they aren't double-read.)
    """
    return name.endswith(".launchd.log") or name == "log_inspector.log"


def _classify(level: str, msg: str) -> tuple[str, str | None]:
    """-> (severity, label). severity is "critical" | "warn".

    Default-open: every ERROR/CRITICAL/WARNING that isn't on the NOISE denylist
    is a finding. Deliberately the inverse of matching known-bad patterns — a
    logger.warning added anywhere later surfaces on its own instead of being
    silently missed by an allowlist nobody remembered to update.
    """
    if level in ("ERROR", "CRITICAL"):
        return "critical", None
    if any(pat in msg for pat in CRITICAL_WARNINGS):
        return "critical", None
    for pat, label in STRAIN_LABELS:
        if pat in msg:
            return "warn", label
    return "warn", None


def _scan_lines(now: datetime) -> list[dict]:
    """Signal A: every reportable log line in the window, oldest first."""
    cutoff = now - timedelta(hours=WINDOW_HOURS)
    findings = []
    for path in sorted(_common.LOGS_DIR.glob("*.log")):
        if _skip_log(path.name):
            continue
        for line in _read_lines(path):
            m = _LINE_RE.match(line)
            if not m:
                continue  # traceback continuation; the [ERROR] above it already counted
            ts, level, msg = m.group(1), m.group(2), m.group(3)
            if level not in ("WARNING", "ERROR", "CRITICAL"):
                continue
            when = _parse_ts(ts)
            if when is None or when < cutoff:
                continue
            if any(pat in msg for pat in NOISE):
                continue
            severity, label = _classify(level, msg)
            findings.append({
                "source": path.stem, "ts": ts, "level": level,
                "msg": msg, "severity": severity, "label": label,
            })
    findings.sort(key=lambda f: f["ts"])
    return findings


def _task_outcomes(now: datetime) -> dict[str, list[str]]:
    """Signal B: -> {"failed": [...], "stalled": [...], "missing": [...]} by task key."""
    cutoff = now - timedelta(hours=WINDOW_HOURS)
    stall_cutoff = now - timedelta(hours=STALL_HOURS)
    out: dict[str, list[str]] = {"failed": [], "stalled": [], "missing": []}

    for task in discover_tasks():
        # Daemons (chat server, bg_worker, reminder_sweep) never emit run
        # boundaries, so parse_runs has nothing to judge them by.
        if task["is_daemon"]:
            continue
        # A Weekday key means weekly: a 24h window can't tell "didn't run" from
        # "isn't due". No task is weekly today; this keeps a future one quiet.
        if "Weekday" in (task["schedule"] or {}):
            continue

        recent = []
        for run in parse_runs(task["log_path"], limit=10):
            start = _parse_ts(run["start"])
            if start and start >= cutoff:
                recent.append((run, start))

        if not recent:
            out["missing"].append(task["key"])
            continue
        if any(r["status"] == "failure" for r, _ in recent):
            out["failed"].append(task["key"])
        elif any(r["status"] == "running" and s < stall_cutoff for r, s in recent):
            out["stalled"].append(task["key"])
    return out


def _by_source(findings: list[dict]) -> str:
    counts = Counter(f["source"] for f in findings)
    return ", ".join(f"{src}({n})" for src, n in counts.most_common())


def _rollup(outcomes: dict[str, list[str]], findings: list[dict]) -> str:
    """The push body: counts, never raw lines — notify() truncates at 500 chars."""
    lines = []
    for kind, prefix in (("failed", "failed"), ("stalled", "stalled"), ("missing", "didn't run")):
        keys = outcomes[kind]
        if keys:
            lines.append(f"{len(keys)} {prefix}: {', '.join(keys)}")

    strain = Counter(f["label"] for f in findings if f["label"])
    if strain:
        lines.append("Model strain: " + ", ".join(f"{n}x {lbl}" for lbl, n in strain.most_common()))

    errors = [f for f in findings if f["severity"] == "critical"]
    if errors:
        lines.append(f"{len(errors)} error lines: {_by_source(errors)}")

    other = [f for f in findings if f["severity"] == "warn" and not f["label"]]
    if other:
        lines.append(f"{len(other)} warnings: {_by_source(other)}")

    return "\n".join(lines)


def _is_urgent(outcomes: dict[str, list[str]], findings: list[dict]) -> bool:
    return bool(
        outcomes["failed"] or outcomes["stalled"] or outcomes["missing"]
        or any(f["severity"] == "critical" for f in findings)
    )


def main() -> int:
    logger = setup_logger("log_inspector")
    logger.info("Starting log inspector run")

    try:
        now = datetime.now()
        # Log timestamps are naive LOCAL (logging's asctime) and _parse_ts reads
        # them as such, so comparing against a naive local now() is consistent.
        # This is the one spot the repo's UTC-vs-local rule doesn't apply — no
        # UTC source is involved — but it looks exactly like the cases where it
        # does, hence the note.
        findings = _scan_lines(now)
        outcomes = _task_outcomes(now)

        # " -> " on purpose: insights.py treats it as the marker for a data line
        # (see _parse_runs_uncached), so a quoted message containing "failed" or
        # "run complete" can't be misread as THIS run's own status.
        for f in findings:
            logger.info(f"finding -> {f['source']} {f['ts']} [{f['level']}] {f['msg']}")
        for kind, keys in outcomes.items():
            for key in keys:
                logger.info(f"task {kind} -> {key}")

        summary = _rollup(outcomes, findings)
        if not summary:
            logger.info(f"No issues in the last {WINDOW_HOURS}h")
            logger.info("Log inspector run complete")
            return 0

        logger.info(f"pushing rollup -> {summary!r}")
        result = notify(
            message=summary,
            title="Wren: overnight issues",
            priority="high" if _is_urgent(outcomes, findings) else "default",
        )
        if result.get("error"):
            logger.warning(f"Rollup push via ntfy did not send: {result['error']}")

        logger.info("Log inspector run complete")
        return 0
    except Exception as e:
        logger.exception(f"Log inspector run failed: {e}")
        notify_failure("log_inspector", e, logger)
        return 1


if __name__ == "__main__":
    sys.exit(main())
