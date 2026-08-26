# Manual frontier escalation

## What it is

A chat affordance that lets you run a turn on an opt-in **frontier** backend
instead of the local model. It has two triggers, and both end in you tapping a
button:

- **The answer was too weak** — re-run the turn you just saw. You judge it.
- **The local model can't answer now** — its one request slot is taken, so chat
  says so up front and offers the frontier model rather than queueing you
  silently behind a background job.

**You are the router.** Detection decides only whether to *offer*; it never
switches by itself, and there is no classifier judging the answer. Every
off-device trip is a conscious, visible, logged act.

It is a thin policy layer over machinery that already exists: `_llm_chat`
(`agent/loop.py`) already dispatches per-backend, and `advance()` already accepts
a backend. Escalation just re-runs the last turn with the configured frontier
backend.

## Provider-neutral by design

The escalation target is **not** hard-coded to any provider. `_llm_chat` branches
on a backend string; the cloud adapter that exists today is Gemini
(`agent/backends/gemini.py`), but adding another (Claude, etc.) is the
"Adding another provider" path in [docs/llm-backend.md](llm-backend.md) — one
`_<provider>_chat` function with the canonical message/tool shape plus one branch
in `_llm_chat`. Nothing in this feature assumes Google/Gemini.

Which backend the button escalates to is named by a dedicated env var:

- **`WREN_ESCALATION_BACKEND`** — the frontier backend for the escalation button
  (e.g. `gemini`, or `anthropic` once that adapter exists). Kept separate from
  `WREN_LLM_BACKEND` on purpose: the global default is `ollama` in a local-first
  setup, so it can't name a *frontier* target. Unset → the escalation button is
  simply not shown.

The button label, the reply badge, and the log record all read the resolved
provider/model from this var via `active_model_label()` (`agent/loop.py`), so the
UI reflects whatever frontier model is actually configured — never a hard-coded
name. Switching providers later is a config change (plus, for a brand-new
provider, its adapter), not an edit to this feature.

## The second trigger — a busy local model

Ollama serves one request at a time and queues the rest **silently**
([docs/ollama-serving.md](ollama-serving.md)). A chat turn sent while a
background job holds the slot simply hangs — on 2026-08-03 that meant three
hours of turns that looked like a connection failure against a perfectly healthy
Ollama.

So before committing a turn to the local model, `/chat` asks whether the slot is
free. `probe_local_model()` does it in two steps:

1. **Is our model resident?** (`/api/ps`, ~40µs.) If it isn't, report **free** —
   see below.
2. **Can it answer?** A throwaway one-token request with a 3s ceiling. A
   resident, idle model answers in ~0.05s, so silence means the slot is taken.

A taken slot answers `{"type": "busy"}` — a plain sentence naming what holds it,
plus two buttons: **Ask [frontier model]** and **Wait for Wren**.

### A cold model reports free, and is never probed

This step is not an optimization; it is what makes the probe correct. A model
that isn't loaded answers nothing until it has loaded — **measured 4.1s** here
with a warm page cache, and the ~50s in `CHAT_MODEL_TIMEOUT`'s note when it
isn't. Probing a cold Ollama therefore times out and reports "busy" when the
slot is in fact empty and the only wait is a load.

That false positive fires on the **first turn after any idle stretch**, which is
most first turns. It was caught in live verification, not by the tests, which is
exactly the shape [the live-model rule](model-constraints.md) warns about: the
suite monkeypatches the model, so it cannot see how long a real cold one takes.

Contention is the thing being detected. A cold start is a different problem and
is deliberately left alone — the turn loads the model and proceeds, as it always
did.

One trap this creates: Ollama reports the **resolved** tag, so a bare `gemma4` in
`.env` comes back from `/api/ps` as `gemma4:latest`. Comparing raw strings would
never match, every turn would read as a cold start, and the feature would be
silently dead. `_same_model()` normalizes the tag, and a test pins it.

### Why it has to ask BEFORE sending

This is the whole reason the feature is a probe rather than a timeout-and-bail.
**A queued turn cannot be cancelled.** The cancel check in `_ollama_chat` runs
between received chunks, and a queued request receives none — so a turn already
handed to a busy Ollama holds the session's one-turn slot until it times out,
and the escape hatch would 409 on the very button it just offered.

Asking first means nothing local has started, so there is nothing to cancel: the
session is left exactly as it was found, the user's message included.

### What it costs

~0.05s plus one `/api/ps` round trip per local chat turn, against a resident,
idle model (measured 2026-08-26 on `gemma4:26b-mlx`). Two things bound that
cost:

- The probe runs **only when a frontier backend is configured**. A local-only
  install has no offer to make, so it never pays.
- `WREN_CHAT_BUSY_PROBE=0` switches it off; `WREN_CHAT_BUSY_PROBE_TIMEOUT`
  (default 3s) sets the ceiling.

**The probe must send the same `num_ctx` as a real call.** Ollama keys the loaded
runner on its context length, so a probe carrying a different one would evict the
resident model and pay a ~17GB reload on *every* chat turn.
`test_probe_matches_the_real_calls_num_ctx` asserts it against `_ollama_chat`'s
own payload, so the two can't drift apart.

### Reading it in the log

Every probe writes one line, whichever way it goes:

```
local model probe: free, slot answered in 0.06s
local model probe: free, model not loaded in 0.01s
local model probe: busy (ReadTimeout) in 3.01s
```

The pass side is logged on purpose. Only the decline logged at first, which made
"is the check even running?" unanswerable from `logs/wren.log` — a healthy turn
and a switched-off probe looked identical, and the only way to see the feature
alive was to hold the slot by hand and watch a decline appear. One line per local
turn also makes the line count a turn count. The decline's full sentence still
comes from the route's `chat turn offered the frontier model:`.

### Both buttons matter

"Wait for Wren" is not a courtesy. The frontier model sends the conversation off
the Mac mini, and some questions are not for that. Waiting is what would have
happened anyway, and it re-sends with the probe skipped so it cannot loop back
into the same offer.

### The known race

The probe can pass and a background job start 50ms later. Then you wait, exactly
as before, and `_diagnose_stall` explains it. Deliberately not designed around: a
narrow window with an existing, honest fallback.

## Why manual-first (and what's deliberately *not* built)

The tempting version is an automatic router — a pre-classifier that predicts
whether the local model will cope, or mid-turn detection that notices it's
failing and escalates itself. Both are **deferred**, for two reasons:

- **A small model is worst at knowing what it can't do.** A pre-classifier run on
  the local model is the fox guarding the henhouse; a heuristic one is a guess.
- **We have no data on the real failure set.** The concrete failures on hand so
  far were *plumbing* (context overflow → repetition loops, since guarded by
  `num_ctx`/`num_predict`/`_trim_history`) or *code bugs*, not "the model is too
  small." Building an auto-router now means guessing at a trigger with zero data.

Manual escalation doubles as the instrument that fixes that: every escalation is
logged as a paired *(request, weak local answer)* record in
`config/escalations.json`. After a few weeks that store is a real dataset of
"things I felt the local model couldn't do" — which is what would justify or
design an automatic router later, keyed on evidence instead of a hunch. Revisit
the auto-router only if that store shows a patterned *reasoning-quality* failure
set (not a context-length one — see below).

Genuinely-plumbing items (num_ctx headroom, the deferred batched tool-call drop,
vault RAG) are separate work; a router was never their fix.

## The privacy tradeoff

Same posture as [docs/llm-backend.md](llm-backend.md): escalating **sends the
current conversation off-device** to whichever frontier provider is configured —
every prior turn in the session, its tool results, whatever's on screen. The
design keeps that honest rather than frictionless:

- **The button is the gate.** One deliberate tap, an honest label naming the
  destination (e.g. *"Redo with {frontier model} — sends this conversation
  off-device"*, filled from `active_model_label()`). No second confirm modal: a
  confirm card in front of an action *you* just chose is theater, and it erodes
  the reflex you rely on for the real `WRITE_TOOLS` gates (which interpose a human
  in front of an action the *model* chose). This is the opposite kind of action,
  so it gets the opposite treatment.
- **The reply is badged.** The frontier answer renders with a visible marker (⚡ +
  the resolved provider/model name) so a long session never blurs which answers
  came from off-device.
- **Every escalation is logged** (see the store below) — privacy audit trail and
  instrument in one.

## Behaviour

### Trigger — retry the last turn

The button attaches to the most recent assistant reply. Tapping it:

1. Drops the local model's rejected reply from the session history.
2. Re-runs the *same* user request through the **full `advance()` loop** with
   `backend=<WREN_ESCALATION_BACKEND>` — tools included, so escalation works for
   any request, including ones whose failure was tool choreography, not prose.
3. Renders the frontier answer with the ⚡ badge and appends it to the session
   normally (so subsequent local turns see it as context).

**Full history ships** with the retry (minus the rejected reply) — a
context-dependent request ("summarize *that* too") needs its referent, and
sending less would routinely produce a *worse* frontier answer than the local one
you were trying to improve on. This is safe under the one-tap gate precisely
because you're looking at the conversation you're sending.

### Safety gates are preserved

Because escalation goes through the normal `advance()` loop, the `WRITE_TOOLS` /
`CONSEQUENTIAL_TOOLS` confirmation gates **still fire regardless of backend** — if
the frontier model decides to send an email, you still get the tap-to-confirm
card. A separate text-only path was rejected partly because it would be a place
those gates could be silently bypassed.

### Failure modes

- **No frontier backend configured** → the button is not shown at all. Presence
  is gated on `WREN_ESCALATION_BACKEND` being set *and* that provider's
  credentials being present, checked at server start like the rest of `.env` (so
  config edits need a restart).
- **Live failure** (network, quota/429, provider error) → show the error plainly
  and **leave the local answer intact.** You escalated *hoping* for better; the
  fallback position is the answer you already had, not a blank.
- **No silent fallback to local.** A silent fallback would leave you thinking you
  got a frontier answer when you didn't — the exact "which model answered this?"
  ambiguity the ⚡ badge exists to kill.
- **No automatic retry on 429.** Retrying spends money and re-ships the payload.
  Surface it; let the human decide.

## The escalation store — `config/escalations.json`

Written via `agent/store.py` (`locked`/`atomic_write_json`), pruned on write to
the last N so it can't grow unbounded (polling-store convention). One record per
escalation:

| field | meaning |
|---|---|
| `ts` | ISO timestamp (UTC) |
| `request` | the user message that was re-run |
| `local_reply` | the local model's rejected reply (the paired half of the dataset); empty on a `busy` record, where the local model never answered |
| `prompt_tokens` | approximate size shipped off-device |
| `backend` | resolved frontier backend (e.g. `gemini`, `anthropic`) |
| `model` | resolved frontier model id |
| `outcome` | `ok` \| `error:<reason>` \| `cancelled` |
| `trigger` | `manual` (the redo button) \| `busy` (the local slot was taken) |

`trigger` is the field that keeps the dataset usable. A router argued for by
`manual` records would be routing on answer **quality**; one argued for by `busy`
records on **availability**. Those are different features, and a log that mixed
them would support neither.

Per repo convention, the store lands with **an explicit `.gitignore` line and a
`tests/conftest.py` redirect in the same commit** — otherwise it commits runtime
data and tests clobber the production file.

**No cost/budget cap in v1.** Escalation is manual — one deliberate tap each — so
*you are the rate limiter*. Token counts are logged so spend is eyeball-able; a
cap guards against an autonomous spender that doesn't exist here. Add one only if
real usage ever surprises you.

## Where it lives

- `agent/loop.py` — `escalation_backend()` (reads `WREN_ESCALATION_BACKEND`) and
  `escalation_available()` (backend set *and* provider credentials present);
  `probe_local_model()` (is the local slot free?) and `_probe_reason()` (which of
  busy / loading / down it is).
- `agent/escalations.py` — the `config/escalations.json` store (`record_escalation`).
- `chat/server.py` — the `/chat/escalate` route; the confirm continuation threads
  the escalated turn's backend through `_run_turn` via `pending_backends` so a
  paused write resumes on the frontier model. `/chat` carries the busy probe and
  its two follow-up flags (`backend: "frontier"`, `force_local`), and
  `_record_if_escalated` writes the log entry with the turn's real outcome.
- `chat/static/chat-dock.js` — the redo button, the ⚡ badge, and the busy offer
  on `/chat`.

## Adding another frontier provider (e.g. Claude)

The feature is provider-neutral, but only the Gemini adapter exists today. To
default escalation to a provider without one, first build its adapter per the
"Adding another provider" section of [docs/llm-backend.md](llm-backend.md), and
add a branch to `escalation_available()` and `active_model_label()` so the button
gates and the badge render correctly. Then point `WREN_ESCALATION_BACKEND` at it.

## Known caveat

The escalated turn goes through the same `advance()` loop as a local turn, so it
inherits the deferred batched tool-call drop ([discussed in the loop docstring](../agent/loop.py))
— harmless while the model calls tools one at a time.

## Testing notes

- New store → `tests/conftest.py` redirect to `tmp_path` in the same commit.
- No real network to any frontier provider in tests — monkeypatch the backend,
  per the suite-wide "monkeypatch all model/network collaborators" rule.
- `chat/static/chat-dock.js` gains the button + badge → covered by
  `chat-dock.test.js` (jest/jsdom); run `npm test`.
