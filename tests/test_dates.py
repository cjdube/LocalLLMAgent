"""Tests for agent.dates.resolve_date — the shared, past-biased date resolver.

`today` is pinned throughout so the MM-DD past/future boundary is deterministic.
"""

from datetime import date, datetime

from zoneinfo import ZoneInfo

from agent.dates import (
    DATE_ARG_GUIDANCE,
    REMINDER_WHEN_GUIDANCE,
    local_timezone,
    resolve_date,
    resolve_reminder_time,
)

TODAY = date(2026, 7, 7)  # a Tuesday

# A fixed "now" for reminder-time tests: Tue Jul 7 2026, 2:00pm local.
_TZ = ZoneInfo(local_timezone())
NOW = datetime(2026, 7, 7, 14, 0, tzinfo=_TZ)


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


# --- relative days ------------------------------------------------------------
#
# Weekday arithmetic lives here rather than in the model: asked on Friday
# 2026-08-14 for "next Tuesday", the model answered with the 19th (a Wednesday),
# looked up an empty day, and reported that wrong date as fact.

def test_the_reported_bug_next_tuesday_from_a_friday():
    # The exact failing case, with the calendar tool's own prefer=.
    assert resolve_date("next tuesday", today=date(2026, 8, 14), prefer="nearest") == "2026-08-18"


def test_tomorrow():
    assert resolve_date("tomorrow", today=TODAY) == "2026-07-08"


def test_next_weekday_is_the_next_one_after_today():
    # TODAY is a Tuesday: "next tuesday" is a week out, never today.
    assert resolve_date("next tuesday", today=TODAY) == "2026-07-14"
    assert resolve_date("next friday", today=TODAY) == "2026-07-10"


def test_next_weekday_asked_the_day_before_means_tomorrow():
    # Monday 2026-08-17 -> the very next Tuesday, not the following week's.
    assert resolve_date("next tuesday", today=date(2026, 8, 17)) == "2026-08-18"


def test_last_weekday_looks_back():
    assert resolve_date("last tuesday", today=TODAY) == "2026-06-30"
    assert resolve_date("last friday", today=TODAY) == "2026-07-03"


def test_bare_weekday_follows_prefer():
    # A bare weekday has no direction of its own, so the caller's bias decides:
    # Chrome history / Strava look back, the calendar and tasks look forward.
    assert resolve_date("tuesday", today=TODAY, prefer="past") == "2026-06-30"
    assert resolve_date("tuesday", today=TODAY, prefer="nearest") == "2026-07-14"
    assert resolve_date("tuesday", today=TODAY, prefer="future") == "2026-07-14"


def test_explicit_qualifier_beats_prefer():
    assert resolve_date("next tuesday", today=TODAY, prefer="past") == "2026-07-14"
    assert resolve_date("last tuesday", today=TODAY, prefer="future") == "2026-06-30"


def test_weekday_phrasing_variants():
    # "the following sunday" / "the next sunday" are phrasings the model
    # actually produced when asked to build a week-long range.
    for phrase in ("Next Tuesday", "  next   tuesday ", "on tuesday", "this tue",
                   "coming tues", "the next tuesday", "the following tuesday"):
        assert resolve_date(phrase, today=TODAY, prefer="nearest") == "2026-07-14"


def test_unrecognised_day_phrase_is_returned_unchanged():
    # Falls through to the numeric parsing and then to passthrough — the caller
    # degrades rather than resolving to a plausible wrong day.
    assert resolve_date("monday morning", today=TODAY) == "monday morning"
    assert resolve_date("sometime next week", today=TODAY) == "sometime next week"


# --- DATE_ARG_GUIDANCE wording ------------------------------------------------
#
# This string is the entire fix as the model sees it. Pinned like the list_games
# description in tests/test_games.py: softening it back into "work out the date"
# reintroduces the bug with every test still green.

def test_guidance_tells_the_model_to_pass_the_phrase_verbatim():
    assert "verbatim" in DATE_ARG_GUIDANCE
    assert "do NOT work out the date yourself" in DATE_ARG_GUIDANCE


def test_guidance_names_the_relative_forms_it_accepts():
    for form in ("today", "tomorrow", "yesterday", "next tuesday", "last friday"):
        assert form in DATE_ARG_GUIDANCE


def test_reminder_guidance_names_weekday_phrases():
    assert "verbatim" in REMINDER_WHEN_GUIDANCE
    assert "next friday" in REMINDER_WHEN_GUIDANCE


# --- resolve_reminder_time -------------------------------------------------

def _r(when):
    return resolve_reminder_time(when, now=NOW)


def test_reminder_relative_hours():
    assert _r("in 2 hours") == datetime(2026, 7, 7, 16, 0, tzinfo=_TZ)


def test_reminder_relative_compact_units():
    assert _r("90m") == datetime(2026, 7, 7, 15, 30, tzinfo=_TZ)
    assert _r("2h") == datetime(2026, 7, 7, 16, 0, tzinfo=_TZ)
    assert _r("in 3 days") == datetime(2026, 7, 10, 14, 0, tzinfo=_TZ)


def test_reminder_clock_later_today():
    assert _r("3pm") == datetime(2026, 7, 7, 15, 0, tzinfo=_TZ)
    assert _r("at 3:30 pm") == datetime(2026, 7, 7, 15, 30, tzinfo=_TZ)


def test_reminder_clock_already_passed_rolls_to_tomorrow():
    # 9am is behind 2pm now, so it means tomorrow's 9am.
    assert _r("9am") == datetime(2026, 7, 8, 9, 0, tzinfo=_TZ)


def test_reminder_tomorrow_with_time():
    assert _r("tomorrow 9am") == datetime(2026, 7, 8, 9, 0, tzinfo=_TZ)
    assert _r("tomorrow at 15:00") == datetime(2026, 7, 8, 15, 0, tzinfo=_TZ)


def test_reminder_weekday_with_time():
    # NOW is Tuesday 2026-07-07; a reminder is always forward-looking.
    assert _r("tuesday 3pm") == datetime(2026, 7, 14, 15, 0, tzinfo=_TZ)
    assert _r("next friday at 9am") == datetime(2026, 7, 10, 9, 0, tzinfo=_TZ)


def test_reminder_day_without_a_time_defaults_to_9am():
    assert _r("monday") == datetime(2026, 7, 13, 9, 0, tzinfo=_TZ)
    assert _r("tomorrow") == datetime(2026, 7, 8, 9, 0, tzinfo=_TZ)


def test_reminder_explicit_datetime():
    assert _r("2026-07-11 08:30") == datetime(2026, 7, 11, 8, 30, tzinfo=_TZ)


def test_reminder_unparseable_returns_none():
    assert _r("sometime soon") is None
    assert _r("") is None
    assert _r("25:00") is None
