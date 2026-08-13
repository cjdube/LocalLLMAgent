"""Tests for the pure helpers in tasks.morning_brief.

Importing tasks.morning_brief pulls in the whole brief pipeline module but runs
no network — only the small pure functions (_clean_snippet, _tasks_html) are
exercised here. The scheme allow-list it renders links through now lives in
tasks._urls; see tests/test_urls.py.
"""

from datetime import date

import pytest

from tasks import morning_brief as mb

TODAY = date(2026, 7, 7)  # a Tuesday


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
# _events_html
# --------------------------------------------------------------------------- #

def test_events_html_empty_state_names_the_configured_window():
    assert f"next {mb.CALENDAR_HOURS_AHEAD} hours" in mb._events_html([])
    assert "next 72 hours" in mb._events_html([], hours_ahead=72)


def test_events_html_lists_event_with_date_and_time():
    out = mb._events_html([{"summary": "Dentist", "start": "2026-07-08T14:30:00-04:00"}])
    assert "Jul 8" in out and "2:30 PM" in out and "Dentist" in out


def test_events_html_escapes_summary():
    out = mb._events_html([{"summary": "<script>alert(1)</script>", "start": "2026-07-08T09:00:00-04:00"}])
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


# --------------------------------------------------------------------------- #
# _glance_buckets — which events the model is told are today's
# --------------------------------------------------------------------------- #

@pytest.fixture(autouse=True)
def _pin_timezone(monkeypatch):
    # Pinned rather than inherited: the local and UTC day agree for most of the
    # day and diverge only in the evening, so a host-zone test passes by luck.
    monkeypatch.setenv("TIMEZONE", "America/New_York")


def test_glance_buckets_separates_today_from_later():
    events = [
        {"summary": "Standup", "start": "2026-07-07T09:00:00-04:00"},
        {"summary": "Dentist", "start": "2026-07-08T14:30:00-04:00"},
    ]
    todays, later = mb._glance_buckets(events, today=TODAY)
    assert todays == [{"when": "today 9:00 AM", "summary": "Standup"}]
    assert later == [{"when": "tomorrow 2:30 PM", "summary": "Dentist"}]


def test_glance_buckets_names_the_weekday_beyond_tomorrow():
    events = [{"summary": "Flight", "start": "2026-07-10T06:15:00-04:00"}]
    _, later = mb._glance_buckets(events, today=TODAY)
    assert later == [{"when": "Friday, Jul 10 6:15 AM", "summary": "Flight"}]


def test_glance_buckets_evening_event_stays_on_its_local_day():
    # 00:30 UTC on Jul 8 is 8:30 PM on Jul 7 in New York — the boundary case
    # where slicing the ISO string would file tonight's event under tomorrow.
    events = [{"summary": "Concert", "start": "2026-07-08T00:30:00+00:00"}]
    todays, later = mb._glance_buckets(events, today=TODAY)
    assert todays == [{"when": "today 8:30 PM", "summary": "Concert"}]
    assert later == []


def test_glance_buckets_all_day_event_has_no_time():
    events = [
        {"summary": "Holiday", "start": "2026-07-07"},
        {"summary": "Conference", "start": "2026-07-08"},
    ]
    todays, later = mb._glance_buckets(events, today=TODAY)
    assert todays == [{"when": "today", "summary": "Holiday"}]
    assert later == [{"when": "tomorrow", "summary": "Conference"}]


def test_glance_buckets_unparseable_start_is_never_called_today():
    todays, later = mb._glance_buckets([{"summary": "Mystery", "start": "soon"}], today=TODAY)
    assert todays == []
    assert later == [{"when": "date unknown", "summary": "Mystery"}]


def test_glance_buckets_empty_input():
    assert mb._glance_buckets([], today=TODAY) == ([], [])


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


# --------------------------------------------------------------------------- #
# starred-repo state: atomic write
# --------------------------------------------------------------------------- #

def test_starred_state_round_trips_and_leaves_no_temp_files(tmp_path, monkeypatch):
    monkeypatch.setattr(mb, "STARRED_STATE_PATH", tmp_path / "github_starred_state.json")

    mb._write_starred_state("2026-07-07T12:00:00+00:00")

    assert mb._read_starred_state() == "2026-07-07T12:00:00+00:00"
    assert list(tmp_path.glob("*.tmp")) == []
