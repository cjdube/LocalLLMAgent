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
# _scores_html
# --------------------------------------------------------------------------- #

def _game(**overrides):
    game = {
        "league": "mlb", "team": "Red Sox", "opponent": "Reds", "home_away": "home",
        "team_score": 5, "opponent_score": 3, "result": "W", "status": "Final",
        "final": True, "start_local": "2026-07-06T19:10:00-04:00",
        "game_number": 1, "games_that_day": 1,
        "url": "https://www.espn.com/mlb/game/_/gameId/1",
    }
    game.update(overrides)
    return game


def test_scores_html_empty_produces_no_section():
    # The NFL is dark half the year, so "no games" is the normal state — an
    # empty string is what tells render_brief_html to drop the section.
    assert mb._scores_html([], None) == ""
    assert mb._scores_html([], {}) == ""


def test_scores_html_renders_a_final():
    out = mb._scores_html([_game()], None)
    assert "Red Sox 5, Reds 3" in out
    assert "Red Sox beat Reds" in out


def test_scores_html_labels_doubleheader_games():
    games = [
        _game(game_number=1, games_that_day=2, team_score=5, opponent_score=3, result="W"),
        _game(game_number=2, games_that_day=2, team_score=4, opponent_score=8, result="L"),
    ]
    out = mb._scores_html(games, None)
    assert "Game 1:" in out and "Game 2:" in out
    assert "Red Sox lost to Reds" in out


def test_scores_html_omits_game_number_for_a_single_game():
    assert "Game 1" not in mb._scores_html([_game()], None)


def test_scores_html_shows_status_instead_of_a_score_when_not_final():
    out = mb._scores_html(
        [_game(final=False, result=None, team_score=None,
               opponent_score=None, status="Postponed")], None)
    assert "Postponed" in out
    assert "Red Sox 5" not in out


def test_scores_html_surfaces_a_fetch_error():
    # "We couldn't tell" must not look like "nobody played".
    out = mb._scores_html([], {"nfl": "network error: timed out"})
    assert "NFL scores unavailable" in out and "timed out" in out


def test_scores_html_drops_a_dangerous_url():
    out = mb._scores_html([_game(url="javascript:alert(1)")], None)
    assert "javascript:" not in out
    assert "box score" not in out
    assert "Red Sox 5, Reds 3" in out  # the game still renders, just unlinked


def test_scores_html_escapes_team_names():
    out = mb._scores_html([_game(team="<script>x</script>")], None)
    assert "<script>" not in out


def test_render_brief_omits_scores_section_when_there_is_nothing():
    args = ({"error": "n/a"}, [], [], "glance", [], "")
    assert "Scores" not in mb.render_brief_html(*args)
    assert "Scores" in mb.render_brief_html(*args, scores=[_game()])


# --------------------------------------------------------------------------- #
# starred-repo state: atomic write
# --------------------------------------------------------------------------- #

def test_starred_state_round_trips_and_leaves_no_temp_files(tmp_path, monkeypatch):
    monkeypatch.setattr(mb, "STARRED_STATE_PATH", tmp_path / "github_starred_state.json")

    mb._write_starred_state("2026-07-07T12:00:00+00:00")

    assert mb._read_starred_state() == "2026-07-07T12:00:00+00:00"
    assert list(tmp_path.glob("*.tmp")) == []


# --------------------------------------------------------------------------- #
# Backlog section: two stacked lists, no model call
# --------------------------------------------------------------------------- #

def _row(title, space="Wren", status="building", change=None, days=1):
    row = {"title": title, "space": space, "status": status,
           "updated": "2026-08-26", "days_since_update": days}
    if change:
        row["change"] = change
    return row


def test_backlog_html_empty_state_covers_both_halves():
    """One empty state, not two: a quiet day should read as one short line."""
    out = mb._backlog_html({})
    assert "Nothing in flight and nothing moved" in out
    assert "<ul>" not in out


def test_backlog_html_stacks_moved_above_in_flight():
    out = mb._backlog_html({
        "moved": [_row("Shipped thing", change="shipped")],
        "moved_total": 1,
        "in_flight": [_row("Being built")],
        "in_flight_total": 1,
    })
    assert out.index("Moved since yesterday") < out.index("In flight")
    assert "Shipped thing" in out and "shipped" in out
    assert "Being built" in out


def test_backlog_html_shows_one_half_when_the_other_is_empty():
    """A quiet night still has work in flight, and a day with no active items
    can still have shipped something. Neither heading may appear empty."""
    moved_only = mb._backlog_html({"moved": [_row("Landed", change="shipped")], "moved_total": 1})
    assert "Moved since yesterday" in moved_only
    assert "In flight" not in moved_only

    flight_only = mb._backlog_html({"in_flight": [_row("Building")], "in_flight_total": 1})
    assert "In flight" in flight_only
    assert "Moved since yesterday" not in flight_only


def test_backlog_html_admits_what_the_caps_dropped():
    """The counts come from the digest's totals, so a capped list says how much
    it is hiding instead of reading as the whole backlog."""
    out = mb._backlog_html({
        "moved": [_row("A", change="added")],
        "moved_total": 5,
        "in_flight": [_row("B")],
        "in_flight_total": 3,
    })
    assert "+4 more moved" in out
    assert "+2 more in flight" in out


def test_backlog_html_omits_the_more_line_when_nothing_was_dropped():
    out = mb._backlog_html({"in_flight": [_row("B")], "in_flight_total": 1})
    assert "more in flight" not in out


def test_backlog_html_names_the_stalled_item_with_its_age():
    out = mb._backlog_html({
        "in_flight": [_row("Fresh", days=0), _row("Forgotten", days=19)],
        "in_flight_total": 2,
        "stalest": _row("Forgotten", days=19),
    })
    assert "Untouched longest: Forgotten (19 days)" in out


def test_backlog_html_has_no_stale_line_when_the_digest_sent_none():
    out = mb._backlog_html({"in_flight": [_row("Fresh", days=0)], "in_flight_total": 1})
    assert "Untouched longest" not in out


def test_backlog_html_escapes_titles_from_clickup():
    """Item titles are typed by hand into ClickUp and land in an HTML email."""
    out = mb._backlog_html({
        "in_flight": [_row("<script>alert(1)</script>")],
        "in_flight_total": 1,
    })
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_backlog_html_surfaces_a_fetch_error_instead_of_faking_a_quiet_day():
    """"Nothing moved" and "ClickUp is down" must never look the same."""
    out = mb._backlog_html({}, error="timeout")
    assert "Backlog unavailable" in out
    assert "timeout" in out
    assert "Nothing in flight" not in out


def test_render_brief_always_carries_a_backlog_section():
    args = ({"error": "n/a"}, [], [], "glance", [], "")
    assert "Backlog" in mb.render_brief_html(*args)


def test_clickup_state_round_trips_and_leaves_no_temp_files(tmp_path, monkeypatch):
    monkeypatch.setattr(mb, "CLICKUP_STATE_PATH", tmp_path / "clickup_state.json")
    assert mb._read_clickup_state() is None

    mb._write_clickup_state(1787826143872)

    assert mb._read_clickup_state() == 1787826143872
    assert list(tmp_path.glob("*.tmp")) == []
