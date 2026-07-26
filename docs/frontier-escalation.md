# Manual frontier escalation

## What it is

A chat affordance that lets you re-run the turn you just saw on an opt-in
**frontier** backend, by hand, when you judge the local model's answer too weak.
**You are the router** — there is no classifier and no automatic detection. Every
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
| `local_reply` | the local model's rejected reply (the paired half of the dataset) |
| `prompt_tokens` | approximate size shipped off-device |
| `backend` | resolved frontier backend (e.g. `gemini`, `anthropic`) |
| `model` | resolved frontier model id |
| `outcome` | `ok` \| `error:<reason>` |

Per repo convention, the store lands with **an explicit `.gitignore` line and a
`tests/conftest.py` redirect in the same commit** — otherwise it commits runtime
data and tests clobber the production file.

**No cost/budget cap in v1.** Escalation is manual — one deliberate tap each — so
*you are the rate limiter*. Token counts are logged so spend is eyeball-able; a
cap guards against an autonomous spender that doesn't exist here. Add one only if
real usage ever surprises you.

## Where it lives

- `agent/loop.py` — `escalation_backend()` (reads `WREN_ESCALATION_BACKEND`) and
  `escalation_available()` (backend set *and* provider credentials present).
- `agent/escalations.py` — the `config/escalations.json` store (`record_escalation`).
- `chat/server.py` — the `/chat/escalate` route; the confirm continuation threads
  the escalated turn's backend through `_run_turn` via `pending_backends` so a
  paused write resumes on the frontier model.
- `chat/static/chat-dock.js` — the redo button and ⚡ badge on `/chat`.

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
