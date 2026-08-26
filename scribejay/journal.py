"""Journaling-only helpers — the parts of a daily entry that are ScribeJay's alone.

Everything ScribeJay SHARES with Wren's tasks/daily_synthesis.py (the prior-day
window, the prompt-bounding compaction, the vault write with its email fallback)
lives in agent/activity_log.py instead, because neither agent may import the
other. What is here is rendering and quality-checking of a journal entry, which
synthesis has no use for.
"""

from tasks._urls import safe_url


def videos_section(videos: list) -> str:
    """Deterministic Markdown section listing every video Liked, with a link to
    each. Built in Python (not asked of the model) so the titles and URLs are
    exact and every link is scheme-validated. Titles keep their raw text; only a
    bad-scheme URL is dropped (the title then renders unlinked)."""
    lines = ["### Videos Liked"]
    if not videos:
        lines.append("- **None:** [No videos Liked this day]")
        return "\n".join(lines)
    for v in videos:
        title = (v.get("title") or "Untitled").strip()
        channel = (v.get("channel") or "").strip()
        url = safe_url(v.get("url") or "")
        label = f"[{title}]({url})" if url else title
        lines.append(f"- {label}{f' — {channel}' if channel else ''}")
    return "\n".join(lines)


def has_substantive_content(text: str) -> bool:
    """True if the draft has at least one real bullet — i.e. a bullet that isn't
    the template's "**None:**" empty-section marker. Lets a task skip writing a
    log whose every section came back empty rather than save an all-"None" file."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("- ") and "**None:**" not in stripped:
            return True
    return False


def commit_totals_line(commits: list) -> str:
    """Per-repo commit and line totals as one deterministic line.

    Arithmetic the model is never asked for (CLAUDE.md: deterministic Python owns
    structure). It is also the footnote that makes the drafted bullets checkable —
    a draft claiming a big day under a two-commit total is visibly wrong."""
    if not commits:
        return "*No commits.*"
    totals: dict = {}
    for c in commits:
        row = totals.setdefault(c["repo"], {"commits": 0, "insertions": 0, "deletions": 0})
        row["commits"] += 1
        row["insertions"] += c["insertions"]
        row["deletions"] += c["deletions"]
    parts = [
        f"{repo} — {row['commits']} commit{'s' if row['commits'] != 1 else ''}, "
        f"+{row['insertions']:,}/-{row['deletions']:,}"
        for repo, row in sorted(totals.items())
    ]
    return "*" + " · ".join(parts) + "*"
