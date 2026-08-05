# LocalLLMAgent — Codebase Review & Improvement Plan

*Comprehensive review performed 2026-08-05 (scheduled audit). Covers security,
structure, performance, and documentation accuracy. No code was changed — this
plan is for manual review and approval.*

Baseline: `59c1cf3` (end of the 2026-07-29 review) → `681ab87` (HEAD), 13
commits. Suite: `984 passed in 3.69s`. Working tree clean.

## Overall assessment

The repo is in good shape and the conventions are holding. Six of the nine audit
dimensions came back with nothing to fix, including two that have slipped
before: every runtime store under `config/` is gitignored *and* conftest-
redirected, and every capability added in the 13-commit window shipped with both
its `docs/<name>.md` file and its README row.

The three findings below are all small. None is a live defect — one is a
duplication that hasn't drifted *yet*, one is a lost diagnostic, one is a stale
comment. They're worth doing because each sits exactly where the next author
will be misled.

---

## Findings

### 1. `_safe_url` exists in three places — a security primitive with three homes

| File | Symbol |
|---|---|
| `tasks/morning_brief.py:171` | `_safe_url` |
| `tasks/opportunity_digest.py:591` | `_safe_url` |
| `tasks/_learnings_common.py:126` | `safe_url` (public name) |

All three bodies are currently identical:

```python
try:
    return url if urlparse(url).scheme in ("http", "https") else ""
except (ValueError, AttributeError):
    return ""
```

They have not drifted yet, which is the reason to act now rather than later. Two
things make drift likely:

- CLAUDE.md names `tasks/morning_brief.py` as *"the `_safe_url` pattern"* — i.e.
  it instructs the next author to **copy** it. A fourth copy is the documented
  workflow.
- The three names already disagree (`_safe_url` twice, `safe_url` once), so a
  grep for the private name misses one third of the call sites. Anyone auditing
  scheme validation by searching `_safe_url` gets an incomplete answer.

The risk isn't today's behavior — it's that a future hardening (adding `mailto:`
rejection, normalizing unicode, handling `\` in schemes) lands in one copy and
silently leaves two HTML/Markdown emitters unpatched.

**Proposed fix:** promote one implementation to a shared home and have the other
two import it. `tasks/_learnings_common.py` already holds shared task helpers,
but it's learnings-specific; a small `tasks/_urls.py` (or `agent/urls.py`, if
chat-side rendering ever needs it) is the cleaner target. Export the public name
`safe_url`, delete the two private copies, update the three call sites, and
change the CLAUDE.md line from "the `_safe_url` pattern in
`tasks/morning_brief.py`" to "import `safe_url` from `tasks/_urls.py`" so the
instruction stops producing copies.

---

### 2. Three interactive model calls omit `logger=`, discarding the cut-off diagnostics

| Call site | `think=` | `logger=` |
|---|---|---|
| `agent/tools/research.py:188` | `False` | **missing** |
| `agent/tools/evaluate_against.py:172` | `False` | **missing** |
| `agent/tools/evaluate_app.py:116` | omitted (thinking ON, deliberate) | **missing** |

Every scheduled task passes `logger=` — `project_scan.py:129` and
`opportunity_digest.py:541` even carry the comment *"surfaces loop.py's
num_predict cut-off warning."* These three chat-invoked tools don't, and
`agent/loop.py:227-247` gates **all** of its diagnostics behind `if logger:`:

- the `prompt_tokens >= num_ctx` front-truncation warning,
- the `eval_tokens >= num_predict` cut-off warning — the one whose message
  literally reads *"a thinking model spending the whole budget on scratchpad
  (which returns EMPTY content)"*,
- and `_diagnose_stall`'s busy-vs-wedged verdict on timeout (`loop.py:219`).

All three tools do guard the empty case (`if not X.strip(): return {"error":
"...retry"}`), so the user isn't shown a blank. But the error text says *retry* —
it cannot say *why*, and nothing reaches `logs/wren.log`. Truncation, a wedged
runner, and a genuinely empty generation all produce the same message.

`evaluate_app.py` is the sharpest case: it's the one tool that deliberately
keeps thinking **on**, and its own comment says *"The guard below is the backstop
if a longer page ever changes that."* The guard can't perform that role — it
detects the symptom and drops the evidence. A longer marketing page pushing past
3072 tokens would present as "retry" forever with no log line naming the cause.

This also cuts against the recorded practice that pytest can't see
empty/truncated responses because it monkeypatches every model call — the
live-run log is the only place this failure is visible, and for these three
tools that log is empty.

**Proposed fix:** thread the module logger into all three calls —
`logger=logger` where the module already has one, otherwise
`logging.getLogger("wren")` to match `chat/routes_games.py:23` and land the lines
in the chat server's log. Three one-line edits; no behavior change on the success
path.

---

### 3. `chat/routes_games.py:33-34` contradicts `docs/games.md:40-41`

The code comment on the proxy timeouts:

> Flask runs threaded, so a warmup parked here for ten minutes doesn't block chat.

The capability doc:

> **Game turns and chat turns queue behind each other.** The AI seats think with
> the same local model chat uses, and Ollama serves one generation at a time.

Both describe the same 620-second warmup window. The comment is true about the
*Flask* thread and misleading about the *outcome*: `docs/ollama-serving.md:14`
records `OLLAMA_NUM_PARALLEL=1` with silent queueing, and lists starvation as
failure mode #1 — *"A long job holds the slot; everyone else waits."* A
ten-minute Weigh Anchor warmup does block chat, just one layer down from where
the comment is looking.

This matters because the comment sits directly above the constant a future
maintainer would tune. Someone reading it while raising `WARMUP_TIMEOUT_S` would
conclude the blast radius is one Flask thread.

**Proposed fix:** extend the comment to name the real constraint — Flask's
threading keeps the *server* responsive, but the generation still holds Ollama's
single slot, so a chat turn started during a warmup queues behind it
(cross-reference `docs/games.md` and `docs/ollama-serving.md`). Comment text
only.

---

## Verified clean

Recording these so the next audit can skip re-deriving them:

- **Environment variables** — 59 documented, 48 read. Every apparent mismatch
  resolves: API keys go through `resolve_key()`, the seven `WREN_*_BACKEND`
  entries are constructed dynamically at `agent/loop.py:322`,
  `WREN_EXTERNAL_TASK_ROOTS` reads via the `EXTERNAL_ROOTS_ENV` constant, and
  `GOOGLE_API_KEY` / `WREN_ESCALATION_BACKEND` are documented in `.env.example`
  prose. **No drift in either direction.**
- **Documentation links** — 23 files in `docs/`, all linked from README; no
  broken `docs/*.md` reference in README or CLAUDE.md.
- **HTTP timeouts** — a paren-balanced scan of every `requests.*` / `urlopen`
  call across `agent/`, `tasks/`, `chat/` found zero without `timeout=`. (A naive
  single-line grep reports ~15 false positives on multi-line calls; ignore that
  result if it recurs.)
- **Route authentication** — all 18 routes across the three blueprints check
  `_authenticated()`. `routes_games.py` correctly redirects browser navigations
  to `/` instead of returning a JSON 401.
- **Store / gitignore / conftest triad** — all 10 runtime stores under `config/`
  are gitignored and redirected to `tmp_path` in `tests/conftest.py`, including
  the newest (`projects.json`).
- **Degradation logging** — `score_items` (`opportunity_digest.py:543`) warns
  with counts on a partial batch, exactly as the rule requires. `parse_nudges`
  has no fixed input cardinality, so silence there is legitimate.
- **Frontend tests** — all three standalone scripts in `chat/static/`
  (`chat-dock.js`, `nav.js`, `run-chart.js`) have matching jest suites.

## Considered and dismissed

- **`game_ai` path injection** (`routes_games.py:102`) — `endpoint` is
  user-controlled and interpolated into the proxy URL, but the host and port are
  fixed from the registry, so a crafted value can only reach another path on the
  same loopback service, and only for an already-authenticated user. Not worth
  code.
- **`request.max_content_length` per-request override** (`routes_games.py:49`) —
  requires Flask ≥ 3.1; `requirements.txt` pins `flask==3.1.3`. Valid.
- **Module sizes** — `chat/server.py` (969), `chat/insights.py` (916),
  `tasks/opportunity_digest.py` (823). Large, but each is internally sectioned
  and the blueprint extraction already happened. No split proposed; flagging only
  as a trend to watch.

## Suggested order

1. Finding 2 (three one-line edits, restores lost diagnostics)
2. Finding 3 (comment only)
3. Finding 1 (touches four files + CLAUDE.md; do it deliberately, with `pytest`
   before and after)
