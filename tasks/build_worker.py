"""Run one queued Claude Code build per invocation, then exit. Non-interactive —
launchd runs it on a StartInterval, and launchd never runs two copies of one
label at once, so a long build just delays the next poll.

The job arrives from tasks/clickup_watcher.py, which has already checked that
the ClickUp Task is `designed`, carries exactly one `.md` plan, and was tagged
`wren-build` by hand. Those three deliberate acts ARE the approval; there is no
phone tap, and nothing here asks for one.

**No model is called anywhere in this module.** Not Ollama, not Gemini. The tag
was the decision and Claude Code does the thinking, so Wren's own single model
slot is never taken (docs/model-constraints.md). Everything here is Python:
the prompt, the checks, the diff numbers, the report.

**A separate worker, not tasks/bg_worker.py.** A build runs for minutes to tens
of minutes. Inside the watcher it would stall tag polling for the whole run;
inside bg_worker it would stall every other background job and inherit that
worker's transient-retry logic, which would happily start a paid build a second
time. This is the same watcher-plus-worker split, one layer along.

**The worktree is mandatory, not a nicety.** Peer Claude Code sessions share
this checkout *and its git index*, and the live chat server runs from these
files. `git worktree add` writes only a branch ref and .git/worktrees/, never
the shared index, so an unattended build can edit freely without colliding with
either.

**Claude is stopped from touching git twice over.** A --settings deny list is
the guard; comparing HEAD against the sha the worktree started on is the proof,
because a guard nobody checks is a guard nobody has. If HEAD moved, it is reset
back and the report says so.

Nothing is ever committed, staged, pushed or merged. The branch is the
deliverable and the user reviews it by hand.

Usage:
    python -m tasks.build_worker
    python -m tasks.build_worker --dry-run   # print the prompt and argv, run nothing
"""

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.tools import clickup
from agent.usage_ledger import record as record_usage
from agent.tools.notify import notify
from tasks import build_queue
from tasks._common import notify_failure, setup_logger

_ROOT = Path(__file__).resolve().parent.parent

# Where a build happens, and with what. All os.getenv with inline defaults, per
# the repo convention; every one is documented in config/.env.example.
DEFAULT_REPO_ROOT = "~/Projects/LocalLLMAgent"
DEFAULT_WORKTREE_ROOT = "~/Projects/.wren-builds"
DEFAULT_CLAUDE_BIN = "~/.local/bin/claude"

# Thirty minutes. A plan-sized change takes a few; this is the ceiling that stops
# a wedged or looping run holding the worker forever, not a target.
DEFAULT_TIMEOUT_S = 1800

# The test run afterwards is this repo's own suite, which takes well under a
# minute. Its own budget, so a slow build cannot eat the time that proves it.
PYTEST_TIMEOUT_S = 600

# Git plumbing is instant or broken.
GIT_TIMEOUT_S = 60

# Every git verb that would move a commit, a branch, a ref or a remote. Deny
# beats allow in Claude Code's permission rules, so this carves the holes out of
# a plain `Bash` allow. Read-only git (status, diff, log, show) is deliberately
# left available — the plan may legitimately need to look at history.
GIT_DENY = [
    "Bash(git commit:*)",
    "Bash(git push:*)",
    "Bash(git add:*)",
    "Bash(git checkout:*)",
    "Bash(git switch:*)",
    "Bash(git branch:*)",
    "Bash(git reset:*)",
    "Bash(git rebase:*)",
    "Bash(git merge:*)",
    "Bash(git stash:*)",
    "Bash(git worktree:*)",
    "Bash(git tag:*)",
    "Bash(git cherry-pick:*)",
    "Bash(git restore:*)",
    "Bash(git clean:*)",
    "Bash(git remote:*)",
    "Bash(gh:*)",
]

# What Claude may reach for. Named explicitly because an unlisted tool is denied
# in headless mode, and a denial there is silent — the run simply gets less done.
ALLOWED_TOOLS = "Bash,Edit,Write,Read,Glob,Grep,TodoWrite,NotebookEdit"

# What the worker links into a fresh worktree so `.venv/bin/python -m pytest`
# works there. Named once because two places need the same list: the linker,
# and diffstat — which must not count the worker's own plumbing as the user's
# work. `.gitignore` says `.venv/`, and a trailing slash matches a directory,
# so the symlink is NOT ignored inside a worktree even though the real folder
# is ignored in the main checkout. Measured on the first live build.
LINKED_PATHS = (".venv", "config/.env")

# How much of Claude's closing summary reaches the ClickUp comment and the push.
# Cut on a line boundary, never mid-word — the first live build ended the
# comment on the word "every", which reads as a crash rather than a trim.
_MAX_SUMMARY_CHARS = 1200


def _repo_root() -> Path:
    return Path(os.getenv("WREN_BUILD_REPO_ROOT", DEFAULT_REPO_ROOT)).expanduser()


def _worktree_root() -> Path:
    return Path(os.getenv("WREN_BUILD_WORKTREE_ROOT", DEFAULT_WORKTREE_ROOT)).expanduser()


def _claude_bin() -> Path:
    return Path(os.getenv("WREN_CLAUDE_BIN", DEFAULT_CLAUDE_BIN)).expanduser()


def _timeout_s() -> int:
    try:
        return int(os.getenv("WREN_BUILD_TIMEOUT_S", DEFAULT_TIMEOUT_S))
    except ValueError:
        return DEFAULT_TIMEOUT_S


def slug(title: str) -> str:
    """A branch- and directory-safe name from a ClickUp title. Bounded, because
    this becomes a path and a git ref."""
    out = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return (out[:48].rstrip("-")) or "build"


# ---- git ------------------------------------------------------------------

def _git(args: list, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, timeout=GIT_TIMEOUT_S)


def create_worktree(job: dict, logger) -> dict:
    """A fresh worktree on a fresh branch off main. Returns
    {worktree, branch, base_sha} or {"error": ...}.

    The branch name carries the job id as well as the title slug: two runs of
    the same Task must not collide, and a build retried after a failure is a
    different branch the user can compare against the first.
    """
    repo = _repo_root()
    if not (repo / ".git").exists():
        return {"error": f"no git repository at {repo}"}

    name = f"{slug(job['title'])}-{job['id']}"
    branch = f"wren-build/{name}"
    worktree = _worktree_root() / name
    if worktree.exists():
        return {"error": f"worktree path already exists: {worktree}"}
    worktree.parent.mkdir(parents=True, exist_ok=True)

    proc = _git(["worktree", "add", "-b", branch, str(worktree), "main"], repo)
    if proc.returncode != 0:
        return {"error": f"git worktree add failed: {(proc.stderr or '').strip()[:300]}"}

    head = _git(["rev-parse", "HEAD"], worktree)
    if head.returncode != 0:
        return {"error": "could not read the new worktree's HEAD"}

    # .venv and config/.env are both gitignored, so a fresh worktree has
    # neither — and without them `.venv/bin/python -m pytest` (the command
    # AGENTS.md tells every contributor to run) does not exist inside it.
    # Symlinks rather than copies: one interpreter, one set of secrets, and
    # nothing to clean up or leave stale.
    for rel in LINKED_PATHS:
        source = repo / rel
        target = worktree / rel
        if source.exists() and not target.exists():
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(source)
            except OSError as e:
                logger.warning(f"could not link {rel} into the worktree: {e}")

    logger.info(f"worktree {worktree} on {branch} at {head.stdout.strip()[:12]}")
    return {"worktree": worktree, "branch": branch, "base_sha": head.stdout.strip()}


def verify_untouched_git(worktree: Path, base_sha: str, branch: str) -> dict:
    """Prove the no-git promise rather than trusting the deny list.

    Returns {"ok": True} or {"ok": False, "note": ...}; on a moved HEAD it also
    resets back to base_sha with a mixed reset, which keeps every edit in the
    working tree and only undoes the commit.
    """
    head = _git(["rev-parse", "HEAD"], worktree)
    if head.returncode != 0:
        return {"ok": False, "note": "could not read HEAD after the build"}
    now = head.stdout.strip()
    if now == base_sha:
        return {"ok": True}
    reset = _git(["reset", "--mixed", base_sha], worktree)
    detail = "commits were undone, every change kept in the working tree"
    if reset.returncode != 0:
        detail = f"AND THE RESET FAILED: {(reset.stderr or '').strip()[:200]}"
    return {"ok": False,
            "note": f"Claude committed despite the deny list ({base_sha[:12]} -> "
                    f"{now[:12]}) — {detail}"}


def diffstat(worktree: Path, base_sha: str) -> str:
    """One line: what changed against the commit the worktree started on.
    Untracked files are counted too — nothing is staged, so `git diff` alone
    would report a brand new module as no change at all."""
    tracked = _git(["diff", "--shortstat", base_sha], worktree)
    line = (tracked.stdout or "").strip() or "no tracked changes"
    untracked = _git(["ls-files", "--others", "--exclude-standard"], worktree)
    new = [f for f in (untracked.stdout or "").splitlines()
           if f.strip() and f.strip().rstrip("/") not in LINKED_PATHS]
    if new:
        line += f", {len(new)} new file(s)"
    return line


# ---- the Claude Code run ---------------------------------------------------

def settings_json() -> str:
    """The --settings payload: what Claude may not do to git, in one place."""
    return json.dumps({"permissions": {"deny": GIT_DENY}}, indent=2)


def build_prompt(job: dict, worktree: Path, branch: str) -> str:
    """The whole prompt, written in Python. Pure — no I/O — so a test can read
    exactly what Claude will be asked.

    **The plan goes in verbatim.** Nothing summarises or re-words it: the user
    wrote and reviewed that text, and every rewrite is a chance to drop a step
    or invent one. Same rule as tasks/clickup_watcher.py's templates.
    """
    described = f"What the task says:\n{job['description']}\n\n" if job.get("description") else ""
    return f"""Implement the plan below. It has already been reviewed and approved.

You are in a throwaway git worktree at {worktree}, on branch {branch}. Nobody
will look at this branch until a human reviews it, and the human merges it by
hand.

Read AGENTS.md at the root of this repository first, and follow it.

Hard rules for this run:
- Do NOT commit, and do NOT stage anything with git add.
- Do NOT create, switch, rename or delete a branch. Do NOT push. Do NOT use gh.
- Leave every change sitting in the working tree, unstaged. That is the deliverable.
- Do not edit anything outside this worktree.
- When the implementation is done, run: .venv/bin/python -m pytest
- Finish with a short plain-text summary: what you changed, and whether the
  tests passed.

This came from a ClickUp task.
Title: {job['title']}
{described}The plan follows exactly as it was written. Implement it.

--- BEGIN PLAN ({job.get('plan_name', 'plan.md')}) ---
{job['plan_text']}
--- END PLAN ---
"""


def claude_argv(prompt: str, settings_path: Path) -> list:
    """The exact command line. `--strict-mcp-config` with no --mcp-config means
    no MCP servers load at all: an unattended build has no business holding the
    user's Gmail, Drive or database tools."""
    argv = [
        str(_claude_bin()), "-p", prompt,
        "--output-format", "json",
        "--permission-mode", "acceptEdits",
        "--allowedTools", ALLOWED_TOOLS,
        "--settings", str(settings_path),
        "--strict-mcp-config",
    ]
    model = os.getenv("WREN_BUILD_MODEL", "").strip()
    if model:
        argv += ["--model", model]
    return argv


def _record_claude_usage(payload: dict) -> None:
    """Put one Claude Code run into the usage ledger.

    This is the only place Wren spends Anthropic tokens, and until now the run's
    own accounting was read, printed into the ClickUp report, and dropped — so
    /activity would have shown a $0 month beside builds that plainly cost money.
    Unlike the Ollama and Gemini paths the price is not estimated here: the CLI
    reports what it was actually charged, and that figure is passed straight
    through as cost_usd.

    `modelUsage` breaks a run down per model when the CLI reports it (one run can
    touch more than one), and a row per model is what keeps the by-model chart
    honest. Without it the run is still recorded, attributed to "claude-code" —
    a row with no model name is far less of a loss than a spend with no row.
    """
    per_model = payload.get("modelUsage")
    if isinstance(per_model, dict) and per_model:
        for name, raw in per_model.items():
            u = raw if isinstance(raw, dict) else {}
            record_usage(
                agent="wren", task="build_worker", backend="anthropic",
                model=name, caller="claude_code",
                prompt_tokens=u.get("inputTokens") or u.get("input_tokens"),
                output_tokens=u.get("outputTokens") or u.get("output_tokens"),
                duration_ms=payload.get("duration_ms"),
                cost_usd=u.get("costUSD"),
                finish_reason=_turns_note(payload),
            )
        return
    raw_usage = payload.get("usage")
    usage = raw_usage if isinstance(raw_usage, dict) else {}
    record_usage(
        agent="wren", task="build_worker", backend="anthropic",
        model="claude-code", caller="claude_code",
        prompt_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
        duration_ms=payload.get("duration_ms"),
        cost_usd=payload.get("total_cost_usd"),
        finish_reason=_turns_note(payload),
    )


def _turns_note(payload: dict) -> str | None:
    """A Claude Code run has no single stop reason — it is many turns. How many
    it took is the nearest useful thing, and it is what distinguishes a build
    that went round in circles from one that landed first try."""
    turns = payload.get("num_turns")
    return f"{turns} turns" if turns is not None else None


def run_claude(worktree: Path, prompt: str, logger) -> dict:
    """One Claude Code run. Never raises; returns {"summary", "cost", "turns"}
    or {"error": ...}."""
    binary = _claude_bin()
    if not binary.exists():
        return {"error": f"claude not found at {binary} (set WREN_CLAUDE_BIN)"}

    settings_path = worktree.parent / f"{worktree.name}.settings.json"
    settings_path.write_text(settings_json())
    # Kept beside the worktree on purpose: when a build goes wrong, the first
    # question is always "what was it actually asked?".
    (worktree.parent / f"{worktree.name}.prompt.txt").write_text(prompt)

    argv = claude_argv(prompt, settings_path)
    logger.info(f"running claude in {worktree} (timeout {_timeout_s()}s)")
    try:
        proc = subprocess.run(argv, cwd=str(worktree), capture_output=True,
                              text=True, timeout=_timeout_s())
    except subprocess.TimeoutExpired:
        return {"error": f"claude did not finish within {_timeout_s()}s"}
    except OSError as e:
        return {"error": f"could not run claude: {e}"}

    try:
        payload = json.loads(proc.stdout)
    except ValueError:
        detail = (proc.stderr or proc.stdout or "").strip()[:400]
        return {"error": f"claude returned no usable JSON (exit {proc.returncode})"
                         + (f": {detail}" if detail else "")}
    if payload.get("is_error"):
        return {"error": f"claude reported an error: {str(payload.get('result'))[:300]}"}
    if proc.returncode != 0:
        return {"error": f"claude exited {proc.returncode}"}
    summary = (payload.get("result") or "").strip()
    # In full, because report_text trims it and tells the reader to look here.
    if summary:
        logger.info(f"claude summary:\n{summary}")
    _record_claude_usage(payload)
    return {
        "summary": summary,
        "cost": payload.get("total_cost_usd"),
        "turns": payload.get("num_turns"),
        "duration_ms": payload.get("duration_ms"),
    }


def run_pytest(worktree: Path) -> str:
    """The suite's own verdict, measured here rather than believed from the
    summary. Claude is also told to run it; this is the number the user is
    given, because a model reporting on its own tests is the one claim in the
    report nothing else would catch."""
    python = worktree / ".venv" / "bin" / "python"
    if not python.exists():
        return "not run (no .venv in the worktree)"
    try:
        proc = subprocess.run([str(python), "-m", "pytest", "-q"], cwd=str(worktree),
                              capture_output=True, text=True, timeout=PYTEST_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return f"did not finish within {PYTEST_TIMEOUT_S}s"
    except OSError as e:
        return f"could not run: {e}"
    lines = [l.strip() for l in (proc.stdout or "").splitlines() if l.strip()]
    tail = next((l for l in reversed(lines)
                 if "passed" in l or "failed" in l or "error" in l.lower()), "")
    verdict = re.sub(r"[=\s]+$", "", re.sub(r"^[=\s]+", "", tail))[:160] or "no summary line"
    return verdict if proc.returncode == 0 else f"FAILING — {verdict}"


# ---- reporting -------------------------------------------------------------

def trim_summary(summary: str, limit: int = _MAX_SUMMARY_CHARS) -> str:
    """Claude's closing summary, cut on a line boundary and marked as cut.

    A mid-word cut reads as a crashed job rather than a trimmed one, and the
    reader cannot tell which. Falls back to a hard cut only when the first line
    alone is already over the limit."""
    summary = summary.strip()
    if len(summary) <= limit:
        return summary
    kept = []
    used = 0
    for line in summary.splitlines():
        if kept and used + len(line) + 1 > limit:
            break
        kept.append(line)
        used += len(line) + 1
    text = "\n".join(kept).rstrip()
    if len(text) > limit:
        text = text[:limit].rstrip()
    return text + "\n[... trimmed. The full summary is in the build log.]"


def report_text(job: dict, branch: str, worktree: Path, changed: str, tests: str,
                claude: dict, git_note: str = "") -> str:
    """What the user reads, on the phone and on the ClickUp task. Every number
    in it was measured by this module."""
    lines = [
        f"wren-build: {branch}",
        f"worktree: {worktree}",
        f"changed: {changed}",
        f"tests: {tests}",
    ]
    if claude.get("cost") is not None:
        cost = f"${claude['cost']:.2f}"
        turns = claude.get("turns")
        mins = round((claude.get("duration_ms") or 0) / 60000, 1)
        lines.append(f"run: {cost} - {turns} turns - {mins} min")
    if git_note:
        lines.append(f"WARNING: {git_note}")
    summary = (claude.get("summary") or "").strip()
    if summary:
        lines.append("")
        lines.append(trim_summary(summary))
    lines.append("")
    lines.append("Nothing was committed or pushed. Review the branch, then merge by hand.")
    return "\n".join(lines)


def _report_back(job: dict, text: str, title_line: str, logger) -> None:
    """Both channels, and neither is allowed to silence the other. A build that
    finishes invisibly is indistinguishable from one that never started."""
    try:
        notify(message=text, title=title_line)
    except Exception:
        logger.exception("could not push the build result")
    try:
        # Called as a library function, exactly as the watcher calls
        # remove_clickup_tag. The WRITE_TOOLS / UNATTENDED_EXCLUDED_TOOLS gates
        # govern what the MODEL may call; no model is involved here and every
        # word of this comment was written in Python.
        result = clickup.comment_on_clickup_task(title=job["title"], comment=text)
        if "error" in result:
            logger.warning(f"could not comment on {job['title']!r}: {result['error']}")
    except Exception:
        logger.exception("could not comment the build result onto ClickUp")


# ---- the job ---------------------------------------------------------------

def run_job(job: dict, logger) -> None:
    made = create_worktree(job, logger)
    if "error" in made:
        build_queue.mark_failed(job["id"], made["error"])
        _report_back(job, f"wren-build: could not start - {made['error']}",
                     "Wren build failed", logger)
        return

    worktree, branch, base_sha = made["worktree"], made["branch"], made["base_sha"]
    if not build_queue.mark_running(job["id"], branch, str(worktree)):
        # Another worker claimed it between next_pending and here. Leave its
        # worktree alone and say nothing: the other run will report.
        logger.info(f"job {job['id']} was claimed by another run; standing down")
        return

    claude = run_claude(worktree, build_prompt(job, worktree, branch), logger)
    git_check = verify_untouched_git(worktree, base_sha, branch)
    git_note = "" if git_check["ok"] else git_check["note"]
    if git_note:
        logger.error(git_note)

    if "error" in claude:
        text = "\n".join([
            f"wren-build: {branch} did not finish - {claude['error']}",
            f"worktree: {worktree}",
            f"changed: {diffstat(worktree, base_sha)}",
        ] + ([f"WARNING: {git_note}"] if git_note else []))
        build_queue.mark_failed(job["id"], claude["error"])
        logger.error(f"job {job['id']} failed: {claude['error']}")
        _report_back(job, text, "Wren build failed", logger)
        return

    text = report_text(job, branch, worktree, diffstat(worktree, base_sha),
                       run_pytest(worktree), claude, git_note)
    build_queue.mark_done(job["id"], text)
    logger.info(f"job {job['id']} done on {branch}")
    _report_back(job, text, "Wren build done", logger)


def _fail_stale(logger) -> None:
    """A job whose worker was killed mid-build stays `running` forever otherwise,
    and `running` is what the user was told to expect an answer from. Nothing is
    retried — the worktree may be half-written, and re-running Claude over it
    would compound the mess rather than fix it."""
    for job in build_queue.stale_running():
        logger.error(f"job {job['id']} has been running since {job['updated']}; failing it")
        build_queue.mark_failed(job["id"], "the worker was killed mid-build")
        _report_back(job, f"wren-build: {job.get('branch') or job['title']} was "
                          "interrupted mid-build and will not resume. Re-tag to try again.",
                     "Wren build interrupted", logger)


def _dry_run() -> int:
    """Print what the next pending job would run, and run nothing."""
    job = build_queue.next_pending()
    if job is None:
        print("no pending build jobs")
        return 0
    name = f"{slug(job['title'])}-{job['id']}"
    worktree = _worktree_root() / name
    branch = f"wren-build/{name}"
    print(f"--- job {job['id']}: {job['title']} ---\n")
    print(f"repo:     {_repo_root()}\nworktree: {worktree}\nbranch:   {branch}\n")
    print("--- settings ---")
    print(settings_json())
    print("\n--- argv ---")
    print(" ".join(shlex.quote(a) for a in
                   claude_argv("<PROMPT>", worktree.parent / f"{name}.settings.json")))
    print("\n--- prompt ---")
    print(build_prompt(job, worktree, branch))
    return 0


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="print the prompt and command for the next job, run nothing")
    args = parser.parse_args(argv)
    if args.dry_run:
        return _dry_run()

    logger = setup_logger("build_worker")
    job = None
    try:
        job = build_queue.next_pending()
        if job is None:
            _fail_stale(logger)
            return 0
        run_job(job, logger)
        return 0
    except Exception as e:
        logger.exception(f"Build worker failed: {e}")
        if job is not None:
            build_queue.mark_failed(job["id"], str(e))
        notify_failure("build_worker", e, logger)
        return 1


if __name__ == "__main__":
    sys.exit(main())
