# The log inspector — how it works

Wren watches everything except itself. Every task and the chat server log their
own failures, and `agent/loop.py` logs the small model's strain signals — but
nothing ever read those files back. A task that failed at 5am, or one launchd
never fired at all, stayed invisible until the missing output was noticed days
later. The log inspector is the reader.

It is **quiet by default: silence means healthy.** A push always means something
needs attention.

Code: `tasks/log_inspector.py`, scheduled by
`launchd/com.craigdube.localllmagent.loginspector.plist`. It reuses the log
parsing in `chat/insights.py` (the dashboard's data layer) rather than
duplicating it.

## When does it run?

Every morning at 8:00, after the whole morning cluster (4:30 → 7:30) has
finished, so it inspects a complete night of work in one pass. It looks back a
fixed **24 hours** and holds no state — nothing to prune, nothing to corrupt. A
problem that persists re-alerts the next day, which is correct: it's still
broken.

## Why it never calls the model

Every other task that produces prose calls the local model. This one is pure
deterministic Python, on purpose: **a health check that called the model
couldn't report that the model is down**, which is the failure it most needs to
report. If Ollama is hung, `complete_text()` would hang with it. Keeping the
inspector model-free makes it the most reliable component in the system.

## The three signals

None subsumes the others.

**Signal A — the line scan** (`_scan_lines`). Reads every `logs/*.log` in the
window and classifies each `[LEVEL] message` line. This is what went wrong.

**Signal B — run outcomes** (`_task_outcomes`). For each scheduled task, uses
`parse_runs()` to ask whether it ran and finished. This catches what a line scan
*cannot see*: a task that crashed before logging anything, or that launchd never
started. Absence of evidence is the signal. Three outcomes:

| Outcome | Meaning |
|---|---|
| `failed` | The run logged an error. |
| `stalled` | Started, never logged an end, and is over an hour old — the process died without raising (SIGKILL, OOM), which no error line records. |
| `missing` | No run at all in the window. launchd never fired it, or it died on import. |

Daemons (chat server, `bg_worker`, `reminder_sweep`) are skipped — they emit no
run boundaries. Weekly tasks (any plist with a `Weekday` key) are skipped for
`missing`, since a 24h window can't tell "didn't run" from "isn't due."

**Signal C — the push channel probe** (`ntfy_health`, in `agent/tools/notify.py`
— the push channel's own module, since the dashboard's live `push up` pill runs
the same probe). Asks ntfy's `/v1/health` whether it's alive. This one is an
*active* check rather than a log scan, and it exists because of a specific
failure:

> On 2026-07-11 the Mac rebooted, colima failed to restart, and ntfy was down
> for **four days**. Not one line was logged about it — because nothing happened
> to need pushing. No task failed; no reminder came due. **A dead push channel
> is invisible to a log scan until something tries to use it, and by then the
> alert is the thing being lost.**

So the inspector asks directly, every morning. It probes `/v1/health` rather
than publishing, since a probe that published would alert the phone daily or
need a throwaway topic. An unset `NTFY_URL` is *not* a fault — it means push is
switched off on purpose.

This finding is reported first in the rollup and always at `high` priority. It
can only ever reach you by **email**, since the push carrying it is by
definition the thing that's broken — see the fallback below.

## The classifier is default-open

**Every `ERROR`/`CRITICAL`/`WARNING` line is reported unless it's on the noise
denylist.** This is deliberately the inverse of matching a list of known-bad
patterns: a `logger.warning` added anywhere in the codebase later surfaces on its
own, instead of being silently missed by an allowlist nobody remembered to
update. The whole codebase has ~17 `logger.warning` sites, so the denylist is
small and knowable.

This earns its keep immediately — it catches real partial failures nobody would
have thought to enumerate, like `calendar_colorizer` skipping an event with no
valid color, or `daily_youtube_learnings` writing a video list because the
synthesis was unusable.

**Severity.** `critical` (pushes at `high` priority) is any `ERROR`/`CRITICAL`
line, plus `warm_model failed` — a WARNING that really means Ollama is
unreachable and every model-using task is about to fail. Everything else is
`warn` (pushes at `default`).

**Strain labels** name the two known model-struggle signals in the rollup —
`reached num_ctx=` → *context overflow* (the prompt overflowed and the system
prompt was truncated off the front), `reached num_predict=` → *repetition loop*
(the generation hit the cap mid-loop). These labels are cosmetic, **not** the
filter; an unrecognised warning still reports, verbatim.

**The noise denylist** (in `NOISE`) — benign lines that would drown the signal:

| Pattern | Why it's noise |
|---|---|
| `result trimmed:` | The tool-result cap working as designed. |
| `login throttled` | The rate limiter working as designed. |
| `bg_resolve: rejected invalid or expired` | Expected — a stale ntfy button was tapped. |

`push failed, will retry` **used to be on this list** ("`reminder_sweep` retries
on its own — transient and self-healing"). The July 2026 outage disproved that:
over four days it was neither, and suppressing it hid the only passive signal
there was. It now reports. The rollup counts rather than lists, so even a 60s
retry loop collapses to one `N warnings: reminder_sweep(N)` line — which is the
general lesson: **suppress noise by summarising it, not by discarding it.**

To tune: add a substring to `NOISE` to silence a pattern, or to `STRAIN_LABELS`
to give it a friendly name in the rollup. Adding to `NOISE` is the only way to
make the inspector quieter — by design, you must name what you're choosing to
ignore.

## Which files it reads

`logs/*.log`, with three exclusions, each load-bearing:

- **`*.launchd.log`** — `setup_logger` attaches both a file handler *and* a
  stdout handler, and launchd captures that stdout into `<task>.launchd.log`.
  Every line exists in both files; reading both double-counts every error.
- **`log_inspector.log`** — this task logs its findings, quoting the offending
  text. Since the classifier matches substrings, scanning its own log would
  re-detect yesterday's findings as today's problems, forever.
- **`*.bak`** — needs no rule; the `*.log` glob already excludes it.

Rotated files (`.log.1`–`.3`) *are* read, via `_read_lines`, because a 24h
window can span a `RotatingFileHandler` rollover. They're pulled in by the
reader, not the glob, so they aren't double-read.

Logs from retired tasks (e.g. `weekly_learnings.log`) have no owning plist but
are still globbed. That's harmless and self-limiting: nothing writes to them
anymore, so their lines can never re-enter a 24h window.

## What the push looks like

`notify()` truncates at 500 chars, so the push is a **rollup of counts, never
raw lines**:

```
PUSH CHANNEL DOWN: ntfy unreachable: Connection refused
2 failed: morning_brief, ai_chat_learnings
1 didn't run: strava_download
Model strain: 5x repetition loop, 1x context overflow
2 error lines: wren(2)
```

The rollup is sent with `email_fallback=True`: it fires once a day and nothing
retries it, so a push that doesn't land means the findings are simply lost —
and the case where the push fails is precisely the case worth hearing about.

Per-line detail goes to `logs/log_inspector.log` and renders in the dashboard's
run-detail view. Those detail lines carry a ` -> ` marker on purpose: `insights.py`
treats a line without one as the run's own status line, so quoting another task's
"…run failed" would otherwise pollute this run's own error field.

**Exit code:** `0` when clean *and* when problems were found and pushed —
finding problems is a successful run. `1` only if the inspector itself failed.

## Expect deliberate overlap

Tasks that already self-report (e.g. `strava_download` pushes on a partial log)
will *also* appear in the 8:00 rollup. That's intended: the immediate push is the
alarm, the rollup is the overnight summary. The inspector is a safety net, not a
replacement.

## Running it by hand

```bash
.venv/bin/python -m tasks.log_inspector
```

Prints its findings and pushes if there are any. To inspect a wider window
without pushing, call the scan directly:

```python
from datetime import datetime
from tasks import log_inspector as li
li.WINDOW_HOURS = 24 * 7
for f in li._scan_lines(datetime.now()):
    print(f["ts"], f["severity"], f["source"], f["msg"][:80])
```
