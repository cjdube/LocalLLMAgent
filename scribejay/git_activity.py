"""Yesterday's commits, read out of the checkouts under PROJECTS_DIR.

The record already said WHEN the user worked (scribejay/claude_time_blocks.py) and
what he READ (scribejay/daily_chrome_learnings.py). It did not say what he shipped.
Git is the only source that does.

Reading is local `git log`. One network call sits in front of it: fetch_repos()
runs `git fetch`, so a commit made on another machine and pushed is on this disk
before the day is scanned. Without it, work done anywhere but this Mac is absent
from the page and nothing says so — a quiet day and a lost day look identical.
The fetch is best-effort: an unreachable remote logs a WARNING and the day is
still written from what is already here.

Gather-only, the way agent/tools/chrome_history.py is for browsing. No model call
lives here (that is scribejay/daily_commits.py), and no TOOL_SCHEMA: Wren reads the
record ScribeJay writes, not the repos themselves.

Scope:
- `HEAD` plus the remote-tracking branches (`--remotes`) — where a commit fetched
  from another machine lands. Not `--all`, which would also fold in stale local
  branches and tags, whose rebase copies are commits nobody made that day.
- Merge commits are skipped — they carry no message worth journaling.
- Author-filtered, because a shared checkout's other contributors are not his day.
"""

import os
import subprocess
from pathlib import Path

DEFAULT_PROJECTS_DIR = str(Path.home() / "Projects")

GIT_TIMEOUT = 15
# Fetching talks to a remote, so it gets its own, longer budget. That budget is
# the safety net for an unattended 4:55 AM run: with the non-interactive
# environment in _fetch, a remote that wants a password fails instead of hanging.
FETCH_TIMEOUT = 30

# Prompt-bounding caps. A heavy day is ~10 commits, so these rarely bind — they
# exist because one `git log` over a repo with a vendored dependency tree can
# produce a 900-file commit, and the small local model's window is the thing that
# breaks first. Every cap that actually drops something logs it (AGENTS.md:
# degrading is only safe if it is logged).
MAX_COMMITS = 40
MAX_FILES_PER_COMMIT = 12
# Char budget for the rendered block. A count cap alone never bounds size: 40
# commits x 12 long paths is still 20k characters if the paths are deep.
MAX_PROMPT_CHARS = 12000

# Record and field separators. Chosen over a newline-delimited format because a
# commit subject may contain anything except these two control characters.
_REC = "\x01"
_FIELD = "\x1f"


def _projects_dir() -> Path:
    return Path(os.getenv("PROJECTS_DIR", DEFAULT_PROJECTS_DIR)).expanduser()


def _git(path: Path, *args: str) -> str | None:
    """`git -C path <args>` stdout — or None if git isn't there, the directory
    isn't a repo, the command fails, or it times out. Never raises: several of the
    user's checkouts have no git at all, so "not a repo" is an ordinary outcome.
    Deliberately does NOT strip: `git log` output is parsed by separator, and a
    trailing newline is part of the last file path's line."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True, text=True, timeout=GIT_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def author() -> str | None:
    """Whose commits count as the user's day.

    SCRIBEJAY_GIT_AUTHOR, else the machine's global git identity. None means no
    filter — right on a single-user Mac, wrong on a shared checkout, which is why
    collect_commits logs a WARNING when it resolves that way rather than treating
    "everyone's commits" as normal."""
    configured = os.getenv("SCRIBEJAY_GIT_AUTHOR")
    if configured:
        return configured
    try:
        proc = subprocess.run(["git", "config", "--global", "user.email"],
                              capture_output=True, text=True, timeout=GIT_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.strip() or None


def _repos(root: Path) -> list[Path]:
    """Every git checkout one level under `root`, sorted by name."""
    if not root.is_dir():
        return []
    return sorted((p for p in root.iterdir() if (p / ".git").exists()), key=lambda p: p.name)


def _fetch(path: Path) -> bool:
    """`git fetch` one repo. True if git returned 0.

    The environment is the whole point. An unattended run must fail rather than
    block, so terminal prompts are off and ssh runs in batch mode; without those a
    remote asking for a password or an unknown host key would sit there until the
    timeout, every single morning. `--no-tags` because nothing here reads tags."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    env.setdefault("GIT_SSH_COMMAND", "ssh -oBatchMode=yes")
    try:
        proc = subprocess.run(
            ["git", "-C", str(path), "fetch", "--all", "--quiet", "--no-tags"],
            capture_output=True, text=True, timeout=FETCH_TIMEOUT, env=env,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def fetch_repos(logger=None) -> dict:
    """Bring every checkout under PROJECTS_DIR up to date with its remote, so a
    commit pushed from another machine is on this disk before any day is scanned.

    Returns {"repos": n, "failed": n}. Never raises and never fatal: a repo with no
    remote is a no-op costing milliseconds, and an unreachable remote logs a
    WARNING and leaves that checkout exactly as it was — the day is still written
    from what is already here, which is what the task did before this existed.

    Called once per run rather than once per day: a fortnight backfill needs the
    objects fetched one time, not fourteen."""
    repos = _repos(_projects_dir())
    failed = [repo.name for repo in repos if not _fetch(repo)]
    if failed and logger:
        logger.warning(
            f"git fetch failed in {len(failed)} of {len(repos)} repos "
            f"({', '.join(failed)}); scanning what is already on disk"
        )
    return {"repos": len(repos), "failed": len(failed)}


def _parse_log(output: str, repo: str) -> list[dict]:
    """`git log --numstat` output for one repo -> one row per commit.

    Rows carry the file paths AND the counts because they answer different
    questions: "tests/ and docs/ were touched" is the shape of the work, the line
    counts are its size. Binary files report "-" for both counts in numstat and
    contribute 0, but still count as a file touched."""
    rows = []
    for chunk in output.split(_REC):
        if not chunk.strip():
            continue
        header, _, body = chunk.partition("\n")
        parts = header.split(_FIELD)
        if len(parts) != 3:
            continue
        sha, stamp, subject = parts
        files, insertions, deletions = [], 0, 0
        for line in body.splitlines():
            fields = line.split("\t")
            if len(fields) != 3:
                continue
            added, removed, path = fields
            files.append(path)
            insertions += int(added) if added.isdigit() else 0
            deletions += int(removed) if removed.isdigit() else 0
        rows.append({
            "sha": sha,
            # Kept whole, with its offset — never sliced. The window that selected
            # this commit was already local (agent/activity_log.py:prior_day).
            "time": stamp,
            "repo": repo,
            "subject": subject,
            "files": files,
            "files_total": len(files),
            "insertions": insertions,
            "deletions": deletions,
        })
    return rows


def collect_commits(start, end, logger=None) -> dict:
    """Every commit the user authored between `start` and `end` (local-aware
    datetimes from agent/activity_log.py:prior_day), across the checkouts under
    PROJECTS_DIR, newest first.

    Returns {"commits": [...], "repos": {name: count}, "total_commits": n,
    "repos_scanned": n}. A repo whose `git log` fails contributes nothing rather
    than failing the run — one broken checkout must not cost the whole journal.

    Reads only. Getting other machines' commits onto this disk first is
    fetch_repos()' job, called once per run by scribejay/daily_commits.py."""
    root = _projects_dir()
    repos = _repos(root)
    who = author()
    if who is None and logger:
        logger.warning(
            "no git author resolved (SCRIBEJAY_GIT_AUTHOR unset and no global "
            "user.email) — counting EVERY author's commits as the user's"
        )

    args = [
        # HEAD and the remote-tracking branches: a commit pushed from another
        # machine is on origin/<branch> after fetch_repos() and on no local branch
        # at all. git log de-duplicates, so a commit on both is counted once.
        "log", "--no-merges", "HEAD", "--remotes",
        f"--since={start.isoformat()}", f"--until={end.isoformat()}",
        f"--pretty=format:{_REC}%h{_FIELD}%aI{_FIELD}%s", "--numstat",
    ]
    if who:
        args.insert(1, f"--author={who}")

    commits: list[dict] = []
    for repo in repos:
        output = _git(repo, *args)
        if output is None:
            if logger:
                logger.warning(f"git log failed in {repo.name}; skipping it")
            continue
        commits.extend(_parse_log(output, repo.name))

    commits.sort(key=lambda c: c["time"], reverse=True)
    counts: dict[str, int] = {}
    for c in commits:
        counts[c["repo"]] = counts.get(c["repo"], 0) + 1
    return {
        "commits": commits,
        "repos": counts,
        "total_commits": len(commits),
        "repos_scanned": len(repos),
    }


def compact_commits(commits: list, logger=None) -> list:
    """Bound the commit rows for the prompt, saying so whenever something is cut.

    Two caps, applied in the order that loses the least: the file list is the
    first thing trimmed (the subject and the counts survive), and only then are
    whole commits dropped."""
    rows = []
    trimmed_files = 0
    for c in commits[:MAX_COMMITS]:
        row = dict(c)
        if len(row["files"]) > MAX_FILES_PER_COMMIT:
            row["files"] = row["files"][:MAX_FILES_PER_COMMIT]
            trimmed_files += 1
        rows.append(row)
    if logger:
        if len(commits) > MAX_COMMITS:
            logger.warning(f"capped commits at {MAX_COMMITS} of {len(commits)}; "
                           "the oldest of the day are not in the prompt")
        if trimmed_files:
            logger.warning(f"trimmed the file list on {trimmed_files} commit(s) to "
                           f"{MAX_FILES_PER_COMMIT} paths")
    return rows


def render_commits(rows: list, logger=None) -> str:
    """The commit block as it goes into the prompt: one line per commit, its files
    indented under it.

    Built in Python rather than handed over as a dict so the shape the model reads
    is fixed. If the block still exceeds MAX_PROMPT_CHARS, the file lists are
    dropped wholesale — subjects and counts are what the draft is actually written
    from, and losing them to a truncation would cost whole commits."""
    def block(with_files: bool) -> str:
        lines = []
        for c in rows:
            lines.append(f"- [{c['repo']}] {c['subject']} "
                         f"({c['files_total']} files, +{c['insertions']}/-{c['deletions']})")
            if with_files and c["files"]:
                lines.append(f"    files: {', '.join(c['files'])}")
        return "\n".join(lines)

    full = block(True)
    if len(full) <= MAX_PROMPT_CHARS:
        return full
    bare = block(False)
    if logger:
        logger.warning(f"commit block was {len(full)} chars (cap {MAX_PROMPT_CHARS}); "
                       f"dropped every file list, now {len(bare)}")
    return bare
