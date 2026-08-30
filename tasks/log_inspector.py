"""Inspect Wren's operational logs and push one rollup when something's wrong.
Non-interactive — run by launchd.

"Wren's logs" includes the sibling repos named by WREN_EXTERNAL_TASK_ROOTS (see
docs/external-tasks.md): their jobs alert on their own failures, but nothing
noticed a job launchd never fired at all, which is exactly Signal B below.

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

from agent.tools.notify import _MAX_MESSAGE_CHARS, notify, ntfy_health
from chat.insights import (
    _LINE_RE,
    _external_roots,
    _parse_ts,
    _read_lines,
    discover_tasks,
    parse_runs,
)
from tasks import _common
from tasks._common import notify_failure, setup_logger

WINDOW_HOURS = 24

# A run still marked "running" this long after it started never logged an end —
# the process died without raising (SIGKILL, OOM), which no error line records.
#
# What keeps a slow-but-healthy run from reading as dead is the schedule gap, not
# this number: every scheduled task starts hours before the 8:00 inspection (the
# latest, starred_installed, at 20:10), so nothing is legitimately in flight when
# we look. The margin is thinner than it appears, though — ai_chat_learnings has
# taken 56 minutes, 93% of an hour. Raise this if a task is ever scheduled within
# ~2h of 8:00, or trim that task's runtime.
STALL_HOURS = 1

# Benign lines that would otherwise drown the signal. Everything else at
# WARNING or above is reported — see _classify for why this is a denylist.
NOISE = (
    # "result trimmed:" was here, described as "the cap working as designed".
    # It wasn't. A trimmed result reads to the model as a complete one, so the
    # cap firing is silent data loss: read_wiki_index shed 52232 chars a day for
    # weeks, and Wren answered "SVPG isn't in your wiki" from a page whose SVPG
    # section had been cut. This line was the only signal, and it was suppressed
    # by definition. Same mistake, same fix, as "push failed, will retry" below.
    #
    # Suppression made sense while five tools trimmed routinely. Now every tool
    # bounds its own result (agent/tools/*, loop.TOOL_RESULT_CHAR_CAPS), so a
    # trim means one outgrew its budget — rare, and exactly worth a push.
    "login throttled",                          # the rate limiter working as designed
    "bg_resolve: rejected invalid or expired",  # expected: a stale ntfy button was tapped
    # "push failed, will retry" was here, on the theory that reminder_sweep's
    # retry makes it self-healing. The July 2026 outage killed that theory: over
    # four days it was neither transient nor self-healing, and suppressing it
    # hid the one signal we did have. The rollup reports by count, so even a
    # 60s retry loop collapses to a single "N warnings: reminder_sweep(N)" line.
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


def _scan_log_dirs() -> list[Path]:
    """Wren's logs/ plus the log directory of every federated task, so a sibling
    repo's scheduled jobs report into the same rollup. Signal B needs no
    equivalent — _task_outcomes goes through discover_tasks(), which is already
    federated.

    Each root's <root>/logs PLUS the directory every federated task actually
    logs to. Both, not just the second: an external repo installed as a tool
    logs outside its checkout (and _task_from_plist has already worked out
    where), but a log left behind by a job whose plist is gone still carries
    lines worth reporting, and only the first half finds those. Ordered,
    deduplicated, Wren's own first.
    """
    dirs = [_common.LOGS_DIR]
    candidates = [root / "logs" for _, root, _ in _external_roots()]
    candidates += [Path(t["log_path"]).parent
                   for t in discover_tasks() if t["external"]]
    for path in candidates:
        if path not in dirs:
            dirs.append(path)
    return dirs


def _scan_lines(now: datetime) -> list[dict]:
    """Signal A: every reportable log line in the window, oldest first."""
    cutoff = now - timedelta(hours=WINDOW_HOURS)
    findings = []
    paths = [p for d in _scan_log_dirs() for p in sorted(d.glob("*.log"))]
    for path in paths:
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
        # Judge a task only on the inspection run that follows a scheduled start,
        # so absence means "was due and didn't run" rather than "isn't due yet".
        # For a daily task that's every run (a 24h period always lands in a 24h
        # window), so this is a no-op there. For a weekly one it's the single run
        # after its due time — previously those were skipped outright, which left
        # opportunity_digest and starred_blurbs with no Signal B at all.
        due = _last_due(task["schedule"], now)
        if due is None or due < cutoff:
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


def _last_due(sci: dict | None, now: datetime) -> datetime | None:
    """The most recent wall-clock time launchd should have started this task,
    or None if the schedule isn't one we can reason about.

    Naive local throughout, matching launchd (which schedules on wall clock) and
    the log timestamps — see the note in main().
    """
    if not isinstance(sci, dict) or not set(sci).issubset({"Hour", "Minute", "Weekday"}):
        return None
    due = now.replace(hour=sci.get("Hour", 0), minute=sci.get("Minute", 0),
                      second=0, microsecond=0)
    if "Weekday" not in sci:
        return due if due <= now else due - timedelta(days=1)
    # launchd's Weekday and isoweekday() agree once both are taken mod 7:
    # Sunday is 0 or 7 in launchd and 7 in isoweekday, Mon-Sat are 1-6 in both.
    due -= timedelta(days=(due.isoweekday() % 7 - sci["Weekday"] % 7) % 7)
    return due if due <= now else due - timedelta(days=7)


def _by_source(findings: list[dict]) -> str:
    counts = Counter(f["source"] for f in findings)
    return ", ".join(f"{src}({n})" for src, n in counts.most_common())


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


# Per-line cap on quoted finding text. A single message can be a whole exception
# repr, and without this one verbose warning eats the budget three terser ones
# would have shared.
MAX_DETAIL_LINE = 150

# ASCII on purpose. The cap notify() enforces counts CHARACTERS, while ntfy's
# own limits are on BYTES, so multi-byte punctuation would make the two
# disagree. The counts lines are already plain ASCII; keep these the same.
_ELLIPSIS = "..."


def _detail(findings: list[dict], budget: int) -> list[str]:
    """Verbatim finding text, most severe first, for as many findings as fit.

    The counts answer "how much broke"; these answer "what". When the budget
    runs out the rollup degrades to counts alone — which is a busy night's old
    behaviour, reached by arithmetic rather than by a threshold to tune.
    """
    # Stable sort, so criticals lead and each severity keeps its ts order.
    lines = []
    for f in sorted(findings, key=lambda f: f["severity"] != "critical"):
        # ts is our own naive-local log stamp (see the note in main()), so
        # slicing out HH:MM is display formatting, not the local-vs-UTC slice
        # the repo bans.
        text = f"- {f['ts'][11:16]} {f['source']}: {f['msg']}"
        if len(text) > MAX_DETAIL_LINE:
            text = text[:MAX_DETAIL_LINE - len(_ELLIPSIS)] + _ELLIPSIS
        if len(text) + 1 > budget:  # +1 for the newline joining it on
            break
        lines.append(text)
        budget -= len(text) + 1
    return lines


def _rollup(outcomes: dict[str, list[str]], findings: list[dict],
            channel_error: str | None = None) -> str:
    """The push body: a counts header, then as much verbatim finding text as the
    ntfy cap leaves room for.

    It was counts and nothing else until 2026-08-07, on the reasoning that
    notify() truncates at 500 chars. That holds for a fifty-finding night. On a
    one-finding night it spent 19 of those characters and discarded the other
    481, so the push read "1 warnings: wren(1)" while the message that said
    exactly what was wrong stayed in the log where no phone could reach it.
    """
    lines = []
    if channel_error:
        # First line on purpose: this one only ever arrives by email (the push
        # carrying it is, by definition, the thing that's broken).
        lines.append(f"PUSH CHANNEL DOWN: {channel_error}")
    for kind, prefix in (("failed", "failed"), ("stalled", "stalled"), ("missing", "didn't run")):
        keys = outcomes[kind]
        if keys:
            lines.append(f"{len(keys)} {prefix}: {', '.join(keys)}")

    strain = Counter(f["label"] for f in findings if f["label"])
    if strain:
        lines.append("Model strain: " + ", ".join(f"{n}x {lbl}" for lbl, n in strain.most_common()))

    errors = [f for f in findings if f["severity"] == "critical"]
    if errors:
        lines.append(f"{_plural(len(errors), 'error line')}: {_by_source(errors)}")

    other = [f for f in findings if f["severity"] == "warn" and not f["label"]]
    if other:
        lines.append(f"{_plural(len(other), 'warning')}: {_by_source(other)}")

    counts = "\n".join(lines)
    if not counts:
        return ""
    return "\n".join([counts] + _detail(findings, _MAX_MESSAGE_CHARS - len(counts)))


def _is_urgent(outcomes: dict[str, list[str]], findings: list[dict],
               channel_error: str | None = None) -> bool:
    return bool(
        channel_error
        or outcomes["failed"] or outcomes["stalled"] or outcomes["missing"]
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
        # "off" (push deliberately disabled) reports no error, same as healthy.
        channel_error = ntfy_health()["error"]

        # " -> " on purpose: insights.py treats it as the marker for a data line
        # (see _parse_runs_uncached), so a quoted message containing "failed" or
        # "run complete" can't be misread as THIS run's own status.
        for f in findings:
            logger.info(f"finding -> {f['source']} {f['ts']} [{f['level']}] {f['msg']}")
        for kind, keys in outcomes.items():
            for key in keys:
                logger.info(f"task {kind} -> {key}")
        if channel_error:
            logger.info(f"push channel -> {channel_error}")

        summary = _rollup(outcomes, findings, channel_error)
        if not summary:
            logger.info(f"No issues in the last {WINDOW_HOURS}h")
            logger.info("Log inspector run complete")
            return 0

        logger.info(f"pushing rollup -> {summary!r}")
        # email_fallback: this rollup fires once a day and nothing retries it,
        # so a failed push means the findings are simply lost — and the case
        # where the push fails is exactly the case worth hearing about.
        result = notify(
            message=summary,
            title="Wren: overnight issues",
            priority="high" if _is_urgent(outcomes, findings, channel_error) else "default",
            email_fallback=True,
        )
        if result.get("error"):
            logger.warning(f"Rollup push via ntfy did not send: {result['error']}")
            fallback = result.get("email_fallback") or {}
            logger.info(f"email fallback -> {fallback}")

        logger.info("Log inspector run complete")
        return 0
    except Exception as e:
        logger.exception(f"Log inspector run failed: {e}")
        notify_failure("log_inspector", e, logger)
        return 1


if __name__ == "__main__":
    sys.exit(main())
