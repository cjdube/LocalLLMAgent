# Daily commits — what got shipped

`scribejay/daily_commits.py`, daily at 4:55 AM. Reads yesterday's commits out of
the local checkouts under `PROJECTS_DIR`, has the model group them into a short
"what I built" page, and writes `Daily-Commits-<date>.md` into `LEARNINGS_DIR`.

**Why it exists.** The rest of the record covered time and reading: Claude Code
hours as calendar blocks, browsing and Likes as daily pages. Nothing said what
was actually *made*. Git is the only source that does, and unlike every other
integration here it needs no API, no token and no network — which is also why it
cannot be rate-limited, deprecated, or fall foul of a terms of service.

## What it reads

`scribejay/git_activity.py` shells out to `git log` in each checkout one level
under `PROJECTS_DIR`. Per commit: subject, ISO timestamp, the paths changed, and
the insertion/deletion counts.

Scope is deliberately narrow:

| Choice | Why |
|---|---|
| `HEAD` only, not `--all` | Commits go straight to `main` here. Scanning every ref folds in fetched branches and rebase duplicates for commits nobody made that day. |
| `--no-merges` | A merge commit's subject describes bookkeeping, not work. |
| Author-filtered | A shared checkout's other contributors are not the user's day. |
| One level under `PROJECTS_DIR` | The same shallow scan `tasks/project_scan.py` does. A nested monorepo checkout is not found. |

**Whose commits count.** `SCRIBEJAY_GIT_AUTHOR`, else the machine's global
`git config user.email`. Set it explicitly when the identity on the commits
differs — a work address, say. With neither resolvable there is **no author
filter at all** and every contributor's commits become the user's; the run logs a
WARNING when it resolves that way, because the symptom is otherwise just a page
that reads slightly wrong.

## What the model is asked for

Very little, on purpose. The commit subjects in these repos are written as
sentences ("Watch mail because I sent it, not because a stranger typed [wren]"),
so the draft is mostly a grouping job:

- **Several commits are usually one piece of work.** Commits an hour apart over
  the same paths are one feature being finished, and get one bullet.
- **The paths are the evidence the subject line doesn't carry.** `tests/` touched
  means it was tested; `docs/` or a README means it was documented; a new file
  under `agent/tools/` is a capability rather than a fix.
- Two sections, `### What I Built` and `### Also`, with the same `**None:**`
  empty-section marker the other journaling tasks use.

**The totals line is computed in Python**, not asked for — `commit_totals_line`
in `scribejay/journal.py`. Arithmetic is never the small model's job
([model-constraints.md](model-constraints.md)), and the line doubles as a check
on the draft: bullets claiming a big day under a two-commit total are visibly
wrong.

## Bounds

Three caps, in `scribejay/git_activity.py`. Each one logs a WARNING whenever it
actually drops something, because a silently shortened prompt produces a thinner
page and nothing alerts on it:

| Cap | Default | Cut first |
|---|---|---|
| `MAX_COMMITS` | 40 | last — whole commits |
| `MAX_FILES_PER_COMMIT` | 12 | first — the file list, subject and counts survive |
| `MAX_PROMPT_CHARS` | 12000 | every file list at once |

`files_total` always reports the real count, so a trimmed list still reads as
"22 files" rather than as twelve.

The char budget is not redundant with the count cap: 40 commits × 12 deeply
nested paths is 20k characters of prompt while both count caps are satisfied.

## Behavior

**A day with no commits writes nothing** and never wakes the model. This is
normal — weekends, travel, a day spent reading.

**An empty draft on a day that *had* commits logs a WARNING.** The quiet-day case
already returned before the model ran, so an all-`None` draft here means the model
failed, not that the day was quiet.

**A failed vault write emails the draft** and pushes a phone alert, like the other
journaling tasks (`persist_or_email`).

**A broken checkout is skipped, not fatal.** One repo whose `git log` fails logs a
WARNING and contributes nothing; the rest of the day still gets written.

## Running it by hand

```bash
.venv/bin/python -m scribejay.daily_commits
```

## Related

- [docs/scribejay.md](scribejay.md) — the agent this belongs to, and its model dial
- [docs/daily-learnings.md](daily-learnings.md) — the reading half of the same daily record
- [docs/projects.md](projects.md) — the other consumer of `PROJECTS_DIR`
