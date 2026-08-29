# Module map

Where each part of Wren lives, and why it is shaped that way. `AGENTS.md`
carries the short version — the rules that bind a change. This file carries the
per-module detail behind them.

## The chat server (`chat/`)

- `chat/server.py` — Flask chat server (phone-reachable UI + API); pauses on gated writes for tap-to-confirm. Every feature API is a Flask blueprint (`chat/routes_dashboard.py`, `chat/routes_opportunities.py`, `chat/routes_starred.py`, `chat/routes_games.py`, `chat/routes_logs.py`, `chat/routes_wiki.py`) — only the conversation engine, auth, and the static view pages live here; `LoginThrottle` and the shared `_authenticated` check are in `chat/login_throttle.py` / `chat/auth.py`. New API routes get a blueprint, not another route in this file. A new **page** is one entry in the `VIEW_PAGES` table, never another hand-written handler — nine copies of the same four lines is what made this the rule that kept getting bent.
- `chat/routes_games.py` — the `/games` blueprint: the games JSON API, each hosted game's built bundle served off disk, and a proxy for its loopback AI service. Games are mounted under Wren's origin so they inherit her token instead of opening a second, unauthenticated listener on the tailnet; the proxy's timeouts must stay above the browser's own budgets. Registry in `agent/tools/games.py`. [games.md](games.md)
- `chat/routes_starred.py` — the `/starred` blueprint. Everything on that page is a nightly-cached store read except the repo list, which is live — and falls back to its own cache when GitHub is slow or rate-limiting, so a paginated fetch can't blank a page three caches could already fill. [starred.md](starred.md)
- `chat/routes_wiki.py` — the `/wiki` blueprint: the learnings-vault graph (`chat/wikigraph.py`), the structural lint (`chat/wikilint.py`), and single-page reads. The lint is a **subprocess** into the sibling ObsidianWikiAgent checkout, never an import — both repos have a top-level `agent` package. That subprocess must stay on `--json`, which logs nothing: the sibling's own log is what this dashboard parses for that job's run history, so a button press that logged would fabricate a scheduled run. Exit code 1 there means *findings exist*, not failure. [wiki-graph.md](wiki-graph.md), [wiki-lint.md](wiki-lint.md)
- `chat/insights.py` — the chat server's no-Flask dashboard data layer: launchd-schedule discovery, run-log parsing, capabilities, and the `/map` system map. Standalone-runnable and unit-testable.
- `chat/logview.py` — the `/logs` viewer's reader (blueprint in `chat/routes_logs.py`, rendering in `chat/static/log-view.js`). Reads any file under `logs/` as folded entries over a bounded 512KB window, because launchd's `.launchd.log` stdout captures are append-only and rotated by nothing. Answers "show me this file" where insights answers "did this job work"; it's the only way to read the chat server's own log. [logs.md](logs.md)

## The agent (`agent/`)

- `agent/loop.py` — model interface: `advance()` (tool-calling loop), `complete_text()` (one-shot, tool-free — what scheduled tasks use). Both route through the `_llm_chat` backend seam; the default `_ollama_chat` lives here, the opt-in Gemini backend in `agent/backends/gemini.py`.
- `agent/backends/` — LLM backend adapters behind the `_llm_chat` seam (currently `gemini.py`); each translates the canonical message/tool shape to/from its provider, so callers speak one shape. [llm-backend.md](llm-backend.md)
- `agent/toolset.py` — the single tool registry (`TOOLS`, `DISPATCH`) and gating sets (`WRITE_TOOLS`, `CONSEQUENTIAL_TOOLS`, `UNATTENDED_EXCLUDED_TOOLS`), shared by the chat server and background runs. [tool-loading.md](tool-loading.md)
- `agent/tools/*.py` — one module per capability (weather, strava, email, notify, wiki, skills, …).
- `agent/tools/sports.py` — final scores from ESPN's public scoreboard endpoint for the teams in `sports.teams` ([preferences.md](preferences.md#sports)); feeds the morning brief's Scores section and the `fetch_scores` chat tool. Its docstring records why ESPN's undocumented endpoint was chosen over MLB's official one, and why events are never re-filtered by their own UTC date.
- `agent/store.py` — locked/atomic JSON store primitives used by every store under `config/`.

## The journaling agent (`scribejay/`)

- `scribejay/*.py` — ScribeJay, the second agent in this repo. It keeps the record —
  Strava onto the calendar, AI Session Time Blocks, yesterday's events
  colour-coded, daily vault pages from Chrome/YouTube/AI chats — while Wren reads
  that record and acts on request. Each task is a pipeline: gather -> one
  `complete_text()` call -> write. No tool registry, and never `advance()`.
- `scribejay/model.py` — ScribeJay's own backend chain (`SCRIBEJAY_<TASK>_BACKEND` ->
  `SCRIBEJAY_LLM_BACKEND` -> ollama), independent of Wren's `WREN_*` variables on
  purpose. Every run logs which it resolved to, because an unset variable is not
  an error — just a smaller model and a thinner draft.
- **Import boundary.** `scribejay/` imports only the porch listed in
  `scribejay/__init__.py`, and nothing under `agent/`, `chat/` or `tasks/` may
  import `scribejay.*` (`evals/` excepted). That list is what would have to travel
  with ScribeJay if it is ever extracted into its own repo. [scribejay.md](scribejay.md)

## Scheduling and state

- `tasks/*.py` — unattended entrypoints run by launchd; `tasks/_common.py` has `setup_logger`/`notify_failure`, and `agent/activity_log.py` the shared gather→persist→email helpers for the two daily-learnings tasks. `tasks/bg_worker.py` is the generic runner that polls `config/bg_jobs.json` for user-initiated background jobs with push-to-approve.
- `tasks/clickup_watcher.py` — turns a `wren-research` or `wren-context` tag on a ClickUp Task into a background job every five minutes. **Never calls the model**: one GET plus a per-tag Python template, because Ollama's single slot means a polling model call starves chat. The tag is removed *before* the job is queued, which is what stops one Task being handled twice. Its two collaborators (`clickup.tagged_clickup_tasks`, `clickup.remove_clickup_tag`) are the only functions in `agent/tools/clickup.py` that take a ClickUp id, and are deliberately not chat tools. [clickup.md](clickup.md#the-tag-watcher)
- `tasks/mail_watcher.py` — the **second** always-on process on the mini, alongside the chat server. It holds a Pub/Sub streaming-pull subscription open rather than polling, which is what buys seconds of latency on new mail without opening a port. Its Pub/Sub client runs callbacks on background threads, so it is autouse-stubbed in `tests/conftest.py` and tested through `handle_notification()`. State (history watermark, dedupe set, watch expiry) is in `agent/tools/mail_state.py`, and the reads in `agent/tools/gmail_read.py`. [mail-watch.md](mail-watch.md)
- `launchd/` — the scheduler: one plist per task (`StartInterval` for pollers, `StartCalendarInterval` for daily/weekly jobs), logging to `logs/`. Wren's jobs are labelled `local.wren.*`, ScribeJay's `local.scribejay.*`; `launchd/reload-after-upgrade.sh` globs both prefixes, since both exec the same `.venv` interpreter.
- `tests/` — flat pytest suite, one `test_<module>.py` per source module — except where a module sits behind a seam and is tested through it (`agent/backends/gemini.py` in `tests/test_loop.py`). Plus the jest/jsdom suites (`npm test`), also flat in `tests/` — one `tests/<script>.test.js` per standalone browser script in `chat/static/`.
- `config/` — `.env` (documented in `.env.example`) plus gitignored JSON stores.

## Related

- [scribejay.md](scribejay.md) — the journaling agent, the seam, and the porch
- [tool-loading.md](tool-loading.md) — how tools reach the model's context
- [limits.md](limits.md) — where every bound is defined
- [security-model.md](security-model.md) — the trust boundaries these modules sit inside
