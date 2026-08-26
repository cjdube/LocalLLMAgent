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
