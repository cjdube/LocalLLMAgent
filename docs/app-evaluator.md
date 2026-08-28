# The app evaluator — how it works

Ask Wren to "tear down" or "evaluate" a product by URL and it returns a
skeptical, VC-style strategic analysis of what the marketing site actually
claims: overall viability, hidden risks, adoption friction, and the technical
constraints the copy glosses over. Ported from the standalone
`~/Projects/app-evaluator` script into Wren's idiom.

Code: `agent/tools/web_fetch.py` (the general page-fetch tool),
`agent/tools/evaluate_app.py` (the teardown pipeline).

## The two tools

- **`fetch_webpage(url)`** — general-purpose: fetches one page via the
  [Firecrawl](https://www.firecrawl.dev/) API and returns its readable content
  as markdown (HTML boilerplate, nav, and scripts stripped). Complements
  `search_web`: search finds pages, this reads one. The markdown is capped at
  `WEB_FETCH_MAX_CHARS` (default 8000, matching the agent loop's tool-result
  cap) so the tool decides its own cut.
- **`evaluate_app(url)`** — the teardown. A fixed pipeline, not a freeform
  agent task (small-local-model constraint, see `AGENTS.md`): Python fetches
  the page with `fetch_webpage`, compacts the markdown deterministically
  (images and link targets stripped, whitespace collapsed, bounded to ~6000
  chars), and the model writes one analysis against a fixed four-section
  template — Overall Assessment / Hidden Risks / Adoption Friction / Missing
  Technical Constraints. No JSON schema to parse, nothing to break.

Both are read-only against the outside world, so they're ungated (no
tap-to-confirm), like `search_web`. Fetched page text is untrusted input: the
system prompt tells the model to ignore any instructions inside it, and the
existing `WRITE_TOOLS`/`CONSEQUENTIAL_TOOLS` gates remain the backstop.

A teardown takes a minute or two (page render + a large local generation), so
Wren may offer to run it via `run_in_background` and push when done.

## Setup

Set `FIRECRAWL_API_KEY` in `config/.env` (free key at
[firecrawl.dev](https://www.firecrawl.dev/); the free tier's credits are ample
for occasional teardowns). Errors — missing key, out of credits, a page
Firecrawl can't render — come back as the uniform `{"error": ...}` shape and
never crash a chat turn.

## Trying it standalone

```bash
.venv/bin/python -m agent.tools.web_fetch --url https://example.com
.venv/bin/python -m agent.tools.evaluate_app --url https://some-startup.com
```

## Deliberate non-goals

- **Not wired into any scheduled/unattended task.** This is a user-initiated
  capability; per the data sourcing policy, Firecrawl fetches only URLs the user
  names in chat, and doesn't become a background scraping pipeline.
- **No structured (JSON) model output.** The original script forced a Pydantic
  schema through Ollama's `format` parameter; Wren asks for fixed markdown
  headings instead — line-oriented output the chat renders directly, with no
  parser to fail.
