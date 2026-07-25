# Evaluation lenses — how they work

A **lens** is a wiki page Craig writes that captures his own standards for a
class of thing — his product principles, say — and that Wren judges targets
against. `evaluate_app` reviews a product against a generic VC rubric baked
into its prompt; `evaluate_against` reviews anything against *Craig's* rubric,
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
description: Craig's product & engineering standards — the lens for evaluating products, features, and pitches (used by evaluate_against)
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
binds first. Past 8 lenses, the rest are invisible to chat — they still work if
named explicitly via the CLI, but Wren won't know to offer them.

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
3. Compact both deterministically and bound them — the lens at **8000** chars,
   the target at **5000**. The asymmetry is deliberate: truncating the lens
   mid-list would silently drop standards the target should be judged against,
   so it gets the generous budget; the target is the disposable input.
4. One model call against a fixed three-heading template — **Where It Aligns /
   Where It Falls Short / What I'd Change**, 2–4 bullets each, each point
   naming the standard it turns on. No JSON to parse, nothing to break.

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

The lens is Craig's own note — trusted. The target is untrusted web or pasted
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
- **Keep it under ~8000 characters** of compacted text, or the tail is cut.
- **It's hand-authored, not ingested.** A lens lives in `wiki/` alongside
  ObsidianWikiAgent's generated pages but has an empty `**Sources**:` line by
  design — it's Craig's assertion, not a summary of anything.

Current lenses: `product-principles`.

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
  (`engineering-standards`, `hiring-bar` were the motivating examples), even
  though only one exists today.
- **Not wired into any scheduled task.** Like `evaluate_app`, this is
  user-initiated: it fetches only URLs Craig names, and doesn't become a
  background pipeline.
- **No structured (JSON) model output.** Fixed markdown headings the chat
  renders directly, with no parser to fail.
