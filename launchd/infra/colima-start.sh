#!/bin/sh
# Start colima, self-healing past the wreckage an unclean shutdown leaves behind.
#
# Why this exists — the July 2026 outage, in full:
#   Jul 11 21:43:07  The Mac began rebooting. On SIGTERM colima's VM hit
#                    `VirtualMachineStateError` and lima's host agent died
#                    *fatally* ("vz: CanRequestStop is not supported") instead
#                    of shutting down cleanly, leaving stale pid/sock files.
#   Jul 11 21:48:14  Boot. Homebrew's LaunchAgent DID fire and run `colima
#                    start -f`. It failed on that stale state:
#                    "errors inspecting instance: [vz driver is running but
#                    host agent is not]" -> exit 1.
#   ...and that was it. Homebrew's plist sets KeepAlive.SuccessfulExit=true,
#   which tells launchd to relaunch ONLY after a *clean* exit. colima exited 1,
#   so launchd correctly did nothing. ntfy — and every Wren push: task-failure
#   alerts, reminders — stayed down for 4 days. Nobody noticed, because nothing
#   happened to need pushing.
#
# Two things were needed, and neither alone is enough:
#   1. Retry on failure (the plist beside this file: KeepAlive true).
#   2. This script. A bare retry would have re-run the same failing command
#      forever — the stale state does not clear itself.
#
# Both stops below are no-ops on an already-stopped instance, so this is safe to
# run unconditionally before every start.
set -u

PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin"
export PATH

# Graceful first. launchd only runs this when colima isn't supposed to be up,
# but the VM can outlive its foreground process — the lima host agent is a
# separate process — and hard-killing a live VM risks the ntfy databases: they
# live on the host (~/ntfy-server) and are bind-mounted through the guest, so an
# in-flight write can be torn.
colima stop >/dev/null 2>&1 || true
# Then forced, to sweep up the stale pid/sock files a *crashed* instance leaves
# behind. This is the step that fixes the Jul 11 failure — the graceful stop
# can't, because by then there's no live VM left to ask nicely.
colima stop --force >/dev/null 2>&1 || true

# exec: colima start -f runs in the foreground and BECOMES the service process,
# so launchd watches colima itself rather than this wrapper's shell.
exec colima start -f
