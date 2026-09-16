# Self-knowledge

Wren can read her own configuration and the documentation of all three systems
she is made of — her own repo, the wiki engine that fills her knowledge base,
and the journaling agent that writes the record she reads. Two tools for the
settings and the docs, one deferred tool group, and nothing added to the system
prompt.

## What prompted it

The user asked Wren what colour the calendar uses for meal prep items. She said
she had no record of one.

She was wrong, and not in a way a memory would have fixed. The answer had been
in `config/settings.json` since the calendar categories were first written:

```json
{"name": "Meal Prep", "color_id": "10", "color_name": "Basil"}
```

`agent/tools/calendar.py` builds `CATEGORY_COLORS` out of that same table at
import and colours the calendar with it every day. No tool and no prompt block
ever showed it to the model. She was blind to her own settings.

A second blindness sat next to it. `README.md` (96KB) and `docs/` describe how
she works, what her limits are, and why each was chosen — none of it reachable
at runtime. Verified: "Meal Prep" and "Basil" appear in **zero** files under
`README.md`, `AGENTS.md` or `docs/`, so documentation alone would not have
answered the question either. Two real gaps, and they are different.

## Why not memories, and why not the wiki

Both were considered first and both were rejected.

Copying eleven colours into `config/wren_memory.json` would spend most of the
1500-char active-memory block (`MAX_MEMORY_BLOCK_CHARS`) on facts that go stale
the moment the user edits `/settings`. The wiki has the same duplication-and-drift
problem one repo further away, and it is ObsidianWikiAgent's store, not a place
for machine-readable configuration.

The authoritative copy already existed and was already loaded. It only needed a
reader.

## The prompt budget

The chat prompt already crowds `OLLAMA_NUM_CTX=49152`. **This change adds
nothing to it.** All three tools sit in one deferred group, `self`, whose only
always-on cost is a single `_GROUP_BLURBS` line.

The tempting alternative — rendering the eleven-row colour table into every
chat prompt — was designed and then dropped. It would guarantee the answer, but
it costs several hundred characters forever to fix one question, and the
assumption underneath it (that a small model will not call a tool for a vague
question) is untested here. This repo has measured evidence the other way:
writing `list_games`' description to the catalogue rule in AGENTS.md took it
from 2-of-12 replays fabricating games to 12 of 12 calling the tool.

So the rule is: build the cheapest rung, measure against the live model, and
climb only on evidence.

| Rung | Always-on cost | Hops | When to take it |
|---|---|---|---|
| 1. All three tools in the `self` group | one blurb line | 2 (`load_tools`, then call) | **This is what shipped.** |
| 2. Promote `describe_setup` to `CORE_TOOL_NAMES` | ~500 chars | 1 | Only if the group is measurably not being loaded. |
| 3. Render the colour table into the chat prompt | ~600 chars more | 0 | Only if rung 2 also fails. |

A core tool schema averages **674 chars** across the current core tools, so
rung 2 costs about what rung 3 does. Neither is free; both are last resorts.

## `describe_setup` — the settings

`agent/tools/setup_info.py`. Returns every non-empty preference section plus
the timezone. The calendar categories come back with both the colour name and
the colorId, because the name is what the user sees in Google Calendar and the id
is what the colorizer writes — one without the other is half an answer, and
which half depends on what he is doing.

It takes **no arguments** on purpose. The whole payload measured 2,872
characters against the 8000-char tool-result cap in `agent/loop.py`, so there
is nothing to narrow and nothing for a small model to get wrong. A `section`
argument would only add a way to ask for the wrong one and get an empty answer
back.

Sections are read through `agent/prefs.py`, not off disk, so a save from the
`/settings` page reaches it without a restart.

`_comment` keys are stripped at every depth. They are notes to whoever
hand-edits the JSON — one of them is a shell command to run — and to the model
they read as instructions about a task it was not asked to do. They are also a
third of the learnings section by size.

### The security boundary

The settings document has two halves. `preferences` holds the structured
personal sections. `values` holds the flat schema rows, and **that is where
every credential lives** (`agent/schema.py` marks them `secret=True`).

This tool reads the `preferences` half only, through
`schema.PREFERENCE_SECTIONS`. That allowlist is the entire security argument:
the preferences half is personal data by construction, the values half holds
keys.

**Do not generalise this into a settings reader.** A tool that took a key name
and returned its value would be one model mistake away from putting an API
token in a chat reply, and the model is the least trusted part of the loop —
chat turns ingest untrusted web and mail content inline.

`tests/test_setup_info.py` plants a real-shaped credential in a fixture and
asserts it never surfaces, in the serialised result rather than as a top-level
key, because a leak would arrive nested inside whatever carried it. A third
test widens the allowlist the way a careless "improvement" would and asserts
the leak **does** happen — if that test ever stops failing-then-passing, the
two above it have stopped proving anything.

## `search_docs` / `read_doc` — the documentation

`agent/tools/docs.py`. Mirrors the *shape* of `agent/tools/wiki.py` — search
returns rows, read returns one trimmed document — without importing its
internals. The two corpora share no conventions, and coupling them would let an
ObsidianWikiAgent format change break this silently.

### The corpus

**About sixty documents, ~740KB, across three checkouts.** The wiki is the knowledge base
Wren queries constantly; how a note gets into it, and how it is maintained, is
part of how she works. Before 2026-09-15 she had no reachable answer to either.

**Wren's own repo — 40 documents**

| Included | Excluded | Why |
|---|---|---|
| `README.md`, `ANALYSIS.md` | `AGENTS.md` | Imperative instructions aimed at **coding agents**. "Run pytest before calling a change done" is an order meant for somebody else, and Wren reading it as one aimed at her is worse than noise. |
| `docs/*.md` (top level) | `docs/reviews/` | Gitignored. Audit plans, not documentation. |
| | `docs/handoff/` | Gitignored. Work belonging to sibling repos. |

`ANALYSIS.md` is in because it is the **descriptive** counterpart to AGENTS.md:
it explains how the subsystems fit without telling anyone to change anything.

**ObsidianWikiAgent — 4 documents.** `README.md`, `SECURITY.md` (the trust
boundary the vault sits behind), `docs/agent-context.md` and
`docs/removing-content.md`. `agent-context.md` is the point of including this
repo at all: its "Ingest and write-path decisions" section is the literal answer
to *how does data get into the wiki*, and it stands in the same descriptive
relation to that repo's `AGENTS.md` as `ANALYSIS.md` does to Wren's. Excluded:
that repo's `AGENTS.md` and `CLAUDE.md`, and `tools/lint_defects/**` — those are
deliberately **defective** wiki pages used as linter fixtures, and feeding them
in would put invented wiki claims in the corpus Wren is told to trust.

**ScribeJay — 20 documents.** `README.md` and the top level of `docs/`, which is
where `docs/architecture.md` lives — the file `docs/scribejay.md` explicitly
defers to. Excluded: `AGENTS.md`, `CLAUDE.md`, and `scribejay/persona.md`, which
is **another agent's system-prompt material**. Wren adopting ScribeJay's voice
because a search hit landed it in her context is a failure with no error message.

Every exclusion above holds **by construction**, not by a rule someone has to
remember: each sibling's root files are named one at a time, never globbed, and
only the top level of `docs/` is walked. `iterdir()`, never `rglob()` — all
three repos gitignore `docs/reviews/`, and a recursive walk would quietly pull
it back in.

### Three corpora, one namespace

Wren's own documents keep **bare** names (`limits`, `module-map`). A sibling's
carry its repo as a prefix: `scribejay/architecture`, `wiki/agent-context`.

The prefix is not decoration. `_doc_paths()` keys on the lowercased filename
stem with `setdefault`, and eight ScribeJay documents share a stem with one of
Wren's — `llm-backend`, `logs`, `model-constraints`, `ntfy-setup`,
`opaque-identifiers`, `readme`, `timezones`, `usage-ledger` — plus
ObsidianWikiAgent's `readme`. A flat merge drops all nine silently, and Wren
tells the user in good faith that ScribeJay has no timezone document while
reading her own.

A prefix was chosen over a second `repo` argument because it makes the wrong
combination unrepresentable: there is one string and the model copies it back,
the same way it already round-trips `module-map`. Bare names still resolve
exactly as before, so nothing that worked stopped working.

`read_doc` also accepts a **dropped** prefix — asking for `architecture` finds
`scribejay/architecture` when that is the only match, and returns the candidate
list when it is not. A bare name that *is* one of Wren's hits the exact match
first and never reaches that fallback: bare means Wren's.

### Where the roots come from

Wren's own root comes from `__file__`, not a setting — her docs travel with her
code, so there is nothing to configure. The siblings do not, so each has its own
settings key: `WREN_WIKI_REPO_PATH` and `WREN_SCRIBEJAY_REPO_PATH`, both
`applies="live"` because `_doc_paths()` reads them at call time.

**Neither reuses `WREN_WIKI_LINT_ROOT`.** Wren now has three relationships to a
sibling checkout — logs read, code run, docs read — and each keeps its own key.
Folding docs-read into the lint root means pointing the linter at a worktree
silently moves Wren's documentation corpus with it, and rewriting that row's
help text would make `/settings` lie about what the field does.

A checkout that is missing, moved or mid-upgrade yields no keys at all. Search
degrades to Wren's own documents and **never errors** — a sibling being absent
means "no ScribeJay docs today", not a broken tool.

Read live from disk on every call, no index — the wiki reads 1.19MB in 13ms and
this corpus is smaller.

A document's name is its filename stem (`limits`, `module-map`, `readme`), with
the repo prefix for a sibling's. Its summary is the first real paragraph after
the H1, with any opening code fence skipped, cut on a word boundary. Lines
opening with `<` are skipped too: ScribeJay's README starts with a raw `<img>`
tag above its H1, and without that one character its summary would be the tag.
Verified: **zero** of Wren's 40 summaries change.

### Scoring

Name and summary terms match as **substrings**, because names are slugs and a
term has to be able to match inside `module-map`. Body terms match on **word
boundaries**, because a body is prose, where substring matching is actively
wrong — it is what ranks a document for the `we` inside `power`.

Weights are name 3 > summary 2 > body 1. A term scores once per document
however often it appears, so a 30KB document cannot outrank a short precise one
on repetition alone — and this corpus has a 96KB README in it. A body-only hit
carries a ~200-char quote, which is the only evidence of why a document matched
a term its title and summary do not hold.

Ranking is `_rank_key(score, name)` → `(-score, is_a_sibling, name)`. Score
first, then **Wren's own document ahead of a sibling's on a tie**, then the name
so the order stays stable run to run.

The middle term is a fix for a measured regression, not a precaution. The
question was asked of Wren, and nine documents share a stem with one of hers, so
a tie is the common case here rather than the edge. Sorting on the name alone,
the alphabet decided it — `s` before `t` — and `scribejay/timezones` beat
`timezones`. Confirmed for `timezones` and `usage ledger` before the fix. With
it, 7 of the 8 ScribeJay collision pairs put Wren's document first; the eighth
(`ntfy setup`) is not a tie at all — ScribeJay's summary carries both query
terms and Wren's carries one, so it wins on summary weight legitimately.

`_rank_key` is a named module-level function purely so `tests/test_docs.py` can
monkeypatch it back to the broken `(-score, name)` shape and assert the sibling
wins — the same failing-then-passing guard pattern the stopword test uses.

### Reading, and the trim

Section reading is **required, not optional**. Roughly a third of the corpus
exceeds the 12000-char budget — no exact count is stated here, because most of
the long ones are the siblings' and that number changes with no commit in this
repo — and `README.md`'s architecture section alone is larger
than the whole budget. README stays one document read by section rather than
eleven synthetic documents, which reuses the `section` argument instead of
inventing a naming scheme.

`MAX_DOC_CHARS = 12000`, with `TOOL_RESULT_CHAR_CAPS["read_doc"] = 14000` in
`agent/loop.py`. **Keep the gap.** The backstop counts the JSON-escaped result
and is blind; if it ever fires it re-cuts a document this tool trimmed on
purpose, taking the trim notice with it.

A trimmed read names the sections it dropped and appends the guard the wiki's
reader uses: *"Do NOT say this document or the documentation lacks something —
you have not read all of it."* Handed a silently truncated document the model
treats what it got as the whole thing and reports that the docs do not cover
something sitting in the part it never saw.

Section matching is deliberately forgiving — exact, then either direction of
substring, then word overlap — because the model is re-typing a heading it read
in a trim notice, and these are long English phrases. An exact-match lookup
would turn a dropped word into a dead end.

The overlap branch drops words under four characters as a cheap stand-in for
"not a stopword", and **the proxy leaks**: `that`, `here` and `with` are all
four letters. Asking for a section named "something that is not a heading here"
returned "Limits that are not about the model", on the shared `that` alone. A
wrong section is worse than no section — no section returns the real heading
list and the model retries, while a wrong one it reads, answers from, and
neither of them learns it got the wrong part. Hence `_STOPWORDS`, which is
checked alongside the length filter.

### Safety by construction

`_doc_paths()` builds a `{name: path}` map first, and a model-supplied name
that is not a key simply does not resolve. Nothing joins model input onto a
path, so there is no traversal to defend against.

**The prefix does not weaken that.** Each key is built here in Python out of a
prefix this module chose and a stem read off disk — never parsed out of model
input. The dropped-prefix fallback matches against keys already in the map, so
`scribejay/../../etc/passwd` is simply a name that is not there.

## The tool descriptions carry the design

Nothing about any of this is in the system prompt — that was the point — so the
three descriptions are the only thing standing between a question and a
fabricated answer.

All three are written to the catalogue rule in AGENTS.md: a tool that answers
"what exists?" or "what is this set to?" must say the answer is **not** in the
model's head, that only what it returns is real, and what to say when it
returns nothing. Without the third clause an empty result gets filled in from
pretraining.

They also name the **everyday** phrasings, not the technical ones. The model
reaches these tools through a `load_tools` hop and will not make that hop for a
question it does not recognise. "What colour do we use for..." is the wording
that failed, so it is the wording in the description.

## Measured against the live model

`pytest` monkeypatches every model call, so it cannot see the two things this
change actually risks. Three cases in `evals/cases_chat.py`, replayed 12 times
each against `gemma4:26b-mlx` on 2026-09-15 — 12 reps because this repo's
precedent for catalogue-rule wording is a 12-replay count (`list_games` went
from 2-of-12 fabricating to 12 of 12 calling the tool):

| Case | Ask | Expected | Result |
|---|---|---|---|
| `docs_wiki_ingest` | "How do my notes actually end up in the wiki?" | `search_docs` | **12/12**, nothing fabricated |
| `docs_sibling_vs_vault` | "What does my wiki say about pricing?" | `search_wiki` | **12/12** |
| `docs_collision_readback` | "What does ScribeJay's timezone doc say?" | `read_doc(name="scribejay/…")` | **12/12**, arguments 12/12 |

The middle one is the discrimination case: `search_docs` now describes itself in
terms of the wiki, which puts two similar-sounding search tools in front of the
model, and this is the check that the broader description cost `search_wiki`
nothing. The third is the one the whole namespacing decision rests on — the
prefix survived the round trip every time.

`docs_wiki_ingest` also caught a real gap before a user did. The cue list held
`how do notes` and the natural spoken form is *"how do **my** notes"*; cue
matching is a literal substring, so the most likely phrasing of the question
this feature exists to answer pre-loaded nothing. Both cues are there now.

## Registration

- **Group `self`** in `agent/toolset.py`, holding all three tools.
- **`GROUP_KEYWORDS["self"]`** pre-loads the group so the model usually skips
  the hop. The matcher is `\b` + the cue, so **every cue matches as a prefix**.
  Two were removed after the false-positive corpus caught them: `set up` fired
  on "set up a meeting with John", and a bare `setting` fired on "setting up a
  call with the insurance adjuster" — ordinary calendar asks. `settings`
  (plural) and `setup` (one word) replaced them. Do not shorten a cue without
  re-running `tests/test_toolset.py`.
- **There is deliberately no bare `wiki` cue here.** `GROUP_KEYWORDS["wiki"]`
  already owns that word for `search_wiki`. The two tools answer different
  questions off similar names: `search_wiki` reads the vault's **contents**
  ("what did I decide about pricing"), `search_docs` reads the engine's
  **documentation** ("how does a note become a page"). Pre-loading both on a
  bare "wiki" puts two similar-sounding search tools in front of a small model
  on the one ask where picking wrong is invisible. **Cue on the process, never
  on the store** — hence `wiki agent`, `wiki lint`, `how does the wiki`, `how do
  notes`, `ingest`, `obsidianwikiagent`. `journaling` is in for the same reason
  and a bare `journal` is out: "what did I write in my journal" is a vault ask.
  `tests/test_toolset.py` has a test in each direction so nobody adds the bare
  cue in six months without something complaining.
- **Policy sets**: all three are read-only, so none appears in `WRITE_TOOLS`,
  `CONSEQUENTIAL_TOOLS` or `UNATTENDED_EXCLUDED_TOOLS`. All three **are** in
  `MAIL_JOB_SAFE_TOOLS`, which is a safe list, not a deny list — an
  unclassified tool is gated on mail jobs by default.
- **`chat/insights.py:TOOL_SERVICES`** gets a `self` node, or the tools draw on
  `/map` in a nameless "other" bucket.

## Files

| File | What it is |
|---|---|
| `agent/tools/setup_info.py` | `describe_setup` |
| `agent/tools/docs.py` | `search_docs`, `read_doc`, plus an unregistered `list_docs` for humans |
| `tests/test_setup_info.py` | The feature, and the credential boundary |
| `tests/test_docs.py` | The corpus boundary, scoring, section matching, the trim, and the sibling namespace |
| `tests/conftest.py` | `_isolate_sibling_doc_repos` pins both sibling roots at paths that do not exist, so the suite reads the same on a clean box as on this machine |
