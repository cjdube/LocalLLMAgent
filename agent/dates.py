"""Shared date-resolution helpers.

The local model can't be trusted to know the current date, so any tool or
skill that accepts a user-supplied day resolves it here in Python rather than
relying on the model to fill in the year. Keeping the "current year, else the
previous year" rule in one place means every capability gets the same
behavior for free — import `resolve_date` instead of re-implementing it, and
drop `DATE_ARG_GUIDANCE` into the tool's JSON schema so the model is told how
to pass the argument.
"""

import os
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

# Reusable JSON-schema description for a "which day" tool argument. Append it
# to a tool's own lead-in (e.g. "The day to look up. " + DATE_ARG_GUIDANCE) so
# the model passes something resolve_date() can handle.
DATE_ARG_GUIDANCE = (
    "Use 'today'/'yesterday' as-is. When no year is stated, pass just 'MM-DD' "
    "(e.g. '07-02') and the year is filled in automatically; use 'YYYY-MM-DD' "
    "only when a year is stated."
)


def local_timezone() -> str:
    """Resolve the system's IANA timezone name (e.g. 'America/New_York').

    Kept here (rather than in a single tool) because several tools and tasks
    need the same local zone to interpret day boundaries consistently — the
    calendar, the Chrome-history lookup, and the weekly/colorizer tasks all
    resolve day ranges in local time, not UTC. Google Calendar also rejects
    abbreviations like 'EDT', so we read the real zoneinfo path via
    /etc/localtime rather than relying on tzinfo.__str__. Overridable with the
    TIMEZONE env var; falls back to 'UTC' if the path can't be resolved."""
    override = os.getenv("TIMEZONE")
    if override:
        return override
    try:
        resolved = Path("/etc/localtime").resolve()
        parts = resolved.parts
        idx = parts.index("zoneinfo")
        return "/".join(parts[idx + 1:])
    except (OSError, ValueError):
        return "UTC"


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


# Guidance dropped into the set_reminder tool schema so the model passes a phrase
# resolve_reminder_time() can handle — and, crucially, doesn't try to compute the
# time itself (it can't do date math reliably).
REMINDER_WHEN_GUIDANCE = (
    "Pass the user's time expression verbatim — do NOT compute or convert it "
    "yourself. Understood: relative delays ('in 2 hours', '90m'), clock times "
    "taken as the next occurrence ('3pm', '15:00'), 'tomorrow 9am', or "
    "'YYYY-MM-DD HH:MM'."
)

# Relative-delay units accepted by resolve_reminder_time, in seconds.
_DELAY_UNIT_SECONDS = {
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
}


def _parse_clock(text: str) -> Optional[tuple[int, int]]:
    """Parse a bare clock time ('3pm', '3:30 pm', '15:00') to (hour, minute),
    or None if it isn't one."""
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", text.strip())
    if not m:
        return None
    hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ampm:
        if not 1 <= hour <= 12:
            return None
        if ampm == "pm" and hour != 12:
            hour += 12
        if ampm == "am" and hour == 12:
            hour = 0
    if hour > 23 or minute > 59:
        return None
    return hour, minute


def resolve_reminder_time(when: str, *, now: Optional[datetime] = None) -> Optional[datetime]:
    """Resolve a reminder time expression to a concrete timezone-aware datetime
    in the local zone, or None if it can't be parsed. Parsing lives here (not in
    the model) so 'in 2 hours' etc. resolve deterministically. `now` is
    injectable so tests can pin the result."""
    tz = ZoneInfo(local_timezone())
    now = now or datetime.now(tz)
    s = (when or "").strip().lower()
    s = re.sub(r"^at\s+", "", s)  # "at 3pm" -> "3pm"
    if not s:
        return None

    # Relative delay: "in 2 hours", "2h", "90m", "in 3 days".
    m = re.fullmatch(r"(?:in\s+)?(\d+)\s*([a-z]+)", s)
    if m and m.group(2) in _DELAY_UNIT_SECONDS:
        return now + timedelta(seconds=int(m.group(1)) * _DELAY_UNIT_SECONDS[m.group(2)])

    # "tomorrow [at] <clock>".
    m = re.fullmatch(r"tomorrow(?:\s+at)?\s+(.+)", s)
    if m:
        clock = _parse_clock(m.group(1))
        if clock:
            return (now + timedelta(days=1)).replace(
                hour=clock[0], minute=clock[1], second=0, microsecond=0)

    # Bare clock time: the next time it occurs (today if still ahead, else tomorrow).
    clock = _parse_clock(s)
    if clock:
        cand = now.replace(hour=clock[0], minute=clock[1], second=0, microsecond=0)
        return cand if cand > now else cand + timedelta(days=1)

    # Explicit "YYYY-MM-DD HH:MM" (or ISO 'T' separator).
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})[ t](\d{1,2}):(\d{2})", s)
    if m:
        try:
            return datetime(int(m[1]), int(m[2]), int(m[3]), int(m[4]), int(m[5]), tzinfo=tz)
        except ValueError:
            return None

    return None
