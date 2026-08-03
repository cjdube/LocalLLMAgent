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

from agent import prefs
from agent.store import load_json
from agent.tools._http import load_env, print_result
from agent.tools.wiki import list_project_pages

# Whose projects these are, for the model-facing descriptions below.
_NAME = prefs.user_name()

load_env()

DEFAULT_PROJECTS_DIR = str(Path.home() / "Projects")

# The registry tasks/project_scan.py writes: the scan above plus the model's
# distillation of each project. Lives here rather than on the task because the
# chat tools below read it too, and a tool module cannot import a task module —
# the same arrangement memory.py, opportunities.py and reminders.py have with
# their stores.
PROJECTS_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "projects.json"

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


def _doc_titles(path: Path) -> tuple:
    """Each docs/*.md as its first Markdown heading, falling back to the stem,
    plus how many were found before MAX_DOC_TITLES capped the list. Titles only
    — the bodies would be tens of thousands of characters, and what a project
    documents is the part that discriminates.

    The count is returned rather than discarded so tasks/project_scan.py can say
    that a project outgrew the cap. A slice that silently drops the tail is the
    "degrade without logging" failure CLAUDE.md warns about: the blurb just
    quietly stops reflecting part of what the project documents."""
    docs = path / "docs"
    if not docs.is_dir():
        return [], 0
    found = sorted(docs.glob("*.md"))
    titles = []
    for doc in found[:MAX_DOC_TITLES]:
        title = doc.stem
        try:
            for line in doc.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("#"):
                    title = line.lstrip("#").strip() or title
                    break
        except OSError:
            pass
        titles.append(title)
    return titles, len(found)


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
    doc_titles, docs_found = _doc_titles(path)
    return {
        "name": path.name,
        "path": str(path),
        "readme": readme,
        "claude_md": claude_md,
        "doc_titles": doc_titles,
        # Pre-cap count, so the task can warn when the tail was dropped. Kept
        # out of content_hash: outgrowing the cap doesn't change what the
        # project *is*, and shouldn't cost every project a re-distillation.
        "docs_found": docs_found,
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
                         "claude_md": "", "doc_titles": [], "docs_found": 0,
                         "content_hash": "",
                         "remote": None, "branch": None, "last_commit": None,
                         "commits_30d": None, "dirty": None,
                         "error": f"{type(e).__name__}: {e}"})
    return {"projects": rows}


def load_registry() -> list:
    """The rows tasks/project_scan.py last wrote — the scan plus each project's
    distilled summary and topics. Resolves PROJECTS_PATH at call time so one
    redirect of that module attribute covers the task that writes it and every
    reader. A missing store is an empty list: the scan hasn't run yet, which is
    not a failure."""
    return load_json(PROJECTS_PATH, {}).get("projects", [])


# --- model-facing tools -----------------------------------------------------
#
# Both read the checkouts LIVE and merge the cached distillation, rather than
# serving the registry alone. The summary and topics need a model call so they
# come from the nightly cache, but "when did I last commit" and "is the tree
# dirty" are the questions a chat ask is usually really about, and a cached
# answer to those is wrong for up to 24 hours. A full scan of 12 checkouts is
# ~300ms, which a chat turn can afford. Same live-fetch/cached-blurb split the
# /starred view uses.

def _slug(text: str) -> str:
    return "".join(c for c in (text or "").lower() if c.isalnum())


def _merge(row: dict, cached: dict) -> dict:
    """One scanned row plus its cached distillation, without the document
    bodies (the model has no use for 2000 chars of README here)."""
    merged = {k: v for k, v in row.items() if k not in ("readme", "claude_md")}
    merged["summary"] = cached.get("summary", "")
    merged["topics"] = cached.get("topics", [])
    return merged


def list_projects() -> dict:
    """Every project checkout with its one-line summary and how recently it was
    touched. Errors from the scan pass through."""
    scanned = scan_projects()
    if "error" in scanned:
        return scanned
    cached = {p["name"]: p for p in load_registry()}
    return {"projects": [
        {k: v for k, v in _merge(row, cached.get(row["name"], {})).items()
         if k not in ("topics", "doc_titles", "docs_found", "content_hash", "path")}
        for row in scanned["projects"]
    ]}


def read_project(name: str) -> dict:
    """One project in full: its current git state, what it is, and the wiki page
    covering it if there is one. Matching is forgiving about case and spacing
    ("weigh anchor" finds WeighAnchor) because the model is passing back a name
    the user typed, not an identifier it was given."""
    if not name or not name.strip():
        return {"error": "name must not be empty"}
    scanned = scan_projects()
    if "error" in scanned:
        return scanned

    wanted = _slug(name)
    match = next((r for r in scanned["projects"] if _slug(r["name"]) == wanted), None)
    if match is None:
        known = [r["name"] for r in scanned["projects"]]
        return {"error": f"no project named '{name}'. Projects: {', '.join(known)}"}

    cached = {p["name"]: p for p in load_registry()}
    project = _merge(match, cached.get(match["name"], {}))

    # The wiki page carries the reasoning the README doesn't — why it was built
    # this way. Joined on the page's `path:` frontmatter, since page names and
    # directory names routinely disagree (the page for LocalLLMAgent is `wren`).
    project["wiki_page"] = None
    try:
        for page in list_project_pages()["projects"]:
            if page.get("path") == match["name"]:
                project["wiki_page"] = {"name": page["name"], "summary": page["summary"]}
                break
    except Exception:
        pass  # the vault is optional; its absence costs the page, not the answer
    return project


LIST_PROJECTS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "list_projects",
        # A catalogue tool — the one shape where pretraining supplies a
        # *plausible* answer, so the model skips the tool and invents entries.
        # list_games hit exactly this: asked the vague "let's play a game" it
        # named Wordle and Chess with fabricated links in 2 of 12 replays, and
        # only the flat "this is NOT something you know" wording fixed it. The
        # risk is higher here — a made-up project name sounds completely
        # ordinary, and nothing in the reply would look wrong.
        "description": (
            f"List {_NAME}'s software projects — the checkouts on this machine — each with "
            "a one-line summary, when it was last committed to, and whether it has "
            f"uncommitted changes. Call this whenever {_NAME} asks about his projects, what "
            "he is working on or has built, what is stale, or names something that might be "
            "a project. This list is NOT something you know: only the projects this tool "
            "returns exist. Never name a project from your own knowledge, never guess that "
            "one exists, and never invent a repository — if the tool returns nothing, say "
            "there are no projects set up. Use read_project for the detail on any one of them."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}

READ_PROJECT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_project",
        "description": (
            f"Read one of {_NAME}'s projects in full: what it is and what it's about, its "
            "current git state (branch, last commit, commits in the last 30 days, whether "
            "the tree is dirty), the titles of its docs pages, and the wiki page covering it "
            "if there is one. Pass a name from list_projects — call that first if you don't "
            "already have the list this turn, rather than guessing a name."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "description": "Project name, e.g. 'WeighAnchor' (case and spacing are forgiving)."},
            },
            "required": ["name"],
        },
    },
}

PROJECT_TOOL_SCHEMAS = [LIST_PROJECTS_SCHEMA, READ_PROJECT_SCHEMA]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd")
    p_scan = sub.add_parser("scan", help="Raw scan, as the nightly task sees it.")
    p_scan.add_argument("--brief", action="store_true",
                        help="Omit the readme/claude_md bodies from the output.")
    sub.add_parser("list", help="The list_projects tool.")
    p_read = sub.add_parser("read", help="The read_project tool.")
    p_read.add_argument("name")
    args = parser.parse_args()

    if args.cmd == "list":
        return print_result(list_projects())
    if args.cmd == "read":
        return print_result(read_project(args.name))

    result = scan_projects()
    if getattr(args, "brief", False) and "projects" in result:
        result = {"projects": [{k: v for k, v in row.items()
                                if k not in ("readme", "claude_md")}
                               for row in result["projects"]]}
    return print_result(result)


if __name__ == "__main__":
    sys.exit(main())
