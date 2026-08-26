# Evaluation lenses — how they work

A **lens** is a wiki page the user writes that captures their own standards for a
class of thing — his product principles, say — and that Wren judges targets
against. `evaluate_app` reviews a product against a generic VC rubric baked
into its prompt; `evaluate_against` reviews anything against *the user's* rubric,
loaded from the vault at call time. When the standards change, he edits a
Markdown file — no code change, no restart.

Code: `agent/tools/wiki.py` (discovery and the prompt index),
`agent/tools/evaluate_against.py` (the evaluation pipeline),
`chat/server.py` (injects the index each turn).

## The marker contract

A lens is an ordinary page in the vault's `wiki/` directory that opts in via
YAML frontmatter. Nothing else distinguishes it from the ~200 concept pages
ObsidianWikiAgent generates, so the marker is the whole mechanism:

```markdown
---
lens: true
description: the user's product & engineering standards — the lens for evaluating products, features, and pitches (used by evaluate_against)
---

# Product Principles
...
```

What the parser actually requires (`_lens_meta`):

- The frontmatter block opens at the **very top** of the file (`---` … `---`).
- `lens: true` on its own line inside it, case-insensitive. A page with
  frontmatter but no marker is not a lens — a `description:` alone is ignored.
- `description:` is optional but wanted: it's the only thing Wren sees about
  the lens before choosing it, so write it as *when to reach for this lens*,
  not just what the page is about.

Only the first 2048 bytes of each page are read to classify it, so the marker
must be at the top — which frontmatter is, by definition.

## Discovery and the prompt index

`list_lenses()` head-reads every wiki page and returns the marked ones as
`{name, description}` rows. `render_lenses_index()` turns those into a capped
block that [chat/server.py](../chat/server.py) appends to the system prompt
**every turn** — so a lens added mid-session is live on the next turn, same as
the skills index.

The caps exist because the chat prompt already crowds `num_ctx`: at most
`MAX_INDEX_LENSES` (8) lenses and `MAX_INDEX_CHARS` (600) characters, whichever
binds first.

**In practice the character cap binds long before the lens count**, and it's
worth knowing which one you're actually up against. The three lenses in the vault
today render at 211, 146, and 119 characters — 476 of the 600-char budget, with
room for roughly one more short description. So four or five descriptions of that
length exhaust the budget, not eight. Writing lens descriptions at that length
silently lowers the real cap to well under `MAX_INDEX_LENSES`.

The truncation is a `break`, not a `continue`: the first line that would overflow
ends the list, so every lens after it disappears from the prompt. Those lenses
still work when named explicitly (in the CLI, or if the user names the page in
chat) — Wren just won't know to offer them.

That would be a silent degrade of exactly the kind `CLAUDE.md` warns about, so
it isn't silent: `render_lenses_index(logger)` logs a WARNING naming which cap
bit, how many of how many lenses made it, the character total, and the dropped
page names. `chat/server.py` passes its logger; the logger is optional so the
CLI path can't break on it. The index is rendered per turn, so a standing drop
warns every turn — `log_inspector` is default-open on WARNINGs and collapses
identical ones into a count, so this surfaces as a daily finding rather than a
push per chat turn.

The cheap fix is short descriptions. A description only has to answer *when do I
reach for this lens* — around 80 characters is plenty, and keeps the count cap
the binding one. Raising `MAX_INDEX_CHARS` is the other lever, but it spends
context every turn, on every turn that never evaluates anything.

The index names each lens by its **exact page slug**, which is what
`evaluate_against` takes as `lens_page` — the model picks from a list rather
than transcribing a guess (see the "never make the model copy an opaque
identifier" rule in `CLAUDE.md`). If no listed lens fits the request, the tool
description tells Wren to ask which lens rather than pick one.

A missing or misconfigured vault degrades to *no lenses* rather than raising —
this feeds the prompt build, which must never break a chat turn.

## The evaluation pipeline

`evaluate_against(lens_page, target_url=... | target_text=...)` is a fixed
pipeline, not a freeform agent task (small-local-model constraint, see
`CLAUDE.md`):

1. Load the lens page from the vault. Missing or empty → `{"error": ...}`.
2. Resolve the target: fetch a URL via `fetch_webpage` (Firecrawl), or take
   inline text.
3. Compact both deterministically and bound them — the lens at **12000** chars,
   the target at **16000**, fetched at **20000** so compaction has headroom.
   Truncating the lens mid-list would silently drop standards the target should
   be judged against, so it must arrive whole.
4. One model call against a fixed template — a **Verdict** line, then three
   headings, **Where It Aligns / Where It Falls Short / What I'd Change**, 2–4
   bullets each, each point naming the standard it turns on. No JSON to parse,
   nothing to break.

### The bounds, and the three that used to stack

Those numbers are generous because this call is **not** the chat. `complete_text`
sends two messages with no conversation behind them, so the whole
`OLLAMA_NUM_CTX` (49152 tokens since 2026-08-26) belongs to this one call. Lens
+ target + system prompt at the current bounds is roughly 7K tokens — a seventh
of the window.

They read as one bound each now. They did not before. A page passed through
**three** caps on its way to the model, two of them invisible at the call site:

| Order | Cap | Where |
|---|---|---|
| 1 | 8000 | `web_fetch.MAX_CHARS` — sized for the *agent loop's* tool-result budget |
| 2 | 6000 | `evaluate_app._compact`, imported here for its regex work |
| 3 | 5000 | `_TARGET_CHARS`, the only one this module declared |

Two consequences, both silent:

- **`_LENS_CHARS = 8000` never ran.** `_compact`'s 6000 bound the lens first, so
  the "the lens must arrive whole" promise in the code was false for any lens
  over 6000 chars. `engineering-manager-expectations` compacts to 8281 and had
  been losing its tail on every run since it was written.
- **A target was cut to roughly the first half of an article** — and the half it
  lost was the *end*, where two of `ai-slop`'s own patterns live
  ("summary-recap endings", "fake-profound kickers"). Asked whether a Medium
  article was slop, the model reported a summary-recap ending it could not see,
  hedged as *"the structure suggests a buildup toward…"*. A cap smaller than the
  documents a lens is written to judge doesn't bound cost; it manufactures
  findings.

The fixes, in the order they matter:

- `_compact` **shrinks and does not bound**. Every cap is now visible at its own
  call site; `evaluate_app` slices at `_CONTENT_CHARS` itself.
- `fetch_webpage` takes an in-process `max_chars`, deliberately **not** in its
  `TOOL_SCHEMA` — `MAX_CHARS` defends the agent loop's context, which isn't the
  model's to raise, and `loop.py` trims every tool result regardless.
- `_LENS_CHARS` is **12000**, not 8000. 8000 wasn't headroom over a real 8281-char
  lens, it was the next silent truncation.

Measured after, on the same article at n=3: it fetches whole (15694 chars,
untruncated), 11323 reach the model, and all 3 runs name and quote the article's
actual closing section instead of inferring one.

### Truncation is declared, never silent

Raising the bounds shrinks the problem; it doesn't remove it. Something will
always be longer, so the remaining question is what the model does with a
document it can only half see — and the answer was: report on the half it
couldn't. Asked whether a Medium article was slop, it produced a
*summary-recap endings* finding, hedged as "the structure suggests a buildup
toward…". A finding shaped exactly like a real one, about text never sent.

That's the bad kind of degrade (`CLAUDE.md`): not a crash, not an empty answer,
but a *confident* one that reads as complete. Three parts fix it, because the
model and the reader each need telling:

- **Detect both cuts.** The fetcher's own truncation and our `_TARGET_CHARS`
  slice. Length alone can't spot the first — a long Wikipedia page fetched at
  `_FETCH_CHARS` compacts to 12613 chars, *under* `_TARGET_CHARS`, and looks
  whole. `fetch_webpage` already reported `truncated: True`; this tool used to
  drop it on the floor.
- **Mark it in the prompt**, and tell the model in the system prompt that a
  finding about how a TRUNCATED target ends is *always* false — the missing text
  is our cut, not the author's omission.
- **Append the note in Python**, not the model: `_Judged on the first N
  characters…_`, plus `truncated: True` in the returned dict. How much was read
  is a fact we hold and the model doesn't (deterministic Python owns structure).
  Silencing the model fixes the false findings but leaves the report *looking*
  complete; the note is what stops the reader assuming it is.

Measured at n=3 on an over-long draft: the flag, the note, and **zero** findings
about the ending — where the lens's own `summary-recap endings` and
`fake-profound kickers` patterns were exactly the bait. The article that now
fits whole reports neither flag nor note, and still flags its real ending.

The log line is INFO, not WARNING. Cutting a very long document is the design
working, and `log_inspector` is default-open on WARNINGs — the thing that must
not be silent is the *evaluation*, and the note is what makes it loud.

### The verdict line

The three headings answer *what did you find?* and never *so what?*. Asked
outright whether an article was AI slop, the template returned a findings list
and left the yes/no to the reader — the question the user actually asked went
unanswered, in a response shaped exactly like an answer.

The fix is one line ahead of the headings:

```
**Verdict:** <Meets the standards|Mixed|Falls short> — one sentence giving the main reason.
```

A fixed three-label menu rather than a free sentence, for the same reason the
headings are fixed: the label *is* the answer, and the model can't retreat into
a paragraph that restates the question. The verdict judges the target against
the lens and nothing else — notably not who or what wrote it, which the
`ai-slop` lens forbids from its own page.

Measured at n=3 per case on `ai-slop`: a slop-saturated draft returned **Falls
short** 3 of 3, a clean draft **Meets the standards** 3 of 3, and the
`product-principles` lens on a real landing page returned **Mixed** — so the
middle label is doing real work rather than absorbing every case. The
"Nothing significant" escape hatch below still fired on both poles.

### The deterministic pre-pass

Some of what a lens wants is counting and exact matching, not judgement. A lens can opt
into having those computed in Python via two optional frontmatter keys, handled by
[agent/tools/prose_checks.py](../agent/tools/prose_checks.py):

```yaml
---
lens: true
description: ...
max_em_dashes_per_sentence: 1
banned_phrases: paradigm shift, cutting-edge, game changer
---
```

Either key alone is enough. A lens with neither (`product-principles`,
`engineering-manager-expectations`) gets no block and behaves exactly as before. No lens is
named in the code — the opt-in lives in the vault, same as the `lens: true` marker.

When enabled, the results are injected ahead of the target as an authoritative block, and
the system prompt tells the model to report them without re-deriving or contradicting them.
The checks run on the **compacted, truncated** target, so a reported finding can never quote
text the model can't see.

Matching folds typographic apostrophes and quotes onto their ASCII forms. A lens is typed
with `'`; a fetched page renders it as `’` (U+2019). Exact substring matching missed every
apostrophe phrase, found nothing, reported **"none"**, and looked identical to a clean
draft — and "none" is the line the model is told it cannot contradict, so the failure was
doubly silent. No phrase had an apostrophe until `ai-slop`'s empty-phrase list moved into
the frontmatter, so nothing had ever exercised it.

Two details are load-bearing, both measured:

- **A passing check says "none" out loud** rather than staying silent. Silence is what let
  the model invent an em-dash finding — it quoted a comma-heavy sentence containing no em
  dashes and called it a cluster. It cannot contradict an explicit "none".
- **One line per finding, never a grouped list.** With `- Banned phrases present: "paradigm
  shift", "cutting-edge".` on one line, the model forwarded only *one* of the two, in 3 of 3
  runs. Split onto its own indented line each, both came through 3 of 3.

Measured on `ai-slop` before and after, at n=3 per case: em-dash detection went from 3 of 3
with an intermittent false positive on clean prose, to 3 of 3 with **zero** false positives
and both offending sentences caught rather than one; exact-phrase detection went from 1–2 of
3 to **3 of 3**. The model no longer does either check, so there is nothing left to fabricate
or crowd out.

A section with nothing to report gets **"Nothing significant"** rather than
bullets. That escape hatch is load-bearing: a fixed template with a bullet quota
compels the model to fill every heading, so a target that genuinely *meets* the
lens gets invented faults. Measured on the `ai-slop` lens against a clean draft —
3 of 3 runs manufactured findings, one of them recommending a change that would
have made the draft worse. A lens page can't fix this from its own text (the
output contract outranks anything the lens says about not over-correcting), so
the permission has to live in the system prompt. It cuts both ways, which is the
point: on a draft that is all slop, **Where It Aligns** comes back empty too.

The call passes `think=False`. Judging a target against standards that are
*both already in the prompt* is comparison, not chain-of-thought; with thinking
on, 1 run in 3 spent the whole budget on scratchpad and returned a blank
evaluation, and turning it off made the output cite the lens's standards more
specifically. An empty response is reported as an error rather than passed
through. See `CLAUDE.md` and commit `b273d1c`.

Runtime is a minute or two (page fetch + a large local generation), so the tool
description tells Wren to offer `run_in_background` when it isn't needed
immediately.

## Trust boundary

The lens is the user's own note — trusted. The target is untrusted web or pasted
text, so the system prompt tells the model to ignore any instructions inside it
and evaluate only the thing it describes. The tool is read-only (it writes
nothing and sends nothing), so it's ungated, like `evaluate_app` and
`search_web`; the `WRITE_TOOLS` / `CONSEQUENTIAL_TOOLS` gates remain the
backstop for anything downstream.

## Authoring a lens

A lens answers: *what do I consistently believe about this class of thing, that
I don't want to re-explain every time?* If the same standards get pasted into
chat twice, that's a lens.

- **Write it in your own voice, as assertions.** The model is told to judge
  against your stated standards and not against generic best practice — vague
  principles produce vague critiques.
- **Sectioned prose works well.** `product-principles.md` uses headings like
  "Scope discipline", "What 'done' means", and "Red flags (call these out)" —
  the last is worth copying, since naming failure modes gives the model
  something concrete to match against.
- **Keep it under ~12000 characters** of compacted text, or the tail is cut.
  The longest lens today (`engineering-manager-expectations`) is 8281.
- **If a check is a count or an exact match, put it in the pre-pass, not in the prose.**
  This is the lesson the two frontmatter keys came out of, and it's worth reaching for
  before tuning wording. The model saturates at roughly a dozen findings and then
  *substitutes* rather than adds, so low-salience mechanical checks lose to the obvious prose
  problems: an em-dash rule and an exact-phrase list buried in the pattern list fired in 1 of
  3 runs each. Moving them to the top of the page got them to 3 of 3 and 2–3 of 3, but at the
  cost of a fabricated finding. Computing them in Python got both to 3 of 3 with no
  fabrication, and shortened the lens.
- **The prose list doesn't just get missed — it gets *recited*.** `ai-slop`'s "Usually empty
  phrases" list stayed in prose longest, and on a short draft it worked fine: 5 of 5 phrases
  caught, 3 of 3 runs. The failure only appears on a long, dense target where the model is
  saturated. On a real 11400-char article containing **none** of the twelve listed phrases,
  3 of 3 runs asserted one as a finding, quoted, under **Where It Falls Short** — reading
  them off the lens rather than the target. Moving the list to `banned_phrases` took that to
  0 of 7; what remains is the odd *"and similar phrases"* aside in **What I'd Change**,
  anchored to a phrase the article really contains. Measure a prose rule on a saturated
  target, not a short one — a short draft will tell you it's fine.
- **Every rule you add destabilises the ones already there.** This is the counterweight to
  the point above, and it bit repeatedly while authoring `ai-slop`. Adding a doc-type
  exemption to the over-correct section knocked the exact-phrase check from 3 of 3 down to 1
  of 3 and introduced a *fabricated* em-dash finding — the model quoted a comma-heavy
  sentence containing no em dashes at all and called it a cluster. Tightening "stay silent if
  the check passes" made that worse, not better: it removed the model's habit of
  self-correcting mid-bullet and left it committing to the false claim instead. Budget rules
  like a scarce resource, change one thing at a time, and re-measure the *old* wins too.
- **Three runs cannot tell 2 of 3 from 3 of 3.** Read a single run's difference as noise
  until a config repeats. Most of the tuning above was measured at n=3, which is enough to
  catch 0 of 3 versus 3 of 3 and not much finer.
- **A rule the model can't apply reliably is worth deleting, not tuning.** `ai-slop`
  originally flagged "bullets where two sentences of prose would read better". That produced
  false positives on the user's own reference docs (whose bold-lead-in bullet lists are the
  intended format) and never once caught the case it was for — a bullet-crutch list in a
  narrative draft went unflagged in 0 of 6 runs across two different formulations. Deleting
  the clause cost nothing real and removed the false positives outright. Prefer that to a
  third attempt at wording.
- **It's hand-authored, not ingested.** A lens lives in `wiki/` alongside
  ObsidianWikiAgent's generated pages but has an empty `**Sources**:` line by
  design — it's the user's assertion, not a summary of anything.

Current lenses: `product-principles`, `engineering-manager-expectations`,
`ai-slop`.

`ai-slop` is the odd one out and worth reading as a second pattern: it judges
*writing* rather than a product or a role, and it's detection-only — it names the
pattern, quotes the offending line, and stops. It carries an explicit "Don't
over-correct" section, because the failure mode for a prose lens is flagging a
writer's real voice as a defect. Adapted from the `no-ai-slop` Claude Code skill,
condensed to the patterns a small local model applies reliably; the skill's
*rewrite* half was deliberately left out (see non-goals).

## Using it

In chat, in natural language — "evaluate this landing page against my product
principles", "critique this RFC against my standards". Wren maps that to the
right `lens_page` from the injected index.

Standalone:

```bash
.venv/bin/python -m agent.tools.evaluate_against --lens product-principles --url https://some-startup.com
.venv/bin/python -m agent.tools.evaluate_against --lens product-principles --text "our pitch ..."
```

## Deliberate non-goals

- **No lens registry, no code per lens.** Adding one is writing a Markdown file
  with the marker. This is why the marker is frontmatter rather than a
  hardcoded page name — the design assumed several lenses from the start
  (`engineering-standards`, `hiring-bar` were the motivating examples), and
  three exist today.
- **Not wired into any scheduled task.** Like `evaluate_app`, this is
  user-initiated: it fetches only URLs the user names, and doesn't become a
  background pipeline.
- **No structured (JSON) model output.** Fixed markdown headings the chat
  renders directly, with no parser to fail.
- **A lens critiques; it never rewrites.** The `no-ai-slop` skill this borrows
  from has an edit mode that returns a revised draft. That half stayed out on
  purpose: "return the full edited draft" is a *document*, and the local model
  writes blurbs and scores, not documents (`CLAUDE.md`). A small model asked to
  rewrite prose while preserving voice will flatten the voice — the exact failure
  the lens exists to catch, and it would degrade silently. Rewriting is what the
  frontier-escalation button is for.
