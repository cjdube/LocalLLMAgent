# Lazy tool loading — how it works

Wren has more than forty tools. Sending every schema to the local model on every chat turn
— plus a prompt paragraph describing each — wastes context the small model
can't spare, for no benefit on the common asks ("what's the weather?"). So chat
sessions use progressive disclosure: a small always-loaded **core** plus
deferred **groups** pulled in on demand.

This applies to **chat only**. The background worker (`tasks/bg_worker.py`) and
the dashboard still use the whole `TOOLS` registry — a background job is one
self-contained task run unattended, with no user to recover from a missed load.

Code: the split and helpers live in `agent/toolset.py`; the per-session wiring
lives in `chat/server.py`.

## Core vs. groups

- **Core** (`CORE_TOOLS`) — always sent: weather, calendar (read + log +
  recolor), Google Tasks, web search, memory (remember/pin/recall/recategorize/
  archive/forget), reminders, the scheduled-task list (`list_scheduled_tasks`),
  and skills **read** (the skills index is rendered into the prompt every turn
  and tells the model to `read_skill`). Plus the `load_tools` meta-tool itself.
- **Groups** (`TOOL_GROUPS`) — loaded on demand:
  - `opportunities` — fractional-work scout, watchlist, company research
  - `wiki` — the learnings wiki
  - `background` — hand a long task off to run detached
  - `web` — fetch a page, evaluate an app, evaluate a target against a wiki
    lens, list starred repos
  - `authoring` — write/delete a skill
  - `brief` — send the morning brief, or send an email
  - `games` — the games he can play, and the link to open one
  - `projects` — his local checkouts: what each is, and how recently he
    touched it

Every tool in `TOOLS` is in exactly one of core or a group — enforced by
`tests/test_toolset.py::test_core_and_groups_partition_the_registry`, so adding
a new tool without slotting it makes that test fail rather than silently making
the tool unreachable in chat.

That test partitions `TOOLS`, so it cannot see the *other* way a tool goes
missing: a schema written at the model but never added to `TOOLS` at all, which
never enters the partition. `fetch_liked_videos` sat that way from `5c40332`
until 2026-08-03 — a full model-facing schema in `agent/tools/youtube.py`, used
only as a scheduled-task data source, unreachable in chat and invisible to every
test. `test_every_model_facing_schema_is_registered` now walks `agent/tools/`
and asserts each `*_SCHEMA` is either in `TOOLS` or named in the (currently
empty) `TASK_ONLY_SCHEMAS` allowlist.

The `activity` group — `fetch_strava`, `fetch_chrome_history`,
`fetch_liked_videos` — and `recolor_event` left the registry entirely when
journaling moved to ScribeJay ([scribejay.md](scribejay.md)): capture is ScribeJay's job,
and Wren reads what it wrote through the calendar and the wiki instead. Two of
those modules — `chrome_history` and `youtube` — stay in `agent/tools/` as plain
libraries with no `TOOL_SCHEMA`, because `tasks/daily_synthesis.py` still calls
them; `strava` had no second caller and left with ScribeJay. The two that stayed are
guarded by `test_scribejay_capture_modules_expose_no_tool_schema` and
`test_capture_tools_are_absent_from_wrens_registry`, so re-adding a schema there
without registering it fails loudly rather than sitting unreachable for months
the way `fetch_liked_videos` did.

## How a group gets loaded (two paths)

1. **Keyword pre-load (deterministic, primary).** On each user message the
   server runs `groups_for_message()` — a word-boundary match against
   `GROUP_KEYWORDS` — and attaches any cued groups *before* the model runs. So
   "what's on my opportunities watchlist?" loads `opportunities` with no
   reasoning hop. This is the main reliability lever: the small model usually
   never has to decide to load anything. Cues are tunable data in
   `agent/toolset.py`.
2. **`load_tools(group)` (model fallback).** For asks the keywords miss, the
   `render_toolgroups_index()` block in the system prompt lists the groups and
   when to load each, and the model calls `load_tools`. The bound callable
   (`chat/server.py:_make_load_tools`) both records the group on the session and
   **extends that turn's live tools list in place** — `advance()` re-sends the
   same list object each iteration, so the group's tools are callable on the
   model's very next step, within the same turn.

## Session state

`loaded_groups[sid]` (a `set`, in-memory like `conversations`) persists a
session's activated groups across turns and across the `/chat/confirm`
continuation (which rebuilds the toolset), so a group loaded mid-turn before a
confirm-gated write is still present after approval. It's cleared on
`/chat/new` and on idle-session eviction.

The full `DISPATCH` is always available to execute — only the *schemas* the
model sees are gated — so a tool is never un-dispatchable; the model just isn't
told about it until its group loads.
