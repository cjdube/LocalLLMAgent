# Daily synthesis — how it works

Wren's first *proactive* routine. Every other scheduled task reads one source
and produces one artifact; this one connects yesterday's activity to what Craig
already knows and nudges him when they line up — e.g. *"You dug into DuckDB
yesterday — it fits your 'local-analytics' note; want a summary?"*

Runs daily at 5:45 AM, after the learnings tasks (5:05 / 5:15) have populated the
day's signals. Nothing is persisted and nothing consequential is done — the only
output is an optional ntfy push — so there is no store to isolate and no
confirmation gate.

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
   copy written to the vault's `raw/` as `Daily-Synthesis-<date>.md` via
   `persist_or_email` — flat, exactly like the learnings tasks, so
   ObsidianWikiAgent files it downstream and Wren needs no knowledge of any
   subdirectory. The push is the live channel; the file is the record Craig
   scrolls back through later. No overlap, or nothing genuine, means **no push
   and no file** — silence is the common case.

## Tuning

- `MAX_CANDIDATES` / `MAX_NUDGES` / `_MIN_TOKEN_LEN` / `_STOPWORDS` in
  `tasks/daily_synthesis.py`.
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
