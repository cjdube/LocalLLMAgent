"""Tests for agent.dates.resolve_date — the shared, past-biased date resolver.

`today` is pinned throughout so the MM-DD past/future boundary is deterministic.
"""

from datetime import date

from agent.dates import resolve_date

TODAY = date(2026, 7, 7)  # a Tuesday


def test_today():
    assert resolve_date("today", today=TODAY) == "2026-07-07"


def test_today_is_case_and_space_insensitive():
    assert resolve_date("  Today ", today=TODAY) == "2026-07-07"


def test_yesterday():
    assert resolve_date("yesterday", today=TODAY) == "2026-07-06"


def test_yesterday_crosses_month_boundary():
    assert resolve_date("yesterday", today=date(2026, 8, 1)) == "2026-07-31"


def test_mmdd_in_the_past_this_year_keeps_current_year():
    assert resolve_date("07-05", today=TODAY) == "2026-07-05"


def test_mmdd_today_keeps_current_year():
    # Boundary: the candidate equals today, which is not strictly future.
    assert resolve_date("07-07", today=TODAY) == "2026-07-07"


def test_mmdd_in_the_future_rolls_back_to_previous_year():
    # Past-biased: July 10th asked on July 7th resolves to *last* year.
    assert resolve_date("07-10", today=TODAY) == "2025-07-10"


def test_single_digit_month_and_day():
    assert resolve_date("7-5", today=TODAY) == "2026-07-05"


def test_slash_separator_is_normalized():
    assert resolve_date("07/05", today=TODAY) == "2026-07-05"


def test_full_iso_is_honored_even_when_future():
    # An explicit year always wins — it is never rolled back.
    assert resolve_date("2027-01-15", today=TODAY) == "2027-01-15"


def test_full_iso_in_the_past_is_honored():
    assert resolve_date("2020-02-29", today=TODAY) == "2020-02-29"


def test_unparseable_string_is_returned_unchanged():
    assert resolve_date("next tuesday", today=TODAY) == "next tuesday"


def test_non_numeric_parts_are_returned_unchanged():
    assert resolve_date("not-a-date", today=TODAY) == "not-a-date"


def test_impossible_calendar_date_is_returned_unchanged():
    # date(2026, 2, 30) raises ValueError -> passthrough, never a crash.
    assert resolve_date("02-30", today=TODAY) == "02-30"


def test_impossible_month_is_returned_unchanged():
    assert resolve_date("13-01", today=TODAY) == "13-01"


# --- prefer= behavior (1.1) ---------------------------------------------------


def test_prefer_defaults_to_past():
    # Explicit "past" matches the default and the original behavior.
    assert resolve_date("07-10", today=TODAY, prefer="past") == "2025-07-10"


def test_prefer_nearest_keeps_this_year_for_near_future():
    # The calendar's case: July 10th asked on July 7th stays in the current
    # year rather than rolling back to last year (the old silent-miss bug).
    assert resolve_date("07-10", today=TODAY, prefer="nearest") == "2026-07-10"


def test_prefer_nearest_keeps_this_year_for_near_past():
    assert resolve_date("07-05", today=TODAY, prefer="nearest") == "2026-07-05"


def test_prefer_nearest_rolls_back_when_last_year_is_closer():
    # Late June asked in early July: last year's is 12 days back, this year's
    # was ~9 days ago too — pick the truly nearest. Here this-year June 28th is
    # 9 days back; last year is 374 back. This year wins.
    assert resolve_date("06-28", today=TODAY, prefer="nearest") == "2026-06-28"


def test_prefer_nearest_rolls_forward_when_next_year_is_closer():
    # January 1st asked on December 1st: next year's Jan 1 (31 days ahead) is
    # nearer than this year's Jan 1 (~334 days back).
    assert resolve_date("01-01", today=date(2026, 12, 1), prefer="nearest") == "2027-01-01"


def test_prefer_future_rolls_forward_for_past_day():
    # A day already gone this year resolves to next year under "future".
    assert resolve_date("07-05", today=TODAY, prefer="future") == "2027-07-05"


def test_prefer_future_keeps_this_year_for_upcoming_day():
    assert resolve_date("07-10", today=TODAY, prefer="future") == "2026-07-10"


def test_prefer_nearest_still_passes_through_impossible_date():
    assert resolve_date("02-30", today=TODAY, prefer="nearest") == "02-30"
