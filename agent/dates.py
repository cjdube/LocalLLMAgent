"""Shared date-resolution helpers.

The local model can't be trusted to know the current date, so any tool or
skill that accepts a user-supplied day resolves it here in Python rather than
relying on the model to fill in the year. Keeping the "current year, else the
previous year" rule in one place means every capability gets the same
behavior for free — import `resolve_date` instead of re-implementing it, and
drop `DATE_ARG_GUIDANCE` into the tool's JSON schema so the model is told how
to pass the argument.
"""

from datetime import date, datetime, timedelta
from typing import Optional

# Reusable JSON-schema description for a "which day" tool argument. Append it
# to a tool's own lead-in (e.g. "The day to look up. " + DATE_ARG_GUIDANCE) so
# the model passes something resolve_date() can handle.
DATE_ARG_GUIDANCE = (
    "Use 'today' or 'yesterday' for those. If a month and day are given "
    "WITHOUT a year (e.g. 'July 2nd'), pass just the month and day as 'MM-DD' "
    "(e.g. '07-02') and the correct year is filled in automatically. Only use "
    "a full 'YYYY-MM-DD' when a specific year is stated."
)


def _resolve_bare_month_day(month: int, day: int, today: date, prefer: str) -> date:
    """Pick the year for a bare MM-DD according to `prefer`.

    - "past"    -> the most recent past occurrence (this year, else last year).
                   Right for backward-looking lookups (Strava, Chrome history).
    - "future"  -> the next occurrence (this year, else next year). For
                   forward-looking asks where the year is unambiguously ahead.
    - "nearest" -> whichever occurrence is closest to `today` in either
                   direction. Right for calendars, where "July 10th" asked on
                   July 7th means *this* year's, not last year's.

    Feb-29 in a non-leap year raises ValueError for that candidate year; such
    years are simply skipped rather than crashing the whole resolution.
    """
    def candidate(year: int) -> Optional[date]:
        try:
            return date(year, month, day)
        except ValueError:
            return None

    if prefer == "future":
        this_year = candidate(today.year)
        if this_year is None or this_year < today:
            return candidate(today.year + 1) or date(today.year, month, day)
        return this_year

    if prefer == "nearest":
        options = [c for c in (candidate(today.year - 1), candidate(today.year), candidate(today.year + 1)) if c]
        return min(options, key=lambda c: abs((c - today).days))

    # "past" (default): this year, unless it hasn't happened yet.
    this_year = candidate(today.year)
    if this_year is None or this_year > today:
        return candidate(today.year - 1) or date(today.year, month, day)
    return this_year


def resolve_date(date_str: str, *, today: Optional[date] = None, prefer: str = "past") -> str:
    """Map a user-supplied date onto a concrete 'YYYY-MM-DD' string.

    - 'today' / 'yesterday'        -> relative to `today` (defaults to now)
    - 'YYYY-MM-DD'                 -> honored as-is (an explicit year wins)
    - 'MM-DD' / 'M-D' (also '/')   -> a bare month/day, with the year filled in
      per `prefer` ("past" | "future" | "nearest"; see
      _resolve_bare_month_day). Lets the user say "July 2nd" without ever
      specifying a year. Defaults to "past" so existing backward-looking
      callers (Strava, Chrome history) are unchanged; the calendar passes
      "nearest" so a near-future day resolves to this year, not last.

    Anything unparseable is returned unchanged, so callers never crash on odd
    model input — their downstream lookup simply won't match it.

    `today` is injectable so timezone-aware callers (e.g. the calendar tool)
    can pass their own local date and tests can pin the result.
    """
    today = today or datetime.now().date()

    normalized = date_str.strip().lower()
    if normalized == "today":
        return today.isoformat()
    if normalized == "yesterday":
        return (today - timedelta(days=1)).isoformat()

    parts = date_str.strip().replace("/", "-").split("-")
    try:
        if len(parts) == 3:
            return date(int(parts[0]), int(parts[1]), int(parts[2])).isoformat()
        if len(parts) == 2:
            month, day = int(parts[0]), int(parts[1])
            return _resolve_bare_month_day(month, day, today, prefer).isoformat()
    except ValueError:
        pass
    return date_str
