# Daily commits — what got shipped

`scribejay/daily_commits.py`, daily at 4:55 AM. Writes `Daily-Work-<date>.md`
into `LEARNINGS_DIR` from two sources: yesterday's commits in the checkouts under
`PROJECTS_DIR`, grouped by the model into a short "what I built" page, and the
ClickUp Tasks that reached a Done status that day, listed by Python.

**Why it exists.** The rest of the record covered time and reading: Claude Code
hours as calendar blocks, browsing and Likes as daily pages. Nothing said what
was actually *made*. Git says it for code, needs no API and no token, and cannot
be rate-limited or fall foul of a terms of service.

**Why git alone was not enough.** Git is only a witness to work that touches a
repository. The Vibe Foundry Space holds business and contract work; the Blog
Space holds research and content. Neither advances by a commit, so before the
ClickUp source those days read as quiet ones — and nothing alerted, because
nothing had failed. See [The ClickUp half](#the-clickup-half) below.

## The ClickUp half

`closed_tasks(day)` in `agent/tools/clickup.py`, rendered by
`closed_tasks_section()` in `scribejay/journal.py`.

| Choice | Why |
|---|---|
| **`date_closed`, never `date_updated`** | Editing a Task months after shipping it bumps `date_updated`, which would file old work under today. They disagree on 2 of this account's 26 closed Tasks — 8% of the record wrong, in the direction that invents work. |
| **Rendered by Python, not the model** | The draft prompt is written for commits ("several commits are often one piece of work"), which says nothing true about a contract being signed. It is also what let this ship before the Foundry and Blog Spaces held a single closed Task: there is no wording to tune on data that does not exist yet. |
| **The Space leads each line** | It is the part git cannot say. A Wren Task mostly restates a commit two sections above it; a Vibe Foundry one is the only record of that day's work anywhere. |
| **`date_updated_gt` at the start of the local day** | Bounds the fetch against the `_MAX_PAGES` ceiling as the workspace grows. Safe rather than lucky: closing a Task *is* an update, so anything closed that day carries a `date_updated` at or after the day's start. |
| **A day of pure non-code work never wakes the model** | Ollama has one slot ([ollama-serving.md](ollama-serving.md)); loading it to render a list nobody drafted would starve chat for nothing. |
| **A ClickUp outage costs the section, not the page** | The standing degrade-don\'t-crash rule. It is logged at WARNING, so a silently missing section is still visible in the 8am log sweep. |

A day that *had* commits but whose draft came back empty writes **nothing at all**,
even when ClickUp closed Tasks that day. That is the model failing, and a page
carrying only the ClickUp list would look plausible enough that nobody would ever
look at it twice.

## What it reads

`scribejay/git_activity.py` shells out to `git log` in each checkout one level
under `PROJECTS_DIR`. Per commit: subject, ISO timestamp, the paths changed, and
the insertion/deletion counts.

Scope:

| Choice | Why |
|---|---|
| `HEAD` **and** `--remotes` | A commit pushed from another machine lands on `origin/<branch>` and on no local branch. Not `--all`, which would also fold in stale local branches and tags, whose rebase copies are commits nobody made that day. `git log` de-duplicates, so a commit on both refs is counted once. |
| `--no-merges` | A merge commit's subject describes bookkeeping, not work. |
| Author-filtered | A shared checkout's other contributors are not the user's day. |
| One level under `PROJECTS_DIR` | The same shallow scan `tasks/project_scan.py` does. A nested monorepo checkout is not found. |

## The fetch in front of the read

`fetch_repos()` runs `git fetch --all --quiet --no-tags` in every checkout before
any day is scanned. This is the Git half's only network call; the ClickUp half
separately calls the ClickUp API with `CLICKUP_API_TOKEN`. GitHub appears as an
application on ScribeJay's `/map` because the remotes here are GitHub.

It exists because the read is otherwise blind to any machine but this one. Work
committed and pushed elsewhere — a cloud session, a second Mac, an edit through
the GitHub web UI — is simply not in the local object store at 4:55 AM, and the
page that results is indistinguishable from a genuinely quiet day. Nothing alerts
on it, because nothing failed.

| Property | Behavior |
|---|---|
| Once per **run**, not per day | A fortnight backfill fetches one time. Fourteen round trips to every remote would return identical objects. |
| Never fatal | An unreachable remote logs a WARNING naming the repos and the day is still written from what is on disk. A dead GitHub costs the newest commits, never the entry. |
| Never blocks | `GIT_TERMINAL_PROMPT=0` and ssh `BatchMode=yes`, plus a `FETCH_TIMEOUT` of 30s per repo. An unattended run has no terminal; without these a remote wanting a password would hang until the timeout every morning. |
| Read-only where it matters | `fetch` updates remote-tracking refs. It never touches the working tree, a local branch, or an in-progress rebase. |

**Known edge.** A commit that is rebased or cherry-picked, where the pre-rebase
copy still sits on a local branch and the post-rebase copy on the remote, counts
twice — two distinct commits, same work. Rare here, since commits go straight to
`main`, and visible in the page rather than silent.

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

**A failed fetch is warned, not fatal.** See the fetch table above: the day is
written from the objects already on disk.

## Running it by hand

```bash
.venv/bin/python -m scribejay.daily_commits
```

`--date 2026-08-25` writes one named day; `--backfill 14` writes each of the last
14 days, oldest first.

A backfill is **one run**, not N runs: the boundary lines the dashboard parses are
logged once around the whole loop, so a fortnight reads as a single row in the run
history rather than fourteen half-runs ([logs.md](logs.md)). The model is warmed on
the first day that actually has commits, so a backfill over a quiet stretch never
loads it at all.

Re-running a day overwrites that day's file — `write_entry` is keyed on the date —
so a backfill is safe to repeat.

## Related

- [docs/scribejay.md](scribejay.md) — the agent this belongs to, and its model dial
- [docs/daily-learnings.md](daily-learnings.md) — the reading half of the same daily record
- [docs/projects.md](projects.md) — the other consumer of `PROJECTS_DIR`
