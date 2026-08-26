#!/bin/bash
# Reload Wren's launchd agents after a Homebrew Python upgrade.
#
# `brew upgrade python@3.12` deletes the old Cellar directory, and two things
# break in ways that produce no alert at all:
#
#   1. launchd cached the old interpreter's code signature and refuses to exec
#      the replacement. Jobs die at launch with OS_REASON_CODESIGNING (exit -9)
#      *before* running any of our code — so no log line, and notify_failure
#      never gets the chance to push. The job simply appears not to have run.
#   2. The chat server keeps serving from the deleted path. Modules already in
#      memory are fine; anything imported lazily afterwards raises
#      ModuleNotFoundError. On 2026-08-13 that emptied the dashboard graphs and
#      /map — datetime.strptime pulls in _strptime on first use — while the rest
#      of the page kept working, which is why it read as a UI bug.
#
# Both are one reload away. Run this after any brew upgrade that touches python:
#
#   ./launchd/reload-after-upgrade.sh           # heal whatever is stale
#   ./launchd/reload-after-upgrade.sh --check   # report only; exit 1 if stale
#   ./launchd/reload-after-upgrade.sh --quiet   # say nothing unless it acts
#
# Safe to run any time: it reloads only what is actually stale, and skips jobs
# that are mid-run rather than killing work in progress. install.sh also clears
# this as a side effect of reloading everything — this script exists because it
# tells you whether you needed it, and won't interrupt a running job to do it.
#
# Staleness is judged two ways, and the first is why the second is not enough:
#
#   proactive — the interpreter's identity (realpath + inode + mtime) is
#     recorded in config/.interpreter_id. When it changes, EVERY agent is
#     reloaded, because every one of them is now carrying a signature launchd
#     will reject. This is the check that matters.
#   reactive  — "needs LWCR update" on an individual job. launchd only sets
#     that flag AFTER a job has tried to exec and failed, so on its own it can
#     never prevent a missed run, only notice one. Kept as a backstop for
#     breakage that isn't an interpreter swap.
#
# The reactive check alone lost a run of every job, every upgrade. A job that
# hadn't fired since the upgrade carried no flag, so this script declared it
# healthy and walked past it; it stayed broken until its next scheduled fire,
# and that fire was the one that died. Cost scales with the schedule: the
# 2026-08-13 07:41 brew upgrade cost the daily jobs one day each, and cost the
# two Sunday jobs (starredblurbs, opportunitydigest) a full week — they were
# not healed until they died at 20:00 and 21:00 on 2026-08-16.
#
# Hence also the kickstart below: a job flagged by the reactive check has by
# definition just missed its window, so it is re-run once immediately. That
# turns a week-long hole into an 18-minute delay.
#
# local.wren.selfheal runs this hourly with --quiet. That job deliberately does
# NOT go through .venv/bin/python like every other plist: the interpreter is the
# thing that gets invalidated, so a Python healer would be killed by the exact
# failure it exists to repair. /bin/bash is Apple-signed and survives.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DOMAIN="gui/$(id -u)"
AGENTS="$HOME/Library/LaunchAgents"
SERVER_LABEL="local.wren.wren"
INTERPRETER_ID="$ROOT/config/.interpreter_id"

CHECK_ONLY=0
QUIET=0
case "${1:-}" in
    --check) CHECK_ONLY=1 ;;
    --quiet) QUIET=1 ;;
    "")      ;;
    *)       echo "usage: $(basename "$0") [--check|--quiet]" >&2; exit 2 ;;
esac

# A repair is a real event — an upgrade silently broke the schedule and this put
# it back. Push it, because the alternative is finding out from whatever job
# didn't run. Never fail the repair just because the push didn't go out.
push() {
    [ -x "$ROOT/.venv/bin/python" ] || return 0
    (cd "$ROOT" && "$ROOT/.venv/bin/python" -m agent.tools.notify \
        --message "$1" --title "Wren: agents reloaded after an upgrade") \
        >/dev/null 2>&1 || true
}

# launchd reports the cached-signature mismatch as "needs LWCR update" (launch
# weak code requirement). That is the authoritative signal — an exit status of
# -9 alone can't distinguish it from any other kill.
needs_reload() {
    launchctl print "$DOMAIN/$1" 2>/dev/null | grep -q "needs LWCR update"
}

is_running() {
    launchctl print "$DOMAIN/$1" 2>/dev/null | grep -qE '^[[:space:]]*state = running'
}

# Identity of the interpreter every other plist execs. Resolved to its real
# Cellar path and stamped with inode and mtime, so a brew upgrade that lands a
# new binary on the SAME path is still detected — the symlink chain out of
# .venv/bin/python doesn't change on every upgrade, but the file it lands on
# always does. Empty output (missing interpreter) is treated as "unknown" and
# never overwrites a good recorded value.
fingerprint() {
    local real
    real=$(/usr/bin/readlink -f "$ROOT/.venv/bin/python" 2>/dev/null) || return 0
    [ -n "$real" ] && /usr/bin/stat -f '%N %i %m' "$real" 2>/dev/null
}

# Does this agent actually exec the interpreter that changed? An interpreter
# swap can only invalidate the agents that run it, so the other three must not
# be swept up in the proactive reload:
#
#   colima      — /bin/bash into colima-start.sh. Bouncing it restarts the VM,
#                 which takes the ntfy push server down with it. That is Wren's
#                 only alerting channel; a Python upgrade must not silence it.
#   weighanchor — node.
#   selfheal    — /bin/bash, i.e. THIS script. Booting it out here would kill
#                 the repair mid-flight.
#
# Read from ProgramArguments rather than grepping the file: the selfheal plist
# names .venv/bin/python in a comment explaining why it doesn't use it, and a
# plain grep takes that as a match.
uses_interpreter() {
    [ "$(/usr/bin/plutil -extract ProgramArguments.0 raw -o - "$AGENTS/$1.plist" 2>/dev/null)" \
      = "$ROOT/.venv/bin/python" ]
}

# Only calendar-scheduled jobs are worth re-running after a failed launch. The
# server is KeepAlive and restarts itself; the StartInterval pollers
# (remindersweep, bgworker, selfheal) fire again within seconds on their own.
# Both would be started off-schedule for nothing.
is_catchup_candidate() {
    [ "$1" = "$SERVER_LABEL" ] && return 1
    grep -q "StartCalendarInterval" "$AGENTS/$1.plist" 2>/dev/null
}

# The server is a KeepAlive daemon, so "is it running" says nothing about whether
# it's healthy. What matters is whether the interpreter it exec'd still exists.
server_is_stale() {
    local pid exe
    pid=$(launchctl print "$DOMAIN/$SERVER_LABEL" 2>/dev/null \
          | awk '/^[[:space:]]*pid = /{print $3; exit}')
    [ -n "${pid:-}" ] || return 1
    exe=$(ps -o comm= -p "$pid" 2>/dev/null | sed 's/^[[:space:]]*//')
    [ -n "$exe" ] && [ ! -e "$exe" ]
}

# Returns non-zero if the agent could not be brought back. Callers MUST test it
# — never call this bare under `set -e`.
#
# `launchctl bootout` is asynchronous: it returns as soon as the teardown is
# queued, while the service is still leaving the domain. Bootstrapping into a
# domain that still holds it fails with "Input/output error" (5). That is not
# hypothetical — reloading all 17 agents in one pass hit it on local.wren.wren,
# and because the failure propagated through `set -e` the script died right
# there, leaving the chat server booted OUT rather than reloaded. A healer must
# never be able to end a run with fewer services than it started with.
#
# So: wait for the service to actually go, then retry the bootstrap.
reload() {
    local label="$1" i
    launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
    for i in $(seq 1 20); do
        launchctl print "$DOMAIN/$label" >/dev/null 2>&1 || break
        sleep 0.5
    done
    for i in 1 2 3; do
        if launchctl bootstrap "$DOMAIN" "$AGENTS/$label.plist" 2>/dev/null; then
            return 0
        fi
        sleep 2
    done
    # One last attempt with stderr shown, so the log says why it failed.
    launchctl bootstrap "$DOMAIN" "$AGENTS/$label.plist" || return 1
}

# `|| true` on both: a missing interpreter or a missing record must leave these
# empty, not abort the run under `set -e`. A vanished .venv is precisely when
# this script has to keep working.
now_fp="$(fingerprint || true)"
was_fp="$(cat "$INTERPRETER_ID" 2>/dev/null || true)"

# Missing baseline means this is the first run since the check was added (or a
# fresh install, where install.sh has just bootstrapped everything). Seed it
# rather than reloading agents that are already healthy — otherwise every new
# checkout opens with a spurious "reloaded everything" push.
seeding=0
if [ -z "$was_fp" ]; then
    seeding=1
fi

# An unreadable interpreter can't be compared against anything. Say nothing and
# leave the recorded value alone; the reactive check below still applies.
interpreter_changed=0
if [ -n "$now_fp" ] && [ "$seeding" -eq 0 ] && [ "$now_fp" != "$was_fp" ]; then
    interpreter_changed=1
fi

# `flagged` is the reactive set — these have already failed to launch, so they
# are the only ones eligible for a catch-up run. `stale` is everything to
# reload, which on an interpreter change is every agent we own.
flagged=()
stale=()
# Both prefixes: journaling runs under local.scribe.* since Scribe was split
# out of Wren, and those agents exec the same .venv interpreter, so leaving them
# out of this glob would leave exactly them broken after a Homebrew python bump.
for dest in "$AGENTS"/local.wren.*.plist "$AGENTS"/local.scribe.*.plist; do
    [ -e "$dest" ] || continue
    label="$(basename "$dest" .plist)"
    [ "$label" = "$SERVER_LABEL" ] && continue   # judged by interpreter, below
    if needs_reload "$label"; then
        flagged+=("$label")
        stale+=("$label")
    elif [ "$interpreter_changed" -eq 1 ] && uses_interpreter "$label"; then
        stale+=("$label")
    fi
done

server_stale=0
if [ "$interpreter_changed" -eq 1 ] || server_is_stale || needs_reload "$SERVER_LABEL"; then
    server_stale=1
fi

if [ ${#stale[@]} -eq 0 ] && [ "$server_stale" -eq 0 ]; then
    # Record the interpreter we just certified everything against. Never on
    # --check: that flag must not change state, or the next real run would
    # think it had already healed the upgrade.
    if [ "$CHECK_ONLY" -eq 0 ] && [ -n "$now_fp" ] && [ "$now_fp" != "$was_fp" ]; then
        printf '%s\n' "$now_fp" > "$INTERPRETER_ID"
        [ "$QUIET" -eq 1 ] || echo "recorded interpreter fingerprint"
    fi
    # Silent on the healthy path, like tasks/reminder_sweep.py: this runs hourly,
    # so a line per run would bury the handful that matter under thousands.
    [ "$QUIET" -eq 1 ] || echo "nothing stale — every agent matches the current interpreter"
    exit 0
fi

# Timestamped because this log accumulates a handful of rare events over months;
# "reloaded morningbrief" is only useful next to the upgrade that caused it.
log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*"; }

# Every array expansion below is length-guarded. /bin/bash on macOS is 3.2, where
# "${arr[@]}" on an *empty* array under `set -u` aborts with "unbound variable" —
# which would have crashed exactly when only the server was stale, instead of
# restarting it.
if [ "$interpreter_changed" -eq 1 ]; then
    log "interpreter changed: ${was_fp:-<none>} -> $now_fp"
fi
if [ ${#stale[@]} -gt 0 ]; then
    for label in "${stale[@]}"; do
        log "stale: $label"
    done
fi
[ "$server_stale" -eq 1 ] && log "stale: $SERVER_LABEL"

if [ "$CHECK_ONLY" -eq 1 ]; then
    exit 1
fi

skipped=0
healed=""
failed=""
if [ ${#stale[@]} -gt 0 ]; then
    for label in "${stale[@]}"; do
        # A scheduled job caught mid-run is doing real work — the wiki ingest can
        # hold the single Ollama slot for the better part of an hour. Its next run
        # is already broken; one more missed run costs less than a killed one.
        if is_running "$label"; then
            log "  skipped $label — mid-run, rerun this once it finishes"
            skipped=1
            continue
        fi
        if reload "$label"; then
            healed="$healed ${label#local.*.}"
            log "  reloaded $label"
        else
            failed="$failed ${label#local.*.}"
            log "  FAILED to reload $label — it is not loaded right now"
        fi
    done
fi

if [ "$server_stale" -eq 1 ]; then
    if reload "$SERVER_LABEL"; then
        healed="$healed ${SERVER_LABEL#local.*.}"
        log "  restarted $SERVER_LABEL"
    else
        failed="$failed ${SERVER_LABEL#local.*.}"
        log "  FAILED to restart $SERVER_LABEL — the chat server is DOWN"
    fi
fi

# Catch-up, reactive path only. A job in `flagged` carried "needs LWCR update",
# which launchd sets only after that job tried to exec and failed — so its
# scheduled window is already gone and nothing will retry it until next time it
# comes round. For a weekly job that is seven days. Run it once now.
#
# Deliberately NOT done for jobs reloaded by the interpreter check alone: those
# were repaired before they ever fired, so starting them here would run them
# off-schedule for no reason.
caught_up=""
if [ ${#flagged[@]} -gt 0 ]; then
    for label in "${flagged[@]}"; do
        is_catchup_candidate "$label" || continue
        is_running "$label" && continue          # skipped above, or already back
        launchctl kickstart "$DOMAIN/$label" >/dev/null 2>&1 || true
        caught_up="$caught_up ${label#local.*.}"
        log "  re-ran $label — it missed its scheduled window"
    done
fi

# Record the interpreter only once every agent is actually back on it. Writing
# it while something is still stale would mark the upgrade handled, and the next
# hourly pass would skip the repair entirely — the one outcome worse than the
# bug this script fixes.
if [ -n "$now_fp" ] && [ "$now_fp" != "$was_fp" ] \
   && [ -z "$failed" ] && [ "$skipped" -eq 0 ]; then
    printf '%s\n' "$now_fp" > "$INTERPRETER_ID"
fi

if [ -n "$healed" ]; then
    if [ -n "$caught_up" ]; then
        healed="$healed (re-ran:$caught_up )"
    fi
    push "Reloaded after an interpreter change:$healed"
fi

# A failed reload means a service is loaded nowhere. Push it on its own — it is
# the one outcome that needs a human, and it must not be buried under the list
# of things that did work.
if [ -n "$failed" ]; then
    push "COULD NOT RELOAD:$failed — check logs/selfheal.launchd.log"
    log "failed to reload:$failed"
    exit 1
fi

if [ "$skipped" -eq 1 ]; then
    log "some agents were mid-run and still need a reload"
    exit 1
fi
log "done"
