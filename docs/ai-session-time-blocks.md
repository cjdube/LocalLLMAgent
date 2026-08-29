# AI Session Time Blocks — how it works

A daily unattended task that reconstructs yesterday's Claude Code and Codex Desktop
working hours and logs them to Google Calendar, so the calendar records how the day
actually went without anyone remembering to block it out after the fact.

Code: `scribejay/claude_time_blocks.py` (the compatibility-preserved task),
`scribejay/transcripts.py` (`fetch_session_activity` and
`fetch_codex_session_activity`, shared with `ai_chat_learnings`).

It is a **companion** to [ai-chat-learnings](ai-chat-learnings.md), not part of it:
the learnings review — *what* was accomplished — is worth having whether or not you
care *when* it happened. The two share the transcript reader and nothing else, and
either can be scheduled without the other.

## Where the hours come from

Claude Code timestamps every event it writes to
`<CLAUDE_CONFIG_DIR>/projects/<slug>/<uuid>.jsonl`, so the day is already on
disk (`CLAUDE_CONFIG_DIR` defaults to `~/.claude`). Codex Desktop likewise writes
timestamped JSONL under `${CODEX_HOME:-~/.codex}/sessions`.

`fetch_session_activity` returns one entry per timestamped event —
`{ts, project, slug, session, text}` — across Claude sessions. The Codex reader
returns the same shape, with an empty slug. Both readers keep timestamped tool,
reasoning, event, and metadata records the learnings task drops: an agent grinding
through tools for twenty minutes with nothing said out loud is still work. `text`
is populated only for visible user/assistant conversation, so private reasoning,
tool traffic, and injected context never enter the blurb prompt.

Only Codex Desktop tasks whose first `session_meta` record says
`thread_source == "user"` are accepted. Imported chats, onboarding, guardians,
and subagents are rejected before their transcript bodies are read. That boundary
matters because Codex can [import work from other agents](https://learn.chatgpt.com/docs/import),
which would otherwise duplicate activity already represented by its original source.
Codex JSONL is a private observed format, not a stable public API; suspicious schema
changes warn and degrade instead of failing the whole daily run.

## One timeline, not one per session

Every Claude and Codex session's events are pooled into a **single** timeline and
split on idle gaps.
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

  Claude · LocalLLMAgent · fix-the-slug-lint — 8:05 to 9:21 AM
  Codex · ObsidianWikiAgent — 9:09 to 9:21 AM

  Logged by ScribeJay from local Claude Code and Codex Desktop session logs.
```

Python owns the structure — the timeline, the rounding, the `AI · <projects> —`
prefix, and the agent/session description lines with their exact spans. Claude's
conversation slug is included where available; Codex has none. The model writes
only the phrase after the dash: one
bounded call per block (2–6 a day, ~2k prompt tokens each), `think=False`, capped at
60 characters. An empty or unusable response falls back to `working session` **and
logs a WARNING** — a block silently titled that would otherwise read as an ordinary
quiet day rather than a broken prompt.

Events are colored with the Work category's color (by *role*, so renaming the
category in `config/preferences.json` doesn't break this) and stamped with a
`source_id` of `claude-time:<date>:<HHMM>`, derived from the block's start. The
prefix is a retained legacy identifier, not a claim that the event is Claude-only.

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
.venv/bin/python -m scribejay.claude_time_blocks --date 2026-08-05 --dry-run
```
```bash
.venv/bin/python -m scribejay.claude_time_blocks --backfill 7
```

`--dry-run` still calls the model (the titles are the part worth checking) but never
touches the calendar. A successful run is silent — the calendar entries are the
record. Only a failure pushes to the phone.

This rollout is **forward-only**. It does not rewrite existing Claude-only calendar
events. Do not backfill a day that already contains generated `claude-time:` blocks:
adding Codex activity can change the combined block boundaries, and the legacy
start-time dedup key cannot reconcile the old and new shapes. Backfill remains safe
for days this task has never generated. Events blocked out manually are likewise
invisible to the dedup key, so dry-run first.

## Safety and privacy

Transcript text is untrusted input (it contains web and tool output that may carry
prompt injection); the task only reads local files and writes a calendar event, and
the blurb prompt treats transcript content as data, not instructions. It runs on the
local model by design. `SCRIBEJAY_CLAUDE_TIME_BLOCKS_BACKEND` is retained as the
legacy per-task override name; choosing a cloud backend sends transcript text from
both supported agents off-device.

## Compatibility names

The module, launchd label, log filename, task key, backend override, and
`claude-time:` prefix all predate Codex ingestion and remain unchanged. Keeping them
preserves dashboard history, launchd installation, model configuration, calendar
deduplication, and colorizer behavior while the user-facing capability is now named
**AI Session Time Blocks**.
