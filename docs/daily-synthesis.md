# Daily synthesis — how it works

Wren's first *proactive* routine. Every other scheduled task reads one source
and produces one artifact; this one connects yesterday's activity to what Craig
already knows and nudges him when they line up — e.g. *"You dug into DuckDB
yesterday — it fits your 'local-analytics' note; want a summary?"*

Runs daily at 5:45 AM, after the learnings tasks (5:05 / 5:15) have populated the
day's signals. The live output is an optional ntfy push, archived to
`SYNTHESIS_DIR`; nothing consequential is done, so there is no confirmation gate.

Code: `tasks/daily_synthesis.py`. Launchd: `launchd/com.craigdube.localllmagent.dailysynthesis.plist`.

## The pipeline

Deterministic Python owns the structure; the model only judges a short,
pre-matched list (the [small-local-model constraint](../CLAUDE.md) — a small
model told to "find connections across everything" manufactures them):

1. **Gather signals** — yesterday's activity, each source guarded so a dead one
   contributes nothing rather than crashing the run:
   - Chrome browsing (`fetch_chrome_history` + `compact_sites`) — site titles,
     domains, and page paths.
   - YouTube Likes (`fetch_liked_videos`) — video titles.
2. **Gather anchors** — Craig's existing world:
   - Wiki page names (`list_wiki_pages`).
   - Companies he watches or has marked *interested* (`get_watchlist`,
     `list_opportunities`).
3. **Match (Python)** — pair each signal with each anchor that shares a token
   (`candidate_pairs`), scored by overlap size, best first, capped at
   `MAX_CANDIDATES` (8). Tokens are lowercase, length ≥ 4, minus a small
   stopword list — enough to trim obvious noise; the model is the real filter.
4. **Judge (one bounded model pass)** — the shortlist goes to the model, which
   keeps only the genuine, non-obvious, actionable connections and writes one
   nudge line each, at most `MAX_NUDGES` (3). A reply of `NONE`, or no bullet
   lines, yields nothing.
5. **Push + archive** — if any nudges survive, one `notify()` with
   `email_fallback=True` (a one-shot alert nothing retries), **and** a durable
   copy written as `Daily-Synthesis-<date>.md` to `SYNTHESIS_DIR` (default
   `<vault>/nudges`) via `persist_or_email`. The push is the live channel; the
   file is the record Craig scrolls back through later. No overlap, or nothing
   genuine, means **no push and no file** — silence is the common case.

## Why the archive is not in `raw/`

It was, for two days (2026-07-23 and 07-24), and that was a category error worth
recording. `raw/` is ObsidianWikiAgent's ingest queue: every file it finds there
is treated as a *source* and written up as a wiki page. But a nudge is a question
addressed to Craig, not a source. The ingest agent dutifully converted

> "You were exploring LM Studio for local agents—is this worth adding to your
> 'claude-code-agent-architecture' research?"

into the declarative claim *"The current focus involves exploring [[lm-studio]]
… within the research surrounding [[claude-code-agent-architecture]]"* — a
fabrication by restatement, on a dated orphan page whose only content pointed at
two pages already in the graph.

So the archive lives in a sibling directory the ingest agent doesn't walk. It is
still inside the vault (Obsidian browses it fine); it just isn't a source. A
`SYNTHESIS_DIR` pointed back at `raw/` reintroduces the bug — hence the guard
test `test_archive_goes_to_synthesis_dir_not_learnings_dir`.

## Tuning

- `MAX_CANDIDATES` / `MAX_NUDGES` / `_MIN_TOKEN_LEN` / `_STOPWORDS` in
  `tasks/daily_synthesis.py`.
- `SYNTHESIS_DIR` — where the nudge archive is written (see below). Must exist;
  a missing dir emails the nudges instead.
- `WREN_DAILY_SYNTHESIS_BACKEND` routes just this task to a cloud model
  (default: the global backend, i.e. local Ollama). The prompt is small, so the
  local model handles it fine; override only if you want it on the cloud.

## Relationship to vault RAG

This token-overlap matching is a deliberate stand-in for real semantic
retrieval over the vault (the deferred "vault RAG" in
[docs/frontier-escalation.md](frontier-escalation.md)). It catches lexical
overlaps ("DuckDB" ↔ a page named `duckdb-analytics`) but not conceptual ones
("columnar store" ↔ "DuckDB"). When vault RAG lands, the candidate-generation
step here is the natural place to swap it in.
