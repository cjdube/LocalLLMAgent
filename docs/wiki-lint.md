# /wiki/lint — reviewing the learnings wiki audit

The learnings vault is audited by `wiki_lint.py` in the sibling
[ObsidianWikiAgent](https://github.com/cjdube/ObsidianWikiAgent) repo: nine
structural checks over every page, plus an opt-in model pass. It runs on a
schedule (Sundays 10:00) and prints a prose report to a launchd log.

That report was effectively unreadable. It lives in a 400 KB append-only file on
the Mac mini, it can only be re-run from a terminal, and it is a week old by the
time anything acts on it. `/wiki/lint` is the same audit, on demand, on a phone.

## What it shows

One collapsible card per check, in the order `structural_findings()` returns
them. Findings render open; **clean checks still render**, collapsed, reading
`0 — clean`. The command line prints only what it found, which leaves you unable
to tell a check that passed from a check that never ran — the counts are the
whole point of a clean vault.

Each finding opens with the page it is about (`orphan.md is an orphan — …`), and
every check in the sibling repo writes them that way. The view parses that
leading slug and turns it into two actions: **peek**, which reads the page inline
via `/api/wiki/page/<name>`, and **graph**, which opens `/wiki?page=<slug>`.

## Apply safe fixes

The one write path Wren has into the vault. It runs the sibling's
`apply_safe_fixes`, which touches only what is mechanically provable:

- self-links, flattened to their display text
- dead `index.md` entries, de-linked

Everything requiring judgment — orphans, duplicate concepts, invented citations,
bad dates — is left alone by design. The button is hidden unless *Broken and self
links* or *Index integrity* actually has findings, so it cannot be clicked into a
no-op that still writes. It confirms first, and the applied changes are logged at
INFO in `logs/wren.log`: a vault edit with no record of who made it is
indistinguishable from an ingest bug.

## How it runs

```
GET /api/wiki/lint  →  chat/wikilint.py
  →  <WREN_WIKI_LINT_ROOT>/.venv/bin/python wiki_lint.py --vault … --json
```

Three things about that seam are load-bearing.

**Subprocess, never import.** Both repos have a top-level `agent` package.
Importing across the seam would shadow one with the other. The sibling's own
interpreter runs its own code with its own dependencies, so a version skew
between the repos is invisible here.

**`--json` deliberately logs nothing.** The prose path calls `setup_logger`,
which writes `logs/wiki_lint.<vault>.log` — the file Wren's own dashboard parses
for that job's run history (see [external-tasks.md](external-tasks.md)). A button
press that logged `Starting wiki lint run` would fabricate a scheduled run that
never happened, and one that crashed mid-write would leave a run `log_inspector`
reports as started-and-never-finished. So `--json` returns before the logger is
built, and pushes no failure alert either.

**Exit code 1 means findings, not failure.** An audit that finds problems has
worked. `chat/wikilint.py` parses stdout first and only consults the exit code
when the output will not parse.

Findings are cached on the vault's `wiki/` mtimes, so the page can poll and
re-check freely; editing a page in Obsidian invalidates the cache for free. A
`--fix` run always executes and always replaces the cached entry.

Every failure — missing checkout, missing virtualenv, timeout, unparseable output
— comes back as `{"error": …}` with a 200. The view renders the error as its own
state, which is more useful than a blank page behind a 500.

## What it does not do

The `--deep` model pass is refused here. It is a multi-minute Gemini
conversation, which is not something a page load can wait on. It still runs
Sundays; read it in the launchd log or through `/logs`.

## Checking it without the browser

```bash
.venv/bin/python -m chat.wikilint
```

Prints the page count and a per-section tally, the same data the view renders.
