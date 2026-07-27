# Background tasks

Hand a multi-step job off to run detached from the chat turn; Wren works it and
pushes the user a summary when it's done. The chat tool `run_in_background`
(`agent/tools/background.py`) enqueues a job; the launchd poller
`tasks/bg_worker.py` runs one job per invocation and exits. State lives in
`config/bg_jobs.json`, written atomically under a cross-process file lock
(`agent/store.py`) so the chat server, the worker, and the approval endpoint
never clobber each other.

Starting a background task is itself confirmation-gated in chat
(`run_in_background` ∈ `toolset.WRITE_TOOLS`), so a job only runs because the user
tapped to start it.

## Execution posture — "A + push-to-approve"

A background run has no human present to confirm, so it can't be allowed to take
an irreversible action on its own. The worker **reads / researches / drafts
freely**, but the moment it wants a consequential, external action
(`toolset.CONSEQUENTIAL_TOOLS` — e.g. `send_email`, `send_morning_brief`) it
**pauses** the job and pushes the user a tap-to-approve notification. The next poll
resumes the job once he's decided. So untrusted content pulled mid-task can never
trigger an unattended irreversible action.

Tools that write **prompt-visible state** — `remember`/`pin`/`recategorize`/
`archive`/`forget` and `write_skill`/`delete_skill` — are removed from a
background run's toolset entirely (`toolset.UNATTENDED_EXCLUDED_TOOLS`), not just
approval-gated: pinned memories and skills feed *future* system prompts, so a
poisoned page read mid-job must not be able to plant a durable instruction that
outlives the job, even behind a tap. The read side (`recall`, `read_skill`)
stays available. The background-management tools themselves are excluded too — a
job spawning or polling jobs is never useful, and `run_in_background` would let a
job replicate.

Reversible, *internal* writes to the user's own account (creating a task, recoloring
an event) do run unattended — a deliberate, bounded exception. The whole policy
is two editable sets in `agent/toolset.py`: `CONSEQUENTIAL_TOOLS` (require
approval) and `UNATTENDED_EXCLUDED_TOOLS` (don't offer at all).

## Job lifecycle

```
pending ──(worker)──▶ done | failed
        └─(worker)──▶ awaiting_approval ──▶ approved | denied ──(worker)──▶ …
```

- **pending** — enqueued (or resumed after an approval); the next poll acts on it.
- **awaiting_approval** — the worker hit a consequential action and pushed for
  approval; the job's full conversation + the paused call are persisted.
- **approved / denied** — the user's decision; the next poll resumes from *after*
  the resolved call. The resolved state is persisted **before** the run
  continues, so a transient failure + retry can't execute an approved
  consequential action twice.
- **done / failed** — terminal; the result is pushed to the phone. Terminal jobs
  older than 14 days are pruned on the next write.

## Approval by phone

The approval push carries two ntfy action buttons (Approve / Deny) that POST to
`/api/bg/resolve` on the chat server. Each button URL embeds an **HMAC-signed,
time-limited token** (`agent/tools/background.py`, signed with `FLASK_SECRET_KEY`,
~1h expiry). The endpoint is token-authenticated rather than session-
authenticated — it's the one mutating route reachable without the login cookie,
because the phone's ntfy app calls it directly. Single-use falls out of the
state machine: `resolve_job` only acts on an `awaiting_approval` job, so a replay
finds nothing to do. A real transition is acknowledged with a confirming push
("Approved" / "Denied"), since the ntfy buttons have no selected state of their
own.

If the buttons expire (their token lifetime passed) or never rendered
(`WREN_PUBLIC_URL` was unset when the job paused), the worker **re-pushes** stale
`awaiting_approval` jobs once per token lifetime so a missed tap doesn't strand a
job forever. There's also a CLI fallback: `python -m agent.tools.background
--approve <id>` / `--deny <id>`.

## Failure handling

Transient errors — the model runner restarting, a network blip — don't
terminally fail a job. The worker leaves it actionable with a bumped attempt
counter and the next poll retries, marking it `failed` only after
`MAX_TRANSIENT_ATTEMPTS`. Transient classification covers both the Ollama path
(`requests` connection/timeout errors) and the Gemini path
(`google.genai` 5xx / httpx transport errors) when a task is cloud-routed.

**Crash caveat:** a job whose *process* is killed mid-run (not a caught error)
stays in its pre-run status and is retried from its last persisted point, so an
already-executed *auto* (reversible, internal) side effect could repeat.
Consequential actions are protected by the persist-before-continue boundary
above.

## Storage & config

- `config/bg_jobs.json` — the job store (pruned on write; capped list for the
  model's context).
- `WREN_PUBLIC_URL` — the base URL the ntfy approval buttons POST back to; unset
  means pushes still go, just without tap-to-approve buttons (CLI fallback then).
- `FLASK_SECRET_KEY` — signs the approval tokens (shared with the chat session
  cookie; no extra key to manage).
