"""Tests for the pure string helpers in agent.tools.github_starred."""

from agent.tools import github_starred as gh


# --------------------------------------------------------------------------- #
# _count_suffix
# --------------------------------------------------------------------------- #

def test_count_suffix_empty_when_nothing_extra():
    assert gh._count_suffix(1, 1, has_more=False, noun="release") == ""


def test_count_suffix_plural():
    assert gh._count_suffix(1, 3, has_more=False, noun="release") == " (+2 more releases)"


def test_count_suffix_singular():
    assert gh._count_suffix(1, 2, has_more=False, noun="release") == " (+1 more release)"


def test_count_suffix_capped_shows_plus():
    # has_more marks a capped fetch -> honest "N+" rather than an exact count.
    assert gh._count_suffix(3, 50, has_more=True, noun="commit") == " (+47+ more commits)"


# --------------------------------------------------------------------------- #
# _first_content_line
# --------------------------------------------------------------------------- #

def test_first_content_line_skips_changelog_heading():
    body = "## Changelog\n- Fix the crash on startup"
    assert gh._first_content_line(body) == "Fix the crash on startup"


def test_first_content_line_strips_leading_commit_hash():
    body = "- `8b9cade` Fix the thing"
    assert gh._first_content_line(body) == "Fix the thing"


def test_first_content_line_strips_backticked_and_bare_hash():
    assert gh._first_content_line("* 1234567 bump deps") == "bump deps"


def test_first_content_line_skips_empty_heading_lines():
    # A bare '###' (hashes only) is skipped; the first line with real content
    # wins, with any leading '#' markers stripped off.
    body = "###\nActual content here"
    assert gh._first_content_line(body) == "Actual content here"


def test_first_content_line_strips_markers_from_heading_with_text():
    # With no bullets, a text-bearing heading is returned sans its markers.
    assert gh._first_content_line("# Release notes") == "Release notes"


def test_first_content_line_empty_body():
    assert gh._first_content_line("") == ""


def test_first_content_line_only_hashes():
    assert gh._first_content_line("####\n#  \n") == ""


# --------------------------------------------------------------------------- #
# _truncate
# --------------------------------------------------------------------------- #

def test_truncate_collapses_whitespace():
    assert gh._truncate("a  b\n\tc") == "a b c"


def test_truncate_short_text_unchanged():
    assert gh._truncate("hello world", max_len=200) == "hello world"


def test_truncate_long_text_adds_ellipsis_on_word_boundary():
    text = "alpha beta gamma delta epsilon zeta"
    got = gh._truncate(text, max_len=20)
    assert got.endswith("…")
    assert len(got) <= 21  # 20 chars (trimmed to a word) + the ellipsis
    assert " " not in got[-2:]  # trimmed at a word boundary, no dangling space


# --------------------------------------------------------------------------- #
# _parse_since (Z-normalization)
# --------------------------------------------------------------------------- #

def test_parse_since_normalizes_trailing_z():
    dt = gh._parse_since("2026-06-01T00:00:00Z")
    assert dt.year == 2026 and dt.tzinfo is not None
