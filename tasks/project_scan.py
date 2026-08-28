"""Refresh the local project registry and distil each project into an anchor.

Non-interactive — run by launchd each morning, between the daily learnings tasks
and daily_synthesis, so the day's anchors are current when synthesis reads them.

Two halves. The scan (agent/tools/projects.py) is deterministic and free: git
freshness and the project's own docs, straight off the disk. The distillation is
one isolated model call per project, so the context window can't overflow no
matter how many checkouts exist — project count drives the number of calls, not
the size of any prompt. Same shape as tasks/starred_blurbs.py, and cached the
same way: a blurb is regenerated only when the project's docs actually change
(content_hash), so the daily run is usually a git refresh and zero model calls.

Why a distillation rather than the raw docs: daily_synthesis matches by token
overlap, normalized by the *smaller* token set. An anchor carrying a whole
README plus project instructions would be thousands of tokens, share something
with every signal, and outrank every real pair — the exact bug documented on
daily_synthesis._ai_chat_signals, which reads the distilled chat log instead of
raw transcripts for this reason. So the model returns a one-line summary and a
short topic list, and the anchor built from them stays the same size class as a
wiki page's.

A project with no README, no agent instructions and no docs/ still gets a row
(its git facts are real), but no blurb — there is nothing to distil, and an
anchor whose only token is its own name can match nothing but its own spelling.
Those are logged by name: the fix is a README in that repo, not code here.

Usage:
    python -m tasks.project_scan
    python -m tasks.project_scan --refresh
"""

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

from agent import prefs
from agent.loop import complete_text, resolve_backend, warm_model
from agent.store import atomic_write_json, locked
from agent.tools import projects as projects_tool
from agent.tools.projects import load_registry, scan_projects
from tasks._common import notify_failure, setup_logger

_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / "config" / ".env")

# How many topic terms to keep. The anchor's token set is name + summary +
# topics; a wiki-page anchor (name + a one-line summary) lands around 20-25
# tokens after _tokenize drops short words, so this keeps a project anchor in
# the same size class instead of letting it dominate the shortlist.
MAX_TOPICS = 15

# One line, so this rarely binds — it's here so a runaway summary can't blow
# past daily_synthesis's own MAX_ANCHOR_SUMMARY_CHARS and waste prompt budget.
MAX_SUMMARY_CHARS = 300

DISTILL_SYSTEM_PROMPT = """You summarize a software project from its own \
documentation, for an index of the user's projects. You will be given a project \
name, its README, its agent instructions if it has any, and the titles of its \
docs pages.

Output EXACTLY two lines, nothing else:

summary: <one plain sentence saying what this project is and does>
topics: <8 to 15 comma-separated technical terms this project is about>

For topics, pick the specific and distinctive terms — the technologies, \
protocols, domain concepts and techniques this project actually uses. Skip \
generic words like software, project, code, application, tool, system.

No markdown, no headings, no quotes, no preamble. Just the two lines."""

_SUMMARY_RE = re.compile(r"^\s*summary\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
_TOPICS_RE = re.compile(r"^\s*topics\s*:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)


def has_docs(row: dict) -> bool:
    """Whether a scanned project has anything for the model to read."""
    return bool(row.get("readme") or row.get("agent_instructions")
                or row.get("doc_titles"))


def render_prompt(row: dict) -> str:
    """One project's documentation as the model's user prompt. Only the fields
    agent/tools/projects.py is allowed to read appear here."""
    parts = [f"project: {row['name']}"]
    if row.get("readme"):
        parts.append(f"README:\n{row['readme']}")
    if row.get("agent_instructions"):
        parts.append(f"agent instructions:\n{row['agent_instructions']}")
    if row.get("doc_titles"):
        parts.append("docs pages: " + ", ".join(row["doc_titles"]))
    return "\n\n".join(parts)


def parse_distillation(raw: str) -> dict:
    """{"summary": str, "topics": [str]} from the model's two lines. Either
    field may come back empty — the caller decides what that means, and logs
    it. Defensive by design: this is a fixed-format parse of small-model output,
    so a missing line is an expected outcome, not an exception."""
    text = raw or ""
    summary_match = _SUMMARY_RE.search(text)
    topics_match = _TOPICS_RE.search(text)

    summary = (summary_match.group(1).strip() if summary_match else "")[:MAX_SUMMARY_CHARS]
    topics = []
    if topics_match:
        for term in topics_match.group(1).split(","):
            term = term.strip().strip(".").lower()
            if term and term not in topics:
                topics.append(term)
    return {"summary": summary, "topics": topics[:MAX_TOPICS]}


def _distill(row: dict, backend, logger) -> dict:
    """One isolated completion for one project. Falls back to the README's first
    non-empty non-heading line when the model returns no usable summary, so a
    project still carries *something* — but an empty topics list is left empty
    and reported by the caller rather than papered over."""
    raw = complete_text(
        system_prompt=DISTILL_SYSTEM_PROMPT,
        user_prompt=render_prompt(row),
        backend=backend,
        logger=logger,   # surfaces loop.py's num_predict cut-off warning
        think=False,     # fixed two-line output; see complete_text
    )
    logger.info(f"distill {row['name']} -> {raw!r}")
    parsed = parse_distillation(raw)

    if not parsed["summary"]:
        for line in (row.get("readme") or "").splitlines():
            line = line.strip().lstrip("#").strip()
            if line:
                parsed["summary"] = line[:MAX_SUMMARY_CHARS]
                break
        logger.warning(
            f"{row['name']}: model returned no usable summary line; "
            f"fell back to the README's first line (raw length {len(raw or '')})"
        )
    return parsed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true",
                        help="Regenerate every distillation, not just the changed ones.")
    args = parser.parse_args(argv)

    logger = setup_logger("project_scan")
    logger.info("Starting project scan run")

    try:
        scanned = scan_projects()
        if "error" in scanned:
            logger.error(f"scan_projects failed: {scanned['error']}")
            notify_failure("project_scan", scanned["error"], logger)
            return 1
        rows = scanned["projects"]
        logger.info(f"{len(rows)} project(s) under the projects dir")

        # Cached distillations keyed by project name, so a rename regenerates
        # (the right call — a renamed project is usually a repurposed one).
        cached = {p["name"]: p for p in load_registry()}

        documented = [r for r in rows if has_docs(r)]
        undocumented = [r["name"] for r in rows if not has_docs(r)]
        if undocumented:
            # Not a failure — but it IS why those projects will never surface in a
            # synthesis nudge, and that would otherwise be invisible. Naming them
            # makes the fix obvious (add a README to that repo).
            allowed = ", ".join(prefs.project_instruction_files())
            logger.warning(
                f"{len(undocumented)} project(s) have no README, configured project "
                f"instructions ({allowed}), or docs/ and so get no anchor: "
                f"{', '.join(undocumented)}"
            )

        # Same reasoning one step further in: a project whose docs/ outgrew
        # MAX_DOC_TITLES still distils fine, it just distils off a truncated
        # picture. Nothing else would ever say so — the blurb looks normal.
        cap = projects_tool.MAX_DOC_TITLES   # module attribute: resolved at call time
        truncated = [f"{r['name']} ({r['docs_found']} docs, capped at {cap})"
                     for r in rows if r.get("docs_found", 0) > len(r.get("doc_titles", []))]
        if truncated:
            logger.warning(
                f"{len(truncated)} project(s) have more docs/ pages than the cap, so "
                f"the tail is missing from their anchor: {', '.join(truncated)}"
            )

        todo = [r for r in documented
                if args.refresh
                or cached.get(r["name"], {}).get("content_hash") != r["content_hash"]
                or not cached.get(r["name"], {}).get("summary")]
        logger.info(f"{len(todo)} of {len(documented)} documented project(s) need distilling")

        distilled = {}
        if todo:
            backend = resolve_backend("project_scan")
            warm_model(logger=logger, backend=backend)
            for row in todo:
                distilled[row["name"]] = _distill(row, backend, logger)

        out = []
        for row in rows:
            prior = cached.get(row["name"], {})
            fresh = distilled.get(row["name"])
            entry = {k: v for k, v in row.items()
                     if k not in ("readme", "agent_instructions")}
            if fresh is not None:
                entry["summary"] = fresh["summary"]
                entry["topics"] = fresh["topics"]
                entry["distilled_at"] = datetime.now(timezone.utc).isoformat()
            else:
                entry["summary"] = prior.get("summary", "")
                entry["topics"] = prior.get("topics", [])
                entry["distilled_at"] = prior.get("distilled_at")
            out.append(entry)

        # A thin topics list is the silent degradation this task is most prone
        # to: the project still appears, still has a summary, and simply stops
        # matching anything. Report it with counts rather than letting the
        # anchor quietly shrink.
        thin = [e["name"] for e in out
                if e["name"] in distilled and len(e["topics"]) < 4]
        if thin:
            logger.warning(
                f"{len(thin)} of {len(distilled)} distilled project(s) came back with "
                f"fewer than 4 topics and will barely match anything: {', '.join(thin)}"
            )

        # Whole store rewritten from the scan each run, so a deleted project is
        # pruned rather than lingering as a phantom anchor.
        with locked(projects_tool.PROJECTS_PATH):
            atomic_write_json(projects_tool.PROJECTS_PATH, {
                "scanned_at": datetime.now(timezone.utc).isoformat(),
                "projects": out,
            })
        logger.info(f"Wrote {len(out)} project(s); project scan run complete")
        return 0
    except Exception as e:
        logger.exception(f"Project scan run failed: {e}")
        notify_failure("project_scan", e, logger)
        return 1


if __name__ == "__main__":
    sys.exit(main())
