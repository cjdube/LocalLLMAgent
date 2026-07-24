"""Tests for the pure string helpers in agent.tools.github_starred."""

import requests

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


# --------------------------------------------------------------------------- #
# _version_core / compare_versions
# --------------------------------------------------------------------------- #

def test_version_core_strips_scheme_prefixes():
    assert gh._version_core("skill-v4.0.2") == "4.0.2"
    assert gh._version_core("app-v0.2.0") == "0.2.0"
    assert gh._version_core("v2.11.0") == "2.11.0"
    assert gh._version_core("0.6.4") == "0.6.4"


def test_version_core_empty_when_no_digits():
    assert gh._version_core("nightly") == ""
    assert gh._version_core("") == ""
    assert gh._version_core(None) == ""


def test_compare_versions_outdated_when_behind():
    # Installed core is behind the latest release tag -> True (update available).
    assert gh.compare_versions("0.43.0", "v0.44.0") is True
    assert gh.compare_versions("skill-v4.0.2", "skill-v4.1.0") is True


def test_compare_versions_current_when_equal_across_schemes():
    # A bare installed version matches the release's v-prefixed tag.
    assert gh.compare_versions("0.43.0", "v0.43.0") is False


def test_compare_versions_false_when_ahead():
    assert gh.compare_versions("v2.0.0", "v1.9.9") is False


def test_compare_versions_none_when_either_missing():
    assert gh.compare_versions(None, "v1.0") is None
    assert gh.compare_versions("v1.0", None) is None
    assert gh.compare_versions("", "v1.0") is None


def test_compare_versions_none_when_unparseable():
    # A version with no numeric core is uncomparable -> None, never a false True.
    assert gh.compare_versions("nightly", "v1.0.0") is None


# --------------------------------------------------------------------------- #
# fetch_latest_release (degrade-don't-crash)
# --------------------------------------------------------------------------- #

class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_fetch_latest_release_happy_path(monkeypatch):
    monkeypatch.setattr(gh, "resolve_key", lambda *a, **k: "tok")
    monkeypatch.setattr(gh.requests, "get", lambda *a, **k: _Resp({
        "tag_name": "v1.3.0",
        "name": "Release 1.3.0",
        "published_at": "2026-07-01T00:00:00Z",
        "html_url": "https://github.com/o/r/releases/tag/v1.3.0",
    }))
    assert gh.fetch_latest_release("o/r") == {
        "tag": "v1.3.0",
        "name": "Release 1.3.0",
        "published_at": "2026-07-01T00:00:00Z",
        "html_url": "https://github.com/o/r/releases/tag/v1.3.0",
    }


def test_fetch_latest_release_name_falls_back_to_tag(monkeypatch):
    monkeypatch.setattr(gh, "resolve_key", lambda *a, **k: "tok")
    monkeypatch.setattr(gh.requests, "get", lambda *a, **k: _Resp({
        "tag_name": "v2", "name": None, "published_at": None, "html_url": "",
    }))
    assert gh.fetch_latest_release("o/r")["name"] == "v2"


def test_fetch_latest_release_no_key_returns_empty(monkeypatch):
    monkeypatch.setattr(gh, "resolve_key", lambda *a, **k: None)
    assert gh.fetch_latest_release("o/r") == {}


def test_fetch_latest_release_404_returns_empty(monkeypatch):
    monkeypatch.setattr(gh, "resolve_key", lambda *a, **k: "tok")

    def _raise(*a, **k):
        raise requests.exceptions.RequestException("404")

    monkeypatch.setattr(gh.requests, "get", _raise)
    assert gh.fetch_latest_release("o/r") == {}


def test_fetch_latest_release_missing_tag_returns_empty(monkeypatch):
    monkeypatch.setattr(gh, "resolve_key", lambda *a, **k: "tok")
    monkeypatch.setattr(gh.requests, "get", lambda *a, **k: _Resp({"name": "no tag"}))
    assert gh.fetch_latest_release("o/r") == {}
