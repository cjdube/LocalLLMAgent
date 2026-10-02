# LLM backend selection

Wren's model calls go through a single seam — `agent/loop.py:_llm_chat` — which
dispatches to a **backend**. The default is local Ollama (the local-first
design); a cloud backend (currently Gemini) is opt-in. Everything above the seam
(`advance`, `complete_text`, and all their callers) speaks one canonical
message/tool shape (the Ollama/OpenAI shape); each backend translates to and
from its provider format *inside itself*, so selecting a backend changes nothing
else in the codebase.

## The privacy tradeoff — read this first

The whole point of Wren is local-first: with the default Ollama backend, nothing
about your day leaves the Mac mini at runtime. **Selecting a cloud backend sends
that task's tool results and conversation history to the provider** (Google, for
Gemini) — calendar entries, email bodies, browsing history, opportunity leads,
whatever the task feeds the model. That's the opposite of the default posture,
so switch deliberately, and prefer the per-task override below over a global
flip so you only send what you mean to.

## Selecting a backend

Two environment variables in `config/.env`, resolved as
**explicit per-task override → global default → `ollama`**:

- `WREN_LLM_BACKEND` — the global default for every model call. Unset (or
  `ollama`) keeps everything local. Set to `gemini` to route *all* calls
  (including chat and the background worker) to the cloud.
- `WREN_<TASK_KEY>_BACKEND` — overrides the global for one task only. This is the
  recommended way to use the cloud: leave the global on `ollama` so chat and
  `bg_worker` stay local, and point just the heavy scheduled summarizers at the
  cloud, where quality matters most and latency is invisible.

`<TASK_KEY>` is the uppercased task/module name. Wired task keys:
`DAILY_SYNTHESIS`, `OPPORTUNITY_DIGEST`, `MORNING_BRIEF`, `STARRED_BLURBS`,
`PROJECT_SCAN`, `RESEARCH`, `EVALUATE_APP`, `EVALUATE_AGAINST`. (The journaling
keys left with ScribeJay — see its own `docs/llm-backend.md`.)

(The list is the set of `resolve_backend("<key>")` call sites — `grep -rn
'resolve_backend(' agent tasks` is the check when this drifts.)

Only these keys do anything: `resolve_backend` builds the variable name from the
key its caller passes, so a `WREN_<ANYTHING>_BACKEND` that doesn't match a call
site above is silently inert rather than an error.

```
# chat + bg_worker stay local; only the daily synthesis goes to the cloud
WREN_LLM_BACKEND=ollama
WREN_DAILY_SYNTHESIS_BACKEND=gemini
```

## ScribeJay has its own chain, in its own repo

The journaling agent lives in the sibling ScribeJay checkout now and resolves its
backend through `SCRIBEJAY_<TASK_KEY>_BACKEND -> SCRIBEJAY_LLM_BACKEND -> ollama`,
with deliberately **no** fallback to `WREN_*`. Both halves are documented there,
in ScribeJay's own `docs/llm-backend.md`; nothing in this repo reads those
variables, so setting one here does nothing.

`chat/server.py` and `tasks/bg_worker.py` deliberately have no per-task wiring —
they follow the global default. `bg_worker` ingests untrusted web/search content,
so it's the sharpest place where private-data egress meets prompt injection;
pointing it at a cloud backend (by setting the global) is the riskiest switch.

## Applying a change

`config/.env` is read only at process start, so an edit takes effect when each
consumer next starts — there's nothing to reload live:

- **Chat server** — restart it so it re-reads `.env`. It runs under launchd, so:
  `launchctl kickstart -k gui/$(id -u)/local.wren.wren`
  (each scheduled task is a separate launchd job that loads `.env` fresh on every
  run, so tasks need no restart — the next run picks up the change.)
- **Scheduled tasks** — nothing to do; the change applies on the task's next run.
  To exercise it immediately, run the task by hand, e.g.
  `python -m tasks.daily_synthesis`.
- **Verify** — the dashboard's identity line shows the active chat backend
  (e.g. `gemini-2.5-flash (gemini)`); task logs under `logs/` show a
  `gemini_chat model=…` line when a task ran on the cloud backend.

## Gemini config

- Key: `GEMINI_API_KEY` or `GOOGLE_API_KEY` (the SDK checks both).
- `WREN_GEMINI_MODEL` — model id, defaults to `gemini-2.5-flash`.
- `WREN_GEMINI_MAX_OUTPUT_TOKENS` — per-call generation cap, defaults to 8192.
- `WREN_GEMINI_THINKING_BUDGET` — thinking tokens, defaults to `0` in code but
  set to `128` in `.env.example`. Thinking counts against the output cap, so a
  large budget returns *empty* content rather than a short answer. `0` is the
  right value and works on 2.5-flash and 3.7-flash, but 2.5-pro and 3.6-flash
  reject it with a bare 400 `INVALID_ARGUMENT`; `128` (or `-1` for dynamic) is
  accepted everywhere. Verify with one real call after changing
  `WREN_GEMINI_MODEL`.
- Dependency: `google-genai` (pinned in `requirements.txt`), imported lazily so a
  purely local install never loads it.

## What the cloud path skips

The small-local-model accommodations are Ollama-only and don't apply to the cloud
path: `warm_model` is a no-op (nothing to pre-load), and the `OLLAMA_NUM_CTX` /
`num_predict` / `keep_alive` options aren't sent. The lazy tool-loading system
(`agent/toolset.py`) still applies to chat because chat still defaults to local;
it would be redundant if you ever went cloud-only.

## Adding another provider (e.g. Claude)

Add a `_<provider>_chat` function with the same signature and canonical return
shape as `_ollama_chat`/`_gemini_chat`, a `_<provider>_client` construction
choke point (so `tests/conftest.py` can blanket-block it), and a branch in
`_llm_chat`. Translate messages/tools/streaming inside the new function; don't
touch the callers.
