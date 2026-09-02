# Security model / trust boundaries

The threat model here is deliberately small: a single user, a Tailscale-only
network surface, and a local model with no outbound API by default. Two
boundaries are worth stating explicitly — the **network** one (who can reach
Wren) and the **prompt-injection** one (what untrusted text can make her do).

## Network

The chat/dashboard server binds to `127.0.0.1` (override with `WREN_CHAT_HOST`).
`tailscale serve` reverse-proxies to that loopback address, so nothing needs to
listen on the LAN.

Access is gated by a 256-bit hex token (`WREN_CHAT_TOKEN`) compared in constant
time; the token cookie is the only credential. The token's entropy already makes
brute-force infeasible, but `/login` also applies a per-client failed-attempt
throttle (`LoginThrottle` in `chat/login_throttle.py`) as defense-in-depth — a
handful of wrong guesses trigger a short, backing-off lockout. The tuning stays
lenient enough that a legitimate mistyped token doesn't durably lock the single
user out.

Every response carries hardening headers — `Content-Security-Policy`,
`X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`,
`Referrer-Policy: no-referrer` — against clickjacking and any future markup slip
(`_security_headers` in `chat/server.py`). The CSP still allows `'unsafe-inline'`
for style/script because the pages carry their logic in inline blocks; the
`self` + `frame-ancestors 'none'` policy is the meaningful win, and tightening to
nonces would mean moving that JS into `/static`.

### The one endpoint without a session cookie

`POST /api/bg/resolve` is exempt, because a background-task approval button on
the phone calls it directly from the ntfy app. It's authenticated instead by an
HMAC-signed, ~1h-expiry, effectively single-use token
(`background.make_approval_token`, signed with `FLASK_SECRET_KEY`), and it does
exactly one thing — flip one already-model-proposed job to approved/denied — so
even a leaked token has a bounded blast radius. Single-use falls out of the job
state machine rather than a token store: `resolve_job` only acts on an
`awaiting_approval` job, so a replay finds nothing to do. Every hit is logged.
It's POST-only: a mutating GET invites prefetchers and leaves the token in more
access logs than it needs to.

## Prompt injection

Untrusted external text flows into model prompts from several tools: Tavily
search results, fetched web pages, GitHub `recent_changes`, incoming email on the
`Wren/Watch` and `Wren/Do` labels, ClickUp Task titles and descriptions,
opportunity-scout feeds (SEC EDGAR, ATS boards, HN), Chrome history titles,
YouTube video titles/descriptions, and the vault pages ScribeJay writes from
AI-chat transcripts. Any of these could contain text crafted to steer the model.
The blast radius is contained by design, in layers.

**Chat is the only place the model drives tool-calling freely**, and every write
tool there is confirmation-gated in code (`confirm_before` in `agent/loop.py`,
sourced from `toolset.WRITE_TOOLS`) — not merely requested in the prompt, so it
doesn't depend on a small local model reliably remembering to ask. The model
cannot send an email, create a calendar event, or write a memory without an
explicit user "yes".

The confirmation card shows the *substance* of the pending action, including the
email's **recipient** and a preview of its **body** — not just the subject — so an
injected or hallucinated message can't be approved sight-unseen. The recipient
isn't the model's to choose anyway: the model-facing `send_email` accepts only
what its schema declares (subject, body) and pins the recipient to
`BRIEF_TO_EMAIL` (`email.send_email_tool`), so an injected `to=` argument is
dropped rather than silently honored — the loop's `fn(**fn_args)` dispatch would
otherwise forward it. The chat card and the background approval push render
through the same describers (`toolset.describe_call` /
`describe_call_detail`), so the two surfaces can't drift on what they disclose.
A drift-guard test (`tests/test_toolset.py`) fails if a newly gated tool has no
describer, since the fallback would show raw JSON on the one surface a human
reads before approving.

**The prose scheduled tasks keep the model out of the write path entirely.**
`morning_brief`, `daily_synthesis`, `opportunity_digest`, `project_scan` and
`starred_blurbs` use the tool-free `complete_text` path — the model only writes
narrative prose and scores, it never calls a tool, so injected instructions have
nothing to actuate.

Two tasks go further and use **no model at all**, which removes the surface
rather than containing it. `log_inspector` scans logs in pure Python — a health
check that called the model couldn't report that the model is down. And in
`clickup_watcher` the tag *is* the decision: `wren-research` / `wren-context`
selects a prompt Python then fills in, so there is no classification step for
injected Task text to steer. (Its reason was Ollama's single slot, not security,
but the surface is gone either way.) The job that tag **queues** does drive the
model — and pauses for a tap on every write, because the user tagged the item and
walked away.

**Rendered output is escaped and scheme-validated.** All model output rendered to
HTML is `html.escape`d and any URL passes a scheme allow-list
(`tasks/_urls.py:safe_url`, the single copy every emitter imports)
before it reaches a page or an email, so injected output can't smuggle scripts or
`javascript:`/`data:` links in. The browser pages assign every model- or
log-derived string via `textContent`, never `innerHTML`.

**Long-term memory is a *persistence* vector.** A fact saved from untrusted text
("remember what that page said") is injected into every future system prompt. So:
capture is explicit and user-initiated, never a background scrape; the writes
(`remember` / `pin` / `recategorize` / `forget`) are confirmation-gated; and
pinned facts are rendered under a heading that frames them as reference facts to
recall, *not* instructions to act on (`memory.render_memory_block()`). `forget`
is gated too, so a poisoned memory can't be silently pruned to cover tracks. See
[memory.md](memory.md).

**Background tasks run detached, with nobody present to confirm** — so they
follow an **"A + push-to-approve"** posture (`toolset.CONSEQUENTIAL_TOOLS`). The
worker reads, researches, and drafts freely, but any external/irreversible action
(`send_email`, `send_morning_brief`) *pauses* the job and is pushed to the
phone for a tap-to-approve decision before it runs — the tap gets an immediate
confirming push ("Approved" / "Denied"), since the ntfy buttons have no selected
state of their own. So untrusted content pulled mid-task can never trigger an
unattended irreversible action: the "no human in the loop" leg of the injection
trifecta (untrusted input × consequential action × no confirmation) is removed
for exactly those actions.

Going further, the tools that write **prompt-visible state** —
`remember`/`pin`/`archive`/`recategorize`/`forget` and
`write_skill`/`delete_skill` — aren't in a background run's toolset **at all**
(`toolset.UNATTENDED_EXCLUDED_TOOLS`), not merely gated. Pinned memories and
skills feed *future* system prompts, so a poisoned page read mid-job must not be
able to plant a durable instruction that outlives the job, even behind an
approval tap. The read side (`recall`, `read_skill`) stays available.

**A job whose task text came out of an email gates far more.** The posture above
is calibrated to a task the user typed. `tasks/mail_watcher.py` queues a
`Wren/Do` email as a job with `origin="mail"`, and `toolset.confirm_set_for()`
then gates every tool outside `toolset.MAIL_JOB_SAFE_TOOLS` — including
reversible internal writes, and including read-only tools that take a
model-chosen URL or query (`fetch_webpage`, `search_web`, `evaluate_app`,
`evaluate_against`, `research_company`), since a URL is an exfiltration channel
whatever it fetches. The set is written as an allow list precisely so a tool
added later is gated by default; `run_in_background` takes no `origin`, so the
model can never place its own job on the looser branch. See
[mail-watch.md](mail-watch.md).

The same set also excludes `run_in_background`, `list_background_jobs` and
`get_job_result`, for a separate reason: a background job has no business
spawning or inspecting background jobs. That closes a job-queue recursion, not
an injection path. Starting a background task is itself confirmation-gated in
chat, so a job only runs because the user said to. See
[background.md](background.md).

## The rule, and where the policy lives

> The model can actuate a **consequential** write only with an explicit human
> "yes" — a tap in chat (`advance()` / `confirm_before`), or a phone approval for
> a background task's external/irreversible action.

The one place the model actuates a write *unattended* is a background task making
a **reversible, internal** write to the user's own account (creating a task,
recoloring an event) — a deliberate, bounded exception, not a general autonomy
grant.

That whole line is three editable sets in `agent/toolset.py`:

| Set | Meaning | Move a tool here to… |
|---|---|---|
| `WRITE_TOOLS` | chat pauses for a tap | require confirmation in chat |
| `CONSEQUENTIAL_TOOLS` | a background run must get phone approval | require approval unattended (out of it = runs unattended) |
| `UNATTENDED_EXCLUDED_TOOLS` | removed from a background run's toolset entirely | deny it to background runs outright |

`CONSEQUENTIAL_TOOLS` is a subset of `WRITE_TOOLS`, and a test enforces it. If
new work ever wires the model to a write tool, hold this boundary — gate the
consequential ones, or keep the model out.

## Cloud backends widen the boundary

The local-first default means nothing about the user's day leaves the machine at
runtime. Selecting a cloud backend (`WREN_LLM_BACKEND` / `WREN_<TASK>_BACKEND`,
or tapping the frontier-escalation button) **sends that task's tool results and
conversation history to the provider**. `bg_worker` is the sharpest case, since
it ingests untrusted web content *and* runs unattended — pointing it at a cloud
backend is the riskiest single switch. See [llm-backend.md](llm-backend.md) and
[frontier-escalation.md](frontier-escalation.md).

## Credentials never ride out inside an error string

`requests` puts the request URL — query string and all, unredacted — inside its
exception messages: `raise_for_status()` raises "401 Client Error: Unauthorized
for url: <url>", and a `ConnectionError` carries "Max retries exceeded with url:
<path?query>". Almost every API here authenticates by header, but
`agent/tools/weather.py:62` passes its key as `appid=` in the query string, so
that exception text *is* a live credential.

That matters because the `{"error": ...}` dicts those exceptions become do not
stay local. They are rendered into the morning brief and **emailed**, handed to
the model as a tool result, and written to `logs/`. Between 2026-07 and
2026-09-02 a single failed weather call sent the OpenWeatherMap key to
`BRIEF_TO_EMAIL`.

`agent/tools/_http.py:redact_query_values` now blanks the value of every query
parameter on **every** branch of `http_error`, including the catch-all — an
exception type nobody anticipated is exactly the one that would carry a URL out.
Parameter names survive, so the message still says which call failed. The rule
for new work: a credential belongs in a header, and if it cannot be, assume its
exception text is public.

The tests that missed this are worth knowing about. `tests/test_weather.py` and
`tests/test_http.py` both built `HTTPError(response=resp)` with **no message**,
then asserted `startswith("HTTP 401")` — green for months against a string that
could not contain a URL. The replacements build the exception the way `requests`
does, and were confirmed to fail with the redaction disabled.

## Related

- `AGENTS.md` — the "Untrusted content boundary" and data-sourcing rules that
  govern new capabilities.
- [background.md](background.md) — the approval flow and exclusions in full.
- [memory.md](memory.md) — why the memory writes are gated, and the friction
  tradeoff.
- Periodic audits of this posture land in [reviews/](reviews/).
