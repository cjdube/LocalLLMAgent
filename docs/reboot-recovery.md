# Reboot recovery

launchd starts Wren's always-on jobs and interval pollers at login. Calendar
jobs retain their normal schedules; they are not normally launched merely because
the Mac restarted.

There is one exception: a Homebrew Python replacement can leave a calendar job
with launchd's `needs LWCR update` marker after it missed its scheduled window.
`launchd/reload-after-upgrade.sh` repairs the stale registration, then records
the missed labels in `config/startup_recovery.json`. It does not start them all
at once.

`local.wren.startuprecovery` polls that queue every minute. It starts one job at
a time, working through `POLICIES` in `tasks/startup_recovery.py`: the jobs you
would notice missing first (the morning brief, the Gmail watch renewal, the log
rollup, the weekly digest), then the rest. A task routed to local Ollama waits
until Ollama answers its health
probe; cloud-backed and deterministic tasks do not. A failed launch or task run
retries three times with increasing delays. During that recovery window, task
failure pushes are collected and replaced with one completion summary (including
any terminal failures), with the usual email fallback.

The queue is intentionally separate from normal scheduling. It is only a repair
path for work that launchd has already proved it missed; a healthy reboot does
not create catch-up work.

Only `local.wren.*` labels are covered. ScribeJay's eight jobs left this repo on
2026-08-30 and carry their own copy of `reload-after-upgrade.sh`, which repairs
their registrations but queues nothing — a missed journaling day is a gap in a
diary, not work to chase. See [scribejay.md](scribejay.md).
