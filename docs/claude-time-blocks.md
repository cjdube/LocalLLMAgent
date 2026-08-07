# Claude Code time blocks — how it works

A daily unattended task that reconstructs yesterday's Claude Code working hours and
logs them to Google Calendar, so the calendar is a record of how the day actually
went without anyone remembering to block it out after the fact.

Code: `tasks/claude_time_blocks.py` (the task), `tasks/_chat_transcripts.py`
(`fetch_session_activity`, the reader it shares with `ai_chat_learnings`).

It is a **companion** to [ai-chat-learnings](ai-chat-learnings.md), not part of it:
the learnings review — *what* was accomplished — is worth having whether or not you
care *when* it happened. The two share the transcript reader and nothing else, and
either can be scheduled without the other.

## Where the hours come from

Claude Code timestamps every event it writes to
`~/.claude/projects/<slug>/<uuid>.jsonl`, so the day is already on disk. The task
only has to decide where one stretch of work ends and the next begins.

`fetch_session_activity` returns one entry per timestamped event —
`{ts, project, slug, session, text}` — across every session file with activity in
the window, oldest first, in local time. Unlike `fetch_claude_sessions`, it keeps
records the learnings task drops (tool results, subagent sidechains, meta): an agent
grinding through tools for twenty minutes with nothing said out loud is still time at
the keyboard. `text` carries the human/assistant text and is `None` for those
records, so the blurb prompt can still be built from what was actually said.

## One timeline, not one per session

Every session's events are pooled into a **single** timeline and split on idle gaps.
The obvious alternative — one calendar event per session — does not work, and the
real data is why:

| Day | Session-days | Sum of their spans | Actually worked |
|---|---|---|---|
| Aug 3, 2026 | 8 | ~19 h | ~5 h |

Sessions overlap constantly: a long-running one in the foreground, a second in
another repo, a quick third to check something. Per-session events would triple-book
the day. Pooling makes the result non-overlapping by construction, and a block that
spans several repos simply names them all.

An idle session logs nothing at all, so **silence is the only signal** that the user
stepped away — which is what makes gap-splitting the right primitive.

## The two knobs

- `WREN_SESSION_BLOCK_GAP_MINUTES` (default **20**) — the idle gap that ends a
  block. Tuned against six real days: 10 minutes fragments a working morning into a
  dozen entries (Aug 3 → 11 blocks), 30 swallows a coffee break and an errand alike,
  20 reproduces the days as they were lived (2–6 blocks, 1–5 hours).
- `WREN_SESSION_BLOCK_MIN_MINUTES` (default **10**) — blocks shorter than this are
  dropped; a 90-second glance at something is not a calendar entry. Measured against
  the block's *raw* span, before the rounding below, so the floor means what it says.

Block edges are then rounded out to 5-minute boundaries (start down, end up), because
a block begins and ends on whichever event happened to be logged — a minute or so
inside the real stretch. The calendar reads `13:40–15:35`, not `13:41–15:31`.

## What the entry looks like

```
AI · LocalLLMAgent, ObsidianWikiAgent — implemented check_slug_typos linting rule
8:05 – 9:25 AM

  LocalLLMAgent · fix-the-slug-lint — 8:05 to 9:21 AM
  ObsidianWikiAgent · check-wiki-slugs — 9:09 to 9:21 AM

  Logged by Wren from Claude Code's local session logs.
```

Python owns the structure — the timeline, the rounding, the `AI · <projects> —`
prefix, and the per-session description lines with their exact spans and Claude
Code's own conversation slugs. The model writes only the phrase after the dash: one
bounded call per block (2–6 a day, ~2k prompt tokens each), `think=False`, capped at
60 characters. An empty or unusable response falls back to `working session` **and
logs a WARNING** — a block silently titled that would otherwise read as an ordinary
quiet day rather than a broken prompt.

Events are colored with the Work category's color (by *role*, so renaming the
category in `config/preferences.json` doesn't break this) and stamped with a
`source_id` of `claude-time:<date>:<HHMM>`, derived from the block's start.

## Idempotency, and why the colorizer leaves these alone

`log_calendar_event` looks up its `source_id` before inserting, so re-running a day —
or sweeping it up again in a `--backfill` — finds the event it already made instead
of duplicating it. The calendar is its own dedup record; there is no state file.

`calendar_colorizer` (5:00 PM) re-classifies *every* event it sees, including ones
colored by a previous run or by hand. Four hours earlier these blocks arrived
already colored, so it skips anything whose `source_id` starts with
`claude-time:` — scoped to that prefix on purpose, since Strava's events also carry
a `source_id` and should keep being classified. The prefix constant lives in
`agent/tools/calendar.py` next to `log_calendar_event`, so neither task imports the
other; `get_events_in_range` surfaces `source_id` on every event so the filter has
something to match.

## Running it by hand

```bash
.venv/bin/python -m tasks.claude_time_blocks --date 2026-08-05 --dry-run
```
```bash
.venv/bin/python -m tasks.claude_time_blocks --backfill 7
```

`--dry-run` still calls the model (the titles are the part worth checking) but never
touches the calendar. Only the plain daily run pushes a "3 blocks, 4.7h logged"
summary to the phone; `--date` and `--backfill` are targeted re-runs and stay quiet.

**Backfilling over days already blocked out by hand will double up.** The dedup key
only knows about events this task created — a manually-written "AI - LogViewer
capabilities" is invisible to it. Dry-run first and check the days you're about to
cover.

## Safety and privacy

Transcript text is untrusted input (it contains web and tool output that may carry
prompt injection); the task only reads local files and writes a calendar event, and
the blurb prompt treats transcript content as data, not instructions. It runs on the
local model by design — `WREN_CLAUDE_TIME_BLOCKS_BACKEND` can opt into the cloud, but
that sends transcript text off-device.
