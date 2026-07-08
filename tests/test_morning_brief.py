"""Tests for the pure helpers in tasks.morning_brief.

Importing tasks.morning_brief pulls in the whole brief pipeline module but runs
no network — only the small pure functions (_safe_url, _clean_snippet,
_sort_by_recency, _parse_pub_date, _tasks_html) are exercised here.
"""

from datetime import date

from tasks import morning_brief as mb

TODAY = date(2026, 7, 7)  # a Tuesday


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


# --------------------------------------------------------------------------- #
# _tasks_html
# --------------------------------------------------------------------------- #

def test_tasks_html_empty_state():
    assert "Nothing past due or due soon" in mb._tasks_html([], today=TODAY)


def test_tasks_html_overdue_label():
    tasks = [{"title": "Pay invoice", "due": "2026-07-05T00:00:00.000Z"}]
    out = mb._tasks_html(tasks, today=TODAY)
    assert 'class="overdue"' in out
    assert "Overdue" in out
    assert "Pay invoice" in out


def test_tasks_html_today_label():
    tasks = [{"title": "Water plants", "due": "2026-07-07T00:00:00.000Z"}]
    out = mb._tasks_html(tasks, today=TODAY)
    assert "Today" in out
    assert 'class="overdue"' not in out


def test_tasks_html_future_date_label():
    tasks = [{"title": "Renew registration", "due": "2026-07-09T00:00:00.000Z"}]
    out = mb._tasks_html(tasks, today=TODAY)
    assert "Thu Jul 9" in out
    assert 'class="overdue"' not in out


def test_tasks_html_undated_task_has_no_label():
    tasks = [{"title": "Someday maybe", "due": None}]
    out = mb._tasks_html(tasks, today=TODAY)
    assert "<li>Someday maybe</li>" in out


def test_tasks_html_escapes_title():
    tasks = [{"title": "<script>alert(1)</script>", "due": "2026-07-07T00:00:00.000Z"}]
    out = mb._tasks_html(tasks, today=TODAY)
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_tasks_html_surfaces_error():
    out = mb._tasks_html([], error="insufficient scope", today=TODAY)
    assert "Tasks unavailable" in out
    assert "insufficient scope" in out


def test_tasks_html_shows_list_name():
    tasks = [{"title": "Renew passport", "due": "2026-07-07T00:00:00.000Z", "list": "Travel"}]
    out = mb._tasks_html(tasks, today=TODAY)
    assert "(Travel)" in out


def test_tasks_html_omits_list_suffix_when_absent():
    tasks = [{"title": "No list info", "due": "2026-07-07T00:00:00.000Z"}]
    out = mb._tasks_html(tasks, today=TODAY)
    assert "(" not in out
