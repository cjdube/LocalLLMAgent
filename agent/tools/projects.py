"""Scan the user's local project checkouts into a ground-truth registry.

Wren's picture of what the user is building has always been second-hand: the
wiki grows a page for a project only when that project happens to come up in a
day's browsing or chat log, so half the checkouts under PROJECTS_DIR have never
been heard of, and the pages that do exist freeze at whatever the last log said
(the page for LocalLLMAgent describes it via OAuth and debugging). This module
reads the repos themselves.

Deterministic and model-free by design — every field here is a fact Python can
read off the disk, and CLAUDE.md's small-local-model rule is that Python owns
structure. The model's only job (in tasks/project_scan.py) is distilling this
into a blurb.

Only three things are read out of each checkout: its README, its CLAUDE.md, and
the headings of docs/*.md. Nothing else — emphatically not config/.env,
config/*.json, or any other file that happens to sit in a project directory.
The registry is written to a store that a scheduled task refreshes and
tasks/daily_synthesis.py reads, so anything picked up here travels.

A checkout that fails any step degrades to a row with null fields rather than
raising: one broken or half-cloned directory must not cost the other eleven.

Not registered in agent/toolset.py — nothing exposes this to the model yet
(agent/tools/learnings_file.py is the precedent for a schema-less module here).

Usage:
    python -m agent.tools.projects
"""

import argparse
import hashlib
import os
import subprocess
import sys
from pathlib import Path

from agent.tools._http import load_env, print_result

load_env()

DEFAULT_PROJECTS_DIR = str(Path.home() / "Projects")

# Cap the docs fed to the small model, matching tasks/starred_blurbs.py's
# README_CHARS: enough to capture what a project is (the pitch lives up top),
# bounded so an enormous README can't blow the prompt.
DOC_CHARS = 2000

# docs/ headings are one line each; this bounds a project with a big docs tree.
MAX_DOC_TITLES = 20

# Bound a hung git invocation so one wedged checkout can't stall the scan. Same
# posture as tasks/starred_installed.py:_run_version_cmd — no shell, argv list.
GIT_TIMEOUT = 10

# The only filenames read out of a checkout. Everything else in a project
# directory is off limits (see the module docstring); keep this list exhaustive
# rather than reaching for a glob.
_README_NAMES = ("README.md", "README.rst", "README.txt", "readme.md")
_CLAUDE_NAME = "CLAUDE.md"


def _projects_dir() -> Path:
    return Path(os.getenv("PROJECTS_DIR", DEFAULT_PROJECTS_DIR)).expanduser()


def _git(path: Path, *args: str) -> str | None:
    """`git -C path <args>` stdout, stripped — or None if git isn't there, the
    directory isn't a repo, the command fails, or it times out. Never raises:
    5 of the user's 12 checkouts have no git at all, so "not a repo" is an
    ordinary outcome, not an error worth surfacing."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True, text=True, timeout=GIT_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _git_facts(path: Path) -> dict:
    """The freshness half of a row. All-None for a non-repo directory."""
    if _git(path, "rev-parse", "--is-inside-work-tree") != "true":
        return {"remote": None, "branch": None, "last_commit": None,
                "commits_30d": None, "dirty": None}
    count = _git(path, "rev-list", "--count", "--since=30.days", "HEAD")
    return {
        "remote": _git(path, "remote", "get-url", "origin"),
        "branch": _git(path, "rev-parse", "--abbrev-ref", "HEAD"),
        # %cs is the committer date as a bare ISO day — no parsing, no timezone
        # slicing (CLAUDE.md's rule about never truncating an ISO stamp).
        "last_commit": _git(path, "log", "-1", "--format=%cs"),
        "commits_30d": int(count) if count and count.isdigit() else None,
        # --porcelain prints nothing for a clean tree, so "" means clean and
        # None (command failed) means unknown rather than clean.
        "dirty": bool(_git(path, "status", "--porcelain")),
    }


def _read_head(path: Path) -> str:
    """First DOC_CHARS of a text file, or "" if it can't be read. A project
    directory can hold anything, so a binary or permission-denied file is an
    empty field rather than an exception."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:DOC_CHARS]
    except OSError:
        return ""


def _readme(path: Path) -> str:
    for name in _README_NAMES:
        candidate = path / name
        if candidate.is_file():
            return _read_head(candidate)
    return ""


def _doc_titles(path: Path) -> list:
    """Each docs/*.md as its first Markdown heading, falling back to the stem.
    Titles only — the bodies would be tens of thousands of characters, and what
    a project documents is the part that discriminates."""
    docs = path / "docs"
    if not docs.is_dir():
        return []
    titles = []
    for doc in sorted(docs.glob("*.md"))[:MAX_DOC_TITLES]:
        title = doc.stem
        try:
            for line in doc.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("#"):
                    title = line.lstrip("#").strip() or title
                    break
        except OSError:
            pass
        titles.append(title)
    return titles


def _content_hash(readme: str, claude_md: str, doc_titles: list) -> str:
    """Fingerprint of the three model-facing fields, so tasks/project_scan.py
    can skip regenerating a blurb for a project whose docs haven't changed. Git
    facts are deliberately excluded: a commit that touches no documentation
    doesn't change what the project *is*."""
    digest = hashlib.sha256()
    digest.update(readme.encode("utf-8"))
    digest.update(claude_md.encode("utf-8"))
    digest.update("\n".join(doc_titles).encode("utf-8"))
    return digest.hexdigest()[:16]


def _scan_one(path: Path) -> dict:
    """One checkout -> one row. Wrapped by scan_projects' per-directory guard."""
    readme = _readme(path)
    claude_md = _read_head(path / _CLAUDE_NAME)
    doc_titles = _doc_titles(path)
    return {
        "name": path.name,
        "path": str(path),
        "readme": readme,
        "claude_md": claude_md,
        "doc_titles": doc_titles,
        "content_hash": _content_hash(readme, claude_md, doc_titles),
        **_git_facts(path),
    }


def scan_projects() -> dict:
    """Every checkout under PROJECTS_DIR as a row, sorted by name. A missing
    PROJECTS_DIR is surfaced as an error (like learnings_file.py) rather than
    raising, so a misconfigured path degrades to "no projects"."""
    root = _projects_dir()
    if not root.is_dir():
        return {"error": f"projects dir not found (check PROJECTS_DIR): {root}"}

    rows = []
    for entry in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        # Directories only: the user keeps stray files in here (a blog draft, a
        # .DS_Store), and dotted dirs are tooling (.claude), not projects.
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        try:
            rows.append(_scan_one(entry))
        except Exception as e:
            # One unreadable checkout must not cost the rest. The row still
            # exists so the caller can see the project and say what went wrong.
            rows.append({"name": entry.name, "path": str(entry), "readme": "",
                         "claude_md": "", "doc_titles": [], "content_hash": "",
                         "remote": None, "branch": None, "last_commit": None,
                         "commits_30d": None, "dirty": None,
                         "error": f"{type(e).__name__}: {e}"})
    return {"projects": rows}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--brief", action="store_true",
                        help="Omit the readme/claude_md bodies from the output.")
    args = parser.parse_args()

    result = scan_projects()
    if args.brief and "projects" in result:
        result = {"projects": [{k: v for k, v in row.items()
                                if k not in ("readme", "claude_md")}
                               for row in result["projects"]]}
    return print_result(result)


if __name__ == "__main__":
    sys.exit(main())
