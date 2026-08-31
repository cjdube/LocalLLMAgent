# `wren-build` — a ClickUp tag that runs Claude Code against an attached plan

`wren-research` and `wren-context` hand a Task to Wren's small local model.
`wren-build` hands it to Claude Code instead, because the local model cannot
write code. The workflow it automates was already the manual one: design a
change in Claude Code plan mode, attach the plan to a ClickUp Task, move the
Task to `designed` — and then open a terminal and run it by hand. That last
step is what the tag replaces.

**Nothing is committed, staged, branched off or pushed.** The build lands as an
unstaged working tree on a throwaway branch in a git worktree. Review and merge
stay entirely manual. That promise is enforced twice — see
[The git guarantee](#the-git-guarantee).

## The three preconditions

All three are checked in Python. No model decides any of them.

| # | Condition | Why |
|---|-----------|-----|
| 1 | The Task carries the `wren-build` tag | The tag *is* the approval — see below |
| 2 | The Task's status is `designed` (case-insensitive) | A plan on a Task still being written is not ready to build |
| 3 | The Task has **exactly one** `.md` attachment | Two plans means guessing which one, and a guess spends real money on the wrong build |

**The tag is the approval, and there is no phone tap.** Reaching this point
takes three deliberate acts — set the status, attach the plan, apply the tag —
and a tap after those three would ask the same question a fourth time. This is
the one place a Wren job spends money without a confirmation, and it is a
considered choice, not an oversight.

**A failed precondition always leaves a comment** on the Task naming which one
failed and saying to re-apply the tag. Silence would be indistinguishable from
a broken watcher.

## The lifecycle

```
tag applied
  → clickup_watcher (every 5 min)
      GET /task/{id}          read status + attachments
      remove the tag          always, pass or fail
      check preconditions     comment and stop if any fails
      download the plan
      enqueue into config/build_jobs.json
      move the Task to `building`
  → build_worker (every 60 s)
      git worktree add -b wren-build/<slug>-<id>
      symlink .venv and config/.env in
      claude -p <prompt> --settings <deny list>
      verify HEAD never moved
      .venv/bin/python -m pytest
      comment on the Task + push to the phone
```

**The tag comes off before the job is queued**, pass or fail. Queue-first would
re-queue the same Task every five minutes forever, and here every repeat is a
paid Claude Code run. This is the existing watcher rule and it matters more here
than anywhere else. The one exception: if the `GET /task/{id}` itself fails, the
tag is left on so the next poll can try again.

**A failed status move does not cancel the build.** The job is already queued at
that point; refusing to build because a label did not change would be worse than
a stale label.

## Why a second worker

`claude -p` on a real plan runs for minutes to tens of minutes. launchd never
runs two copies of one label at once, so:

- building inside `clickup_watcher` would stall tag polling for the whole run;
- building inside `bg_worker` would stall every other background job, and would
  expose a paid build to that worker's transient-retry logic.

So `tasks/build_worker.py` is its own launchd label
(`local.wren.buildworker`, `StartInterval` 60), draining
`config/build_jobs.json`. This mirrors the existing watcher → `bg_worker` split
exactly. It is a polling job, so it is excluded from `/map` on purpose and
writes no `Starting … run` lines; results reach the user through ClickUp and the
phone, which is where he is already looking.

**No Ollama call happens anywhere in this path.** The tag is the decision and
Claude Code does the thinking, which also leaves the single Ollama slot free
([docs/model-constraints.md](model-constraints.md)).

## The worktree

`git worktree add -b wren-build/<slug>-<job id> <path> main`, under
`WREN_BUILD_WORKTREE_ROOT` (default `~/Projects/.wren-builds/`).

A worktree is **mandatory, not tidiness**. Peer Claude Code sessions share this
checkout *and its git index*, and the live chat server runs from these files.
`git worktree add` writes only a branch ref and `.git/worktrees/` — it never
touches the shared index or the working tree anyone else is looking at.

A fresh worktree has no `.venv` and no `config/.env`, because both are
gitignored. The worker symlinks both in from the main checkout, which is what
makes `.venv/bin/python -m pytest` work inside the worktree exactly as
`AGENTS.md` documents it. Being gitignored, both stay invisible to `git status`.

**Worktrees are not auto-deleted.** The branch is the deliverable. Remove it by
hand with `git worktree remove` after merging or discarding.

## The git guarantee

*Nothing is committed* is enforced twice, because a guard nobody checks is a
guard nobody has.

**The guard** is a `--settings` file written beside the worktree, denying every
writing git verb:

```
Bash(git commit:*)  Bash(git push:*)     Bash(git add:*)      Bash(git checkout:*)
Bash(git switch:*)  Bash(git branch:*)   Bash(git reset:*)    Bash(git rebase:*)
Bash(git merge:*)   Bash(git stash:*)    Bash(git worktree:*) Bash(git tag:*)
Bash(git cherry-pick:*)  Bash(git restore:*)  Bash(git clean:*)  Bash(git remote:*)
Bash(gh:*)
```

Deny beats allow in Claude Code's permission precedence, which is why
`--allowedTools Bash` and this list can coexist. Read-only git — `status`,
`diff`, `log`, `show` — is deliberately left available: a build that cannot see
its own diff is worse at its job for no gain.

**The proof** runs afterwards in Python. The worker captures the base sha before
the run and compares `git rev-parse HEAD` after it. If HEAD moved, it runs
`git reset --mixed <base>` — `--mixed`, never `--hard`, so the work survives in
the working tree — and the report says loudly that Claude committed despite the
deny list. A reset that itself fails escalates the wording again.

Also passed: `--strict-mcp-config` with no `--mcp-config`, so no MCP servers
load. An unattended build has no business holding Gmail or Supabase tools.

## The prompt

Written entirely in Python, never by a model. It is a fixed header (the worktree
path, the branch, read `AGENTS.md` first, the hard no-git rules, run
`.venv/bin/python -m pytest`, end with a short summary), then the ClickUp title
and description, then **the plan verbatim** between
`--- BEGIN PLAN (<name>) --- / --- END PLAN ---`.

Nothing rewrites or summarises the plan. Every rewrite is a chance to drop a
step or invent one. The ClickUp id never appears in the prompt
([docs/opaque-identifiers.md](opaque-identifiers.md)).

Read exactly what will be sent, without spending anything:

```bash
.venv/bin/python -m tasks.build_worker --dry-run
```

## The report

Delivered two ways — an ntfy push and a `wren-build:` comment on the Task — in
separate `try` blocks, so a failure in one cannot silence the other. It carries:

- the branch and the worktree path;
- the diff size, counting untracked files too (nothing is staged, so new files
  would otherwise read as *no change*) — but **not** the two symlinks the worker
  itself made. `.gitignore` says `.venv/`, and a trailing slash matches a
  directory, so the symlink is not ignored inside a worktree even though the
  real folder is ignored in the main checkout. The first live build reported
  *1 new file(s)* for its own plumbing;
- the pytest tail line, **measured by Python**, not quoted from Claude;
- cost, turn count and wall time from Claude's own JSON;
- a `WARNING:` line if the git check bit;
- Claude's own summary, cut at 1200 characters **on a line boundary** and marked
  as cut. The first live build ended its comment mid-word, which reads as a
  crashed job rather than a trimmed one. The untrimmed summary is written to
  `logs/build_worker.log`, which is what the trim line points at;
- the closing line *Nothing was committed or pushed.*

**Every failure path reports too** — timeout, non-zero exit, `is_error` in the
JSON, a worktree that would not create. A build that dies quietly is
indistinguishable from one that was never queued.

## The queue

`config/build_jobs.json`, through `agent/store.py` (`locked`, `load_json`,
`atomic_write_json`), gitignored, pruned on write.

- A job carries **the whole plan text**, not a link. The attachment could change
  or vanish between the tag and the run, and the run is what spends the money.
- `mark_running()` is a **claim**, not a status write: it returns `False` if the
  job is no longer pending, which is what excludes a second worker.
- **5 pending jobs is a hard refusal**, not a soft cap. A queue that deep means
  the worker is not draining, and piling on makes that worse.
- A job stuck `running` for 6 hours is failed by the next poll. Nothing is
  retried — a retry is another paid run.

## Configuration

All `os.getenv()` with inline defaults; every one is documented in
`config/.env.example`.

| Variable | Default | What it does |
|----------|---------|--------------|
| `WREN_BUILD_REPO_ROOT` | `~/Projects/LocalLLMAgent` | The repo a build may touch |
| `WREN_BUILD_WORKTREE_ROOT` | `~/Projects/.wren-builds` | Where worktrees are made |
| `WREN_CLAUDE_BIN` | `~/.local/bin/claude` | The Claude Code binary |
| `WREN_BUILD_TIMEOUT_S` | `1800` | Kills a run that will not end |
| `WREN_BUILD_MODEL` | *(unset)* | Passed as `--model` only when set |

## Known limits

**Cost has no cap.** Every tag spends real Claude Code tokens on a long run, and
there is no daily budget. If one is wanted, the natural place is a per-day job
count in `tasks/build_queue.py`.

**Wren's repo only.** `WREN_BUILD_REPO_ROOT` exists so this can grow, but a
ScribeJay plan tagged today would be built in the wrong checkout. The
precondition check cannot catch that, because ScribeJay items live in the `wren`
Space too. A second tag is the eventual answer.

**Attachment URLs need no auth.** ClickUp's download links are unguessable but
public. Worth knowing; not a blocker, since a plan is not a secret.

**launchd and Claude Code auth.** `claude` authenticates from the macOS
Keychain, and a launchd job does not always have the access an interactive shell
does. If a build fails to authenticate under launchd, the fallback is
`ANTHROPIC_API_KEY` in `config/.env`.
