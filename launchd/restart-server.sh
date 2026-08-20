#!/bin/bash
# Restart Wren's chat server.
#
# `kickstart -k` kills the running instance and starts a fresh one in the same
# launchd job, so KeepAlive doesn't race us by respawning the old code first.
# This is the command to run after editing anything the server imports — the
# server reads config/.env and loads every module at startup only.
#
# Not a substitute for launchd/install.sh: editing the *plist* still needs
# install.sh, which boots the job out and bootstraps the new definition.
set -euo pipefail
launchctl kickstart -k "gui/$(id -u)/local.wren.wren"
echo "restarted local.wren.wren"
