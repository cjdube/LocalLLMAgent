"""Tests for scribejay/journal.py — the journaling-only rendering helpers: the
deterministic Liked-videos list (including its URL scheme guard), the
per-repo commit totals line, and the empty-draft check. Everything ScribeJay shares with tasks/daily_synthesis.py is
tested in tests/test_activity_log.py instead."""

from scribejay import journal as lc


# --------------------------------------------------------------------------- #
# safe_url / videos_section
# --------------------------------------------------------------------------- #

def test_videos_section_renders_linked_list():
    section = lc.videos_section([
        {"title": "Git Deep Dive", "channel": "LearnThatStack",
         "url": "https://www.youtube.com/watch?v=abc"},
        {"title": "No Channel Vid", "channel": "", "url": "https://youtu.be/xyz"},
    ])
    lines = section.splitlines()
    assert lines[0] == "### Videos Liked"
    assert lines[1] == "- [Git Deep Dive](https://www.youtube.com/watch?v=abc) — LearnThatStack"
    assert lines[2] == "- [No Channel Vid](https://youtu.be/xyz)"


def test_videos_section_empty_states_none():
    assert "None" in lc.videos_section([])


def test_videos_section_drops_bad_scheme_url_but_keeps_title():
    section = lc.videos_section([
        {"title": "Sketchy", "channel": "X", "url": "javascript:alert(1)"},
    ])
    assert "- Sketchy — X" in section
    assert "javascript:" not in section


# --------------------------------------------------------------------------- #
# has_substantive_content
# --------------------------------------------------------------------------- #

def test_has_substantive_content_true_with_a_real_bullet():
    assert lc.has_substantive_content("### X\n- **GitHub:** reviewed a PR")


def test_has_substantive_content_false_when_only_none_markers():
    text = ("## Daily Log\n\n### What I Worked On\n- **None:** [No qualifying items]\n\n"
            "### Tools & Tech Encountered\n- **None:** [No qualifying items]")
    assert not lc.has_substantive_content(text)


def test_has_substantive_content_false_when_no_bullets():
    assert not lc.has_substantive_content("## Daily Log: July 12, 2026\n\nheader only")


# --------------------------------------------------------------------------- #
# commit_totals_line
# --------------------------------------------------------------------------- #

def _c(repo, insertions, deletions):
    return {"repo": repo, "insertions": insertions, "deletions": deletions}


def test_commit_totals_line_sums_per_repo():
    line = lc.commit_totals_line([
        _c("LocalLLMAgent", 2111, 127), _c("LocalLLMAgent", 10, 0), _c("ObsidianWikiAgent", 40, 26)])
    assert line == "*LocalLLMAgent — 2 commits, +2,121/-127 · ObsidianWikiAgent — 1 commit, +40/-26*"


def test_commit_totals_line_singular_for_one_commit():
    assert "1 commit," in lc.commit_totals_line([_c("r", 1, 0)])


def test_commit_totals_line_with_no_commits():
    # The task skips the write on an empty day, so this is a guard rather than a
    # path anyone renders — it must not raise.
    assert lc.commit_totals_line([]) == "*No commits.*"


# ---- closed_tasks_section: the half git cannot witness ----


def _closed(title, space, status="complete"):
    return {"title": title, "space": space, "status": status}


def test_a_closed_task_renders_with_its_space_and_status():
    out = lc.closed_tasks_section([_closed("Proposal for Acme", "Vibe Foundry")])
    assert out.splitlines()[0] == "### Closed in ClickUp"
    assert "- **Vibe Foundry:** Proposal for Acme *(complete)*" in out


def test_the_space_leads_the_line():
    """It is the part git cannot say. A Wren Task restates a commit two sections
    up; a Vibe Foundry one is the only record of that day's work anywhere."""
    line = lc.closed_tasks_section([_closed("X", "Blog")]).splitlines()[1]
    assert line.startswith("- **Blog:**")


def test_tasks_are_grouped_by_space():
    out = lc.closed_tasks_section([
        _closed("Zebra", "Wren"), _closed("Apple", "Blog"), _closed("Beta", "Wren")])
    spaces = [ln.split("**")[1] for ln in out.splitlines()[1:]]
    assert spaces == ["Blog:", "Wren:", "Wren:"]


def test_an_empty_day_renders_the_none_marker():
    """has_substantive_content reads "**None:**" as an empty section, which is how
    a day with commits but no closures still reads as a real entry."""
    out = lc.closed_tasks_section([])
    assert "**None:**" in out
    assert not lc.has_substantive_content(out)


def test_a_rendered_task_list_counts_as_substantive_content():
    """The other half of the promise above — a day of pure non-code work must not
    look empty to the same check."""
    assert lc.has_substantive_content(
        lc.closed_tasks_section([_closed("Proposal for Acme", "Vibe Foundry")]))


def test_a_title_with_a_newline_cannot_break_the_list():
    """A Task name should not contain one, but a pasted title would silently split
    one bullet into fragments — the same bug a multi-paragraph description caused
    in daily_synthesis."""
    out = lc.closed_tasks_section([_closed("Call notes\n\nsecond para", "Blog")])
    assert len(out.splitlines()) == 2
    assert "Call notes second para" in out


def test_a_task_with_no_status_still_renders():
    out = lc.closed_tasks_section([{"title": "X", "space": "Blog", "status": ""}])
    assert "- **Blog:** X" in out
    assert "*()*" not in out
