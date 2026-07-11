# LocalLLMAgent — Codebase Review & Improvement Plan

*Comprehensive review performed 2026-07-10 (scheduled audit). Covers security,
structure/code quality, performance, documentation, and test coverage. No code
was changed — this plan is for manual review and approval.*

> **Status (2026-07-11):** steps 1–7 of the execution order have been executed
> on this branch (see the commit history: one commit per step, two for step 7's
> sub-batches). All high-severity items (H1–H4), all mediums (M1–M10), and the
> low-severity items are addressed, with these deliberate exceptions:
> **L-deferred:** the `.env`-loading/`sys.path` centralization (item 9 below —
> pure churn, no behavior change), the HTTP-endpoint reference table (item 13,
> "not urgent"), and hardening `int(os.getenv(...))` parses (item 11 — a
> malformed hand-edited value failing loudly at startup is acceptable
> feedback). Tool-result logging (item 3) was documented as a privacy note in
> the README rather than redacted, since the dashboard's run history depends
> on parsed results. Suite grew from 260 to 331 tests, all passing.

## Overall assessment

The codebase is in genuinely good shape for its threat model (single user,
Tailscale-only exposure, local model). Verified clean: no shell/SQL injection
surface (the one `subprocess` call uses list args with plist-sourced, validated
input; Chrome history uses parameterized queries on a read-only connection);
path traversal is properly guarded in `wiki.py` and `skills.py`; no SSRF (all
HTTP tools target hardcoded hosts); all four frontends render model/log text
via `textContent` only, backed by CSP/`X-Frame-Options`/`nosniff` headers;
login uses `hmac.compare_digest` on a 256-bit token with Secure/SameSite
cookies; secrets are gitignored and the Google token is written `0o600`; the
unauthenticated `/api/bg/resolve` endpoint is correctly protected by a signed,
expiring, effectively single-use token. All outbound HTTP calls except the
Google client carry timeouts, JSON stores use atomic writes, dashboard log
parsing is signature-cached, and log files rotate. **All 260 tests pass
(~1s, fully offline)**, README accuracy is exceptional, and docstring quality
is consistently high with rationale for the tricky parts.

The findings below are real but none is an emergency. The four high-severity
items are where review time should go first.

### Priority summary

| # | Finding | Area | Severity |
|---|---------|------|----------|
| H1 | Background jobs auto-execute `write_skill`/`pin` — prompt-injection persistence channel | Security | High |
| H2 | JSON stores have no cross-process locking — lost updates between server and launchd workers | Correctness | High |
| H3 | `send_email` honors an undeclared `to`/`html` argument; chat confirm card never shows the recipient | Security | High |
| H4 | Chat history grows unbounded and silently blows the 8K context window | Performance/Correctness | High |
| M1–M10 | Store robustness, worker retry/stranding, Google timeouts, dedup refactors, eviction, test gaps | Various | Medium |
| L1–L14 | Hygiene: throttle keying, unpinned dep, log privacy, doc touch-ups, dead code | Various | Low |

---

## High severity

### H1. Background runs auto-execute `write_skill` and `pin` — a prompt-injection persistence channel
- **Where:** `agent/toolset.py:179-181` (`CONSEQUENTIAL_TOOLS`), `tasks/bg_worker.py`, `agent/tools/skills.py` (`write_skill` overwrites by slug), `agent/tools/memory.py` (`pin` → rendered into every future system prompt).
- **Issue:** A background job pauses for phone approval only on `send_email`, `send_morning_brief`, `forget`, `delete_skill`. Everything else — `write_skill`, `pin`, `remember`, `log_calendar_event`, `create_task`, `set_reminder` — executes unattended. The stated invariant ("untrusted content pulled mid-task can never trigger an unattended irreversible action," `background.py:8`) holds only for those four tools.
- **Impact:** A web page or search result fetched during a background job can instruct the model to save a poisoned skill or pin a malicious "fact." Pinned facts are injected into every future system prompt and skills steer future multi-step procedures — a durable foothold from purely external content that can later shape an *approved* consequential action (e.g. an email whose body the injected skill dictates). This is the classic indirect-prompt-injection persistence pattern.
- **Recommendation:** Add `write_skill` and `pin` (and ideally `remember`) to `CONSEQUENTIAL_TOOLS`, or exclude them from the background dispatch entirely, as `BG_EXCLUDE` already does for the bg-management tools. The code comment at `toolset.py:177` already identifies this as "the one line to move to tighten the policy" — the calendar/task writes are a judgment call, but prompt-visible state (skills, pins) should not be writable unattended from runs that ingest untrusted content.

### H2. JSON stores race across processes — lost updates can re-run jobs and resurrect reminders
- **Where:** `agent/tools/background.py` (`_LOCK` + `_load`/`_save`), `agent/tools/reminders.py`, `agent/tools/memory.py` — all guard read-modify-write with a `threading.Lock` only.
- **Issue:** The writers are separate *processes*: the Flask server, `bg_worker` (every 30s), and `reminder_sweep` (every 60s). A thread lock does not serialize them; `os.replace` prevents torn reads but not lost updates.
- **Impact:** Worker `mark_done(jobA)` racing the server's `run_in_background(jobB)` means whichever save lands second clobbers the other: job B vanishes, or job A reverts to `pending` and **re-runs its already-executed side effects**. Same shape for reminders: a `set_reminder` racing the sweeper's `complete()` resurrects a fired reminder → duplicate pushes every subsequent sweep. Low probability per event, but these run thousands of times a day, forever.
- **Recommendation:** Add an inter-process lock (`fcntl.flock` on a sidecar `.lock` file) held across load-modify-save, implemented once in a shared store helper (see M4) so all three stores get it. Alternative: make each file single-writer by routing all mutations through one process.

### H3. `send_email` accepts an undeclared `to` argument, and the chat confirmation card never shows the recipient
- **Where:** `agent/tools/email.py:22-39` (schema declares only `subject`/`body`; the function signature accepts `to` and `html`), `agent/loop.py:177` (`fn(**fn_args)` forwards whatever the model emits), `chat/server.py:325-373` (`_describe_call`/`_describe_detail` show subject + body preview only).
- **Issue:** A hallucinated — or injected — `to` argument is silently honored, while the human approving the send in chat sees only the subject and body. The background path's describer (`tasks/bg_worker.py`) has already diverged from the chat one on exactly this.
- **Impact:** Combined with H1's ingestion of untrusted content, an injected `send_email(to="attacker@…")` presents an innocuous-looking confirmation card in chat. The approval gate fires, but the approver can't see the thing that matters.
- **Recommendation:** Either wrap `send_email` for `DISPATCH` with a fixed recipient (strip `to`/`html` from the model-facing callable), or declare them in the schema **and** render the effective recipient (`args.get("to") or BRIEF_TO_EMAIL`) on both confirmation surfaces. Merge the two `_describe_call` implementations into `agent/toolset.py` so they can't diverge again.

### H4. Chat history grows unbounded and silently overflows the 8K context window
- **Where:** `chat/server.py:215,428-445` (`conversations[sid]` appended forever), `agent/loop.py:99,142-147` (`num_ctx` fixed at 8192; front-truncation is *detected and logged* but not prevented).
- **Issue:** Every turn re-sends the whole history; tool results add up to 8,000 chars each. Nothing trims or summarizes.
- **Impact:** Prefill latency grows linearly with session length (tens of seconds per turn on a local model once history is large). Worse, once `prompt_tokens >= num_ctx`, Ollama silently drops the *front* of the prompt — the system prompt with identity, tool rules, and pinned memories — which is precisely the repetition-loop failure mode the code comments describe. A long research-style chat hits this in ~5–10 heavy turns.
- **Recommendation:** Enforce a budget on `history` in `/chat` before `advance()`: keep the system message pinned, and when total size exceeds a threshold (~24K chars, or driven precisely by the `prompt_tokens` value Ollama already returns), drop or summarize the oldest assistant/tool message pairs. Surface a subtle "older messages trimmed" note to the UI if desired.

---

## Medium severity

### M1. A corrupted store file crashes every chat turn and every scheduled run
- **Where:** `memory.py`, `background.py`, `reminders.py` — `_load()` catches only `FileNotFoundError`.
- **Impact:** One bad byte in `wren_memory.json` and `render_memory_block()` (called from `with_identity()`, which seeds every conversation) 500s the chat server on every message, fails the bg worker every 30s (spamming failure pushes), and kills the morning brief. `tasks/morning_brief.py:61-65` already demonstrates the right pattern (`FileNotFoundError, JSONDecodeError, OSError`).
- **Recommendation:** In the shared load helper, catch decode/OS errors, move the corrupt file aside (`.corrupt-<ts>`), log loudly, return the empty store.

### M2. `bg_worker` cold-starts the full Google/agent import stack every 30 seconds
- **Where:** `launchd/...bgworker.plist` (`StartInterval` 30), `tasks/bg_worker.py` (module-level import of `agent.toolset` → googleapiclient et al.).
- **Impact:** ~2,880 interpreter launches/day at ~0.5–1.5s of import CPU each — on the order of 30–70 minutes of pure import CPU daily on an always-on Mac, almost always to find an empty queue.
- **Recommendation:** Import the heavy stack lazily after `next_actionable()` returns a job (`background.py` itself needs only stdlib + itsdangerous), and/or switch the plist to `WatchPaths` on `config/bg_jobs.json`, or raise `StartInterval` (approval latency is human-gated anyway).

### M3. One transient error permanently fails a background job
- **Where:** `tasks/bg_worker.py:111-126` — all exceptions → `mark_failed`.
- **Impact:** An Ollama restart or momentary network blip terminally fails a job whose auto-executed side effects have already happened; it would have succeeded 30 seconds later.
- **Recommendation:** Classify transient errors (`ConnectionError`, `Timeout`): leave the job actionable with a persisted `attempts` counter, mark failed only after N attempts.

### M4. An `awaiting_approval` job can strand forever
- **Where:** `agent/tools/background.py` — token expiry 3600s, `approval_actions()` returns `None` without `WREN_PUBLIC_URL`, CLI supports only `--list`.
- **Impact:** Tap after an hour → expired token; no `WREN_PUBLIC_URL` → no buttons at all; and there is no CLI/dashboard path to resolve, so the job sits invisible to `next_actionable()` until someone hand-edits `bg_jobs.json`.
- **Recommendation:** Worker re-pushes with fresh tokens for jobs stuck `awaiting_approval` past the token lifetime; add `--approve/--deny <job_id>` to `background.main()`.

### M5. Google API calls are the only HTTP surface with no timeout
- **Where:** `agent/tools/google_auth.py:96-111` — `build(...)` on the default httplib2 transport.
- **Impact:** A wedged connection to googleapis.com blocks a chat-turn thread indefinitely (`should_cancel` is only consulted between model chunks, so Stop can't interrupt a blocked tool call) or hangs a launchd run past its next fire.
- **Recommendation:** Build services over `httplib2.Http(timeout=30)` (via `AuthorizedHttp`), or `socket.setdefaulttimeout(30)` in task entrypoints.

### M6. Atomic-write/load boilerplate duplicated four times
- **Where:** `memory.py`, `reminders.py`, `background.py`, `tasks/morning_brief.py` — byte-identical `_save`, near-identical `_load`.
- **Impact:** H2 and M1 must each be fixed in four places; drift is inevitable.
- **Recommendation:** `agent/store.py` with `load_json(path, default)` / `atomic_write_json(path, data)` plus the H2 file lock. This is the enabling refactor for H2 + M1 — do it first.

### M7. Chat-server session state is never evicted
- **Where:** `chat/server.py:215-222` — `conversations`, `pending_confirmations`, `cancel_events` grow until restart; `/chat/new` clears only the current sid.
- **Impact:** Slow leak (each history can be hundreds of KB per H4); also two concurrent `/chat` requests for one sid interleave appends into the same history mid-`advance()` and clobber each other's cancel Event (no per-sid lock).
- **Recommendation:** Timestamp histories and sweep entries idle >24h on each `/chat`; add a per-sid lock or return 409 when a turn is already running for the session.

### M8. Calendar category color IDs duplicated as magic strings (with a str/int mismatch)
- **Where:** `tasks/weekly_learnings.py:141-154` hardcodes `"1"`, `"3"`, `"6"`; `agent/tools/strava.py:166-167` uses `colorId: 4` (int) where `CATEGORY_COLORS` in `agent/tools/calendar.py:63-73` — the declared single source of truth — uses strings.
- **Impact:** Recoloring a category in `CATEGORY_COLORS` silently empties the corresponding bucket in every future weekly review — no error, just degraded output.
- **Recommendation:** Derive `_categorize`'s id→bucket map from `CATEGORY_COLORS`; import the constant in strava.py or drop its advisory fields.

### M9. `chat()` / `chat_confirm()` duplicate the whole advance/rollback/cancel scaffold
- **Where:** `chat/server.py:440-459` vs `478-496` — ~20 lines copy-pasted; the confirm-then-checkpoint ordering subtlety is documented in only one copy.
- **Recommendation:** Extract `_run_turn(sid, history, checkpoint)`; both routes become a few lines. Do this before H4's trimming logic lands so it lands once.

### M10. Test gaps in the unattended paths
- `tasks/calendar_colorizer.py` has **zero** test coverage — the only scheduled task with none, yet it patches real calendar events daily; its model-JSON parse / `VALID_COLOR_IDS` validation is testable pure logic. Extract the classify-and-apply block and test malformed JSON, unknown colorIds, and the error branch.
- `tasks/weekly_learnings.py`: `_week_range` (year boundaries) and the vault-unmounted → email-fallback path (a README-stated "never silently lost" guarantee) are untested. Note the fallback's `send_email` result is unchecked — it returns `{"error": ...}` rather than raising, so vault-unmounted + Gmail error loses the draft to the log despite the contract.
- `log_calendar_event`'s `source_id` dedupe — the mechanism behind "re-runs never create duplicates" — is never exercised; test with a stubbed Google service.
- Add a three-line registry invariant test: `TOOLS` schema names ⊆ `DISPATCH`, `WRITE_TOOLS` ⊆ `DISPATCH`, `CONSEQUENTIAL_TOOLS` ⊆ `WRITE_TOOLS`.

---

## Low severity

1. **Login throttle keyed on spoofable `X-Forwarded-For`** (`chat/server.py:298-305`): an attacker on the tailnet can rotate the header to bypass lockout; also failed-only entries are never expired (unbounded dict). Key on `request.remote_addr` unless the peer is the known proxy, and sweep stale entries. (Defense-in-depth only — the 256-bit token is the real control.)
2. **`itsdangerous` is a direct import but unpinned** — present only transitively via Flask, contradicting requirements.txt's stated pin-everything policy. Add an explicit pin; also run `pip-audit` against the resolved env periodically.
3. **Tool results logged unredacted** (`agent/loop.py:190`): `_redact_args` masks secret-looking argument keys, but full results (email bodies, browsing history, memory text) land in `logs/*.log` and are re-served by the run-detail API. No credential leak found — this is personal-data-at-rest. Consider truncating read-tool results in logs, and treat `logs/` as sensitive in backups.
4. **`bg_jobs.json` grows forever** and is re-parsed every 30s; `list_background_jobs` returns every job ever. Prune done/failed jobs older than ~14 days; cap the listing at ~20.
5. **Dashboard run-status poller never stops on fetch errors** (`chat/static/dashboard.html:344-347`): a stuck tab polls every 2.5s indefinitely. try/catch + clearInterval after N failures.
6. **`insights.py` failure detection by substring** (`chat/insights.py:335-339`): `"failed" in msg` matches tool-result lines (e.g. `fetch_strava -> {"error": "...refresh failed..."}`) because the failure branch runs before the `" -> "` guard — green runs get spurious error text. Apply the same `"->" not in msg` guard used by `_is_run_success`.
7. **Interval tasks misclassified as daemons** (`chat/insights.py:113`): `bg_worker`/`reminder_sweep` (StartInterval) show as "Always on" — no run history, no Run-now. Detect StartInterval as a third kind. (Note: today this misclassification is what prevents a dashboard-triggered `bg_worker` from racing launchd's copy — resolve H2 first or keep the guard deliberately.)
8. **`run_agent()` is dead code** (`agent/loop.py:280-300`): no callers; remove or fold its docstring's dispatch-contract text into `advance()`.
9. **`.env` loading and `sys.path` hacks duplicated across ~9 modules**; `_http.py` hosting env loading is misnamed. Centralize in a small `_env`/`_config` module.
10. **`weather.py` uses `urllib` and hand-rolls error mapping** next to the shared `requests` + `http_error()` conventions it partially imports. Align it.
11. **`RunManager._procs` never removes exited children** (zombie until a later `status()` poll); `daily_log` returns success (0) on partial failures, so a persistently malformed activity is skipped forever without an alert; `int(os.getenv(...))` at import crashes on malformed values — wrap with default + warning.
12. **`config/.env.example` missing** `OLLAMA_MAX_TOOL_RESULT_CHARS` and `WREN_SKILLS_DIR` (both README-documented). Add commented entries.
13. **README gaps:** the chat-tools section and the confirmation-gate list omit the five Google Tasks tools (the gate list names 8 of 13 `WRITE_TOOLS` — make it reference `toolset.WRITE_TOOLS` as the single source of truth); launchd setup step 7 should say "edit the absolute `/Users/craigdube/...` paths in each plist first"; the test-suite description understates current coverage (260 tests / 20 files); an endpoint reference table (route, method, auth, purpose) would consolidate the scattered route prose.
14. **`/api/bg/resolve` accepts GET as well as POST** with the token in the query string (query strings tend to end up in proxy/access logs). ntfy's http action supports POST — drop the GET method and keep tokens out of URL logs where possible.

---

## Suggested execution order

1. **`agent/store.py`** (M6): shared `load_json`/`atomic_write_json` + `fcntl` file lock → fixes **H2** and **M1** in one place; migrate the four stores.
2. **Tighten background policy** (H1): move `write_skill`/`pin`/`remember` into `CONSEQUENTIAL_TOOLS` (one-line change the code comment already anticipates) + a test asserting the policy.
3. **Email recipient hardening** (H3): schema + describer fix, merge the two `_describe_call`s into `toolset.py`.
4. **History trimming** (H4), after extracting `_run_turn` (M9) so it lands once.
5. **Worker robustness batch** (M2–M4): lazy imports, transient-retry, stranded-approval re-push + CLI resolve.
6. **Google client timeout** (M5) — small, isolated.
7. **Test debt** (M10) + the low-severity hygiene items opportunistically.

Items 1–4 are each small, independently shippable, and cover everything
high-severity. Nothing here requires an architectural change.
