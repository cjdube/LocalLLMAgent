#!/bin/bash
# Install Wren's launchd agents.
#
# The committed plists carry __WREN_ROOT__ and __HOME__ placeholders instead of
# absolute paths — launchd expands neither ~ nor $HOME in ProgramArguments, so
# the substitution has to happen at install time. This fills them in from where
# the repo actually sits and bootstraps each agent.
#
#   ./launchd/install.sh                    # all agents in launchd/
#   ./launchd/install.sh launchd/local.wren.morningbrief.plist   # just these
#
# Re-running is safe: an already-loaded agent is booted out first. colima lives
# in launchd/infra/ and is NOT installed by default (it's optional infrastructure
# for the ntfy push server) — pass it explicitly to install it.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AGENTS="$HOME/Library/LaunchAgents"
DOMAIN="gui/$(id -u)"

mkdir -p "$AGENTS"

if [ $# -gt 0 ]; then
    plists=("$@")
else
    plists=("$ROOT"/launchd/*.plist)
fi

for src in "${plists[@]}"; do
    name="$(basename "$src")"
    label="${name%.plist}"
    dest="$AGENTS/$name"

    sed -e "s|__WREN_ROOT__|$ROOT|g" -e "s|__HOME__|$HOME|g" "$src" > "$dest"

    # bootout, not unload: bootout is the modern verb and is what pairs with
    # bootstrap. Ignore its failure — an agent that isn't loaded yet is fine.
    launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
    launchctl bootstrap "$DOMAIN" "$dest"
    echo "installed $label"
done
