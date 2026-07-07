"""Tests for the pure helpers in tasks.morning_brief.

Importing tasks.morning_brief pulls in the whole brief pipeline module but runs
no network — only the small pure functions (_safe_url, _clean_snippet,
_sort_by_recency, _parse_pub_date) are exercised here.
"""

from tasks import morning_brief as mb


# --------------------------------------------------------------------------- #
# _safe_url — scheme allow-list guarding externally-sourced URLs
# --------------------------------------------------------------------------- #

def test_safe_url_allows_http():
    assert mb._safe_url("http://example.com/x") == "http://example.com/x"


def test_safe_url_allows_https():
    assert mb._safe_url("https://example.com/x") == "https://example.com/x"


def test_safe_url_rejects_javascript_scheme():
    assert mb._safe_url("javascript:alert(1)") == ""


def test_safe_url_rejects_data_scheme():
    assert mb._safe_url("data:text/html,<script>alert(1)</script>") == ""


def test_safe_url_rejects_other_schemes():
    assert mb._safe_url("ftp://example.com/x") == ""


def test_safe_url_rejects_relative_and_empty():
    assert mb._safe_url("/relative/path") == ""
    assert mb._safe_url("") == ""


# --------------------------------------------------------------------------- #
# _clean_snippet
# --------------------------------------------------------------------------- #

def test_clean_snippet_strips_heading_markers():
    assert mb._clean_snippet("## Heading\n\nBody text") == "Heading Body text"


def test_clean_snippet_collapses_whitespace():
    assert mb._clean_snippet("a\t b   c\nd") == "a b c d"


def test_clean_snippet_truncates_on_word_boundary():
    got = mb._clean_snippet("alpha beta gamma delta epsilon zeta", max_len=20)
    assert got.endswith("…")
    assert " " not in got[-2:]


def test_clean_snippet_short_text_unchanged():
    assert mb._clean_snippet("short and clean") == "short and clean"


# --------------------------------------------------------------------------- #
# _parse_pub_date / _sort_by_recency
# --------------------------------------------------------------------------- #

def test_parse_pub_date_valid_rfc2822():
    dt = mb._parse_pub_date("Tue, 07 Jul 2026 10:00:00 GMT")
    assert dt is not None and dt.year == 2026 and dt.month == 7 and dt.day == 7


def test_parse_pub_date_empty_is_none():
    assert mb._parse_pub_date("") is None


def test_parse_pub_date_garbage_is_none():
    assert mb._parse_pub_date("not a date") is None


def test_sort_by_recency_newest_first():
    articles = [
        {"id": "old", "published_date": "Mon, 06 Jul 2026 10:00:00 GMT"},
        {"id": "new", "published_date": "Tue, 07 Jul 2026 10:00:00 GMT"},
    ]
    ordered = [a["id"] for a in mb._sort_by_recency(articles)]
    assert ordered == ["new", "old"]


def test_sort_by_recency_undated_sinks_to_bottom():
    articles = [
        {"id": "undated", "published_date": ""},
        {"id": "dated", "published_date": "Tue, 07 Jul 2026 10:00:00 GMT"},
    ]
    ordered = [a["id"] for a in mb._sort_by_recency(articles)]
    assert ordered == ["dated", "undated"]
