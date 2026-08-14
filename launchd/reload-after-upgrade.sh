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
#
# Safe to run any time: it reloads only what is actually stale, and skips jobs
# that are mid-run rather than killing work in progress. install.sh also clears
# this as a side effect of reloading everything — this script exists because it
# tells you whether you needed it, and won't interrupt a running job to do it.
set -euo pipefail

DOMAIN="gui/$(id -u)"
AGENTS="$HOME/Library/LaunchAgents"
SERVER_LABEL="local.wren.wren"

CHECK_ONLY=0
if [ "${1:-}" = "--check" ]; then
    CHECK_ONLY=1
elif [ $# -gt 0 ]; then
    echo "usage: $(basename "$0") [--check]" >&2
    exit 2
fi

# launchd reports the cached-signature mismatch as "needs LWCR update" (launch
# weak code requirement). That is the authoritative signal — an exit status of
# -9 alone can't distinguish it from any other kill.
needs_reload() {
    launchctl print "$DOMAIN/$1" 2>/dev/null | grep -q "needs LWCR update"
}

is_running() {
    launchctl print "$DOMAIN/$1" 2>/dev/null | grep -qE '^[[:space:]]*state = running'
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

reload() {
    launchctl bootout "$DOMAIN/$1" 2>/dev/null || true
    launchctl bootstrap "$DOMAIN" "$AGENTS/$1.plist"
}

stale=()
for dest in "$AGENTS"/local.wren.*.plist; do
    [ -e "$dest" ] || continue
    label="$(basename "$dest" .plist)"
    [ "$label" = "$SERVER_LABEL" ] && continue   # judged by interpreter, below
    needs_reload "$label" && stale+=("$label")
done

server_stale=0
if server_is_stale || needs_reload "$SERVER_LABEL"; then
    server_stale=1
fi

if [ ${#stale[@]} -eq 0 ] && [ "$server_stale" -eq 0 ]; then
    echo "nothing stale — every agent matches the current interpreter"
    exit 0
fi

for label in "${stale[@]}"; do
    echo "stale: $label"
done
[ "$server_stale" -eq 1 ] && echo "stale: $SERVER_LABEL (running on a deleted interpreter)"

if [ "$CHECK_ONLY" -eq 1 ]; then
    exit 1
fi

skipped=0
for label in "${stale[@]}"; do
    # A scheduled job caught mid-run is doing real work — the wiki ingest can
    # hold the single Ollama slot for the better part of an hour. Its next run
    # is already broken; one more missed run costs less than a killed one.
    if is_running "$label"; then
        echo "  skipped $label — mid-run, rerun this once it finishes"
        skipped=1
        continue
    fi
    reload "$label"
    echo "  reloaded $label"
done

if [ "$server_stale" -eq 1 ]; then
    reload "$SERVER_LABEL"
    echo "  restarted $SERVER_LABEL"
fi

if [ "$skipped" -eq 1 ]; then
    echo "some agents were mid-run and still need a reload"
    exit 1
fi
echo "done"
