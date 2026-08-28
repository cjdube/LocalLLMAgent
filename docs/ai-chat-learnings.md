# AI chat learnings — how it works

A daily unattended task that reviews the prior day's chats with AI agents and
writes a brief **Accomplished / Learned** summary of each into the user's Obsidian
vault — outcomes and takeaways, not the back-and-forth. It follows the same
gather → compact → local model → persist shape as the other daily learnings
tasks (`daily_chrome_learnings`, `daily_youtube_learnings`).

Code: `scribejay/ai_chat_learnings.py` (the task), `scribejay/transcripts.py`
(reading + compacting the three sources).

## Why there's no "past chats" API

There is no supported API used here to fetch consumer chat history. Claude Code
and Codex Desktop leave local session logs; Gemini needs a manual export. This
capability uses only what lands on disk locally, which keeps it ToS-clean and
local-first. Codex's JSONL layout is an observed private format rather than a
documented API, so its reader warns and skips when the expected shape drifts.

## The three sources

- **Claude Code sessions** — Claude Code writes every session to
  `<CLAUDE_CONFIG_DIR>/projects/<slug>/<uuid>.jsonl` as an append-only log of
  JSON events (`CLAUDE_CONFIG_DIR` defaults to `~/.claude`).
  For a given day, `fetch_claude_sessions` reads every session with activity that
  day (across all projects), keeping only role=user/assistant **text** — tool
  calls, tool results, subagent sidechains, thinking blocks, and injected
  `<system-reminder>` spans are all dropped. A session spanning several days is
  summarized once per day it was active ("new or revisited that day"), carrying
  only that day's turns. An append-only-log mtime prefilter skips files last
  written before the day began, so a 14-day backfill doesn't re-parse the whole
  history 14 times.
- **Codex Desktop sessions** — Codex writes JSONL logs under
  `<CODEX_HOME>/sessions/YYYY/MM/DD/` (`CODEX_HOME` defaults to `~/.codex`).
  `fetch_codex_sessions` keeps only top-level tasks the user directly started
  (`session_meta.payload.thread_source == "user"`). Imported chat history,
  onboarding, guardian reviews, and subagents are skipped before their bodies
  are read. This matters because Codex can
  [import and sync recent work from other agents](https://learn.chatgpt.com/codex/import);
  treating every local file as a Codex chat would duplicate that material.
  Within a kept task, only user `input_text` and assistant
  `commentary`/`final_answer` `output_text` are retained. Developer instructions,
  injected environment/plugin/task context, reasoning, tools, and events are
  dropped. Codex CLI is not covered by this v1 reader.
- **Gemini drop folder** (`WREN_GEMINI_CHATS_DIR`, default
  `<vault>/gemini_inbox`) — Gemini has no local footprint, so drop an exported
  conversation (`.md`/`.txt`/`.json`, one file per chat) into this folder and it
  gets summarized once. `config/ai_chat_learnings_state.json` records processed
  filenames (by mtime) so re-runs never re-summarize the same file; files are
  never modified or deleted. Only the normal "yesterday" run reads the folder —
  backfill runs skip it (a drop file has no reliable per-day date).

## Output

One file per day, `AI-Chat-Learnings-<date>.md`, written to `LEARNINGS_DIR` (the
vault's `raw/`), with one section per session:

```
## AI Chat Learnings: July 12, 2026

### Claude · LocalLLMAgent · i-d-like-to-explore-cozy-minsky · 4:34 AM
**Accomplished**
- ...
**Learned**
- ...

### Codex · LocalLLMAgent · 11:12 AM
**Accomplished**
- ...
**Learned**
- ...

### Gemini · <filename>
...
```

Python owns the day math, chronological ordering across Claude and Codex,
section headers (project from `cwd`, Claude's slug when present, and start time),
and file assembly; the model only writes the bullets, against a fixed template.
Each session is one bounded model call (`AI_CHAT_LEARNINGS_MAX_CHARS`, default
12000 chars, head+tail if longer) — a small, focused prompt the on-device model
handles reliably.

A day with no chats, or where every summary came back "None", writes nothing
(keeps the vault clean). If the vault write fails — e.g. `LEARNINGS_DIR` points
somewhere that doesn't exist — the draft is emailed and a phone alert pushed, so
an entry is never silently lost (same contract as the other learnings tasks).

## Backfilling

```bash
.venv/bin/python -m scribejay.ai_chat_learnings                    # yesterday (all sources)
.venv/bin/python -m scribejay.ai_chat_learnings --backfill 14      # each of the last 14 days, one process
.venv/bin/python -m scribejay.ai_chat_learnings --date 2026-06-29  # one day (Claude + Codex)
```

`--backfill N` runs the per-day logic once for each of the last N days (Claude
and Codex sessions), oldest first, each writing its own dated file. Gemini is
skipped because a drop file has no reliable per-day date. Model calls are
sequential, never parallel. To be even gentler on the on-device model, backfill
day-by-day with `--date` in separate processes; both write the same idempotent
`AI-Chat-Learnings-<date>.md`.

## Safety

Transcript text is untrusted input — it contains web/tool output that may carry
prompt injection. The task only reads local files and writes markdown (no gated
or consequential tools), and the summarizer treats transcript content as data,
not instructions. Runs on the local model by design; `SCRIBEJAY_AI_CHAT_LEARNINGS_BACKEND`
can opt into the cloud, but that would send transcript text off-device.
