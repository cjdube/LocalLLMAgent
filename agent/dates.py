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
#
# Phrased like REMINDER_WHEN_GUIDANCE below: pass the phrase verbatim, don't
# compute. The model gets weekday arithmetic wrong (it answered "next Tuesday"
# with the Wednesday), so the only safe instruction is one that never asks it to
# do the math — see docs/model-constraints.md.
DATE_ARG_GUIDANCE = (
    "Pass the user's day expression verbatim — do NOT work out the date "
    "yourself. Understood: 'today', 'tomorrow', 'yesterday'; weekday phrases "
    "('next tuesday', 'last friday', 'monday'); a bare 'MM-DD' (e.g. '07-02') "
    "when a month and day are named without a year; 'YYYY-MM-DD' only when a "
    "year is stated."
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


# Weekday name -> Python weekday() index (Mon=0). Common short forms included
# because the model shortens them even when the user didn't.
_WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

# Words that pin a weekday phrase to one direction, overriding `prefer`.
_BACKWARD_QUALIFIERS = {"last", "past", "previous"}
_FORWARD_QUALIFIERS = {"next", "this", "coming", "upcoming", "following"}

_RELATIVE_DAY_OFFSETS = {"today": 0, "tomorrow": 1, "yesterday": -1}


def _resolve_relative_day(text: str, today: date, prefer: str) -> Optional[date]:
    """Resolve a relative day phrase ('tomorrow', 'next tuesday') to a date, or
    None if it isn't one — in which case resolve_date() falls through to its
    numeric parsing, so no existing input changes behavior.

    This lives in Python because the small local model can't do weekday
    arithmetic: asked on a Friday for "next Tuesday" it answered with the
    following Wednesday, looked up an empty day, and reported the wrong date as
    fact. See docs/model-constraints.md.

    'next tuesday' means the next Tuesday *after* today, not the Tuesday of the
    following calendar week — so asked on Monday it means tomorrow. A bare
    weekday has no direction of its own and follows the caller's `prefer`:
    "past" looks back (Chrome history, Strava), "future"/"nearest" look forward
    (calendar, tasks). An explicit qualifier always wins over `prefer`.
    """
    # Strip the filler the model puts in front of a day when it builds a range:
    # it asked for "the following Sunday" and "the next Sunday" before settling.
    normalized = re.sub(r"\s+", " ", text.strip().lower())
    normalized = re.sub(r"^(?:on\s+)?(?:the\s+)?", "", normalized)

    offset = _RELATIVE_DAY_OFFSETS.get(normalized)
    if offset is not None:
        return today + timedelta(days=offset)

    parts = normalized.split(" ")
    if len(parts) == 2 and parts[0] in _BACKWARD_QUALIFIERS | _FORWARD_QUALIFIERS:
        qualifier, name = parts
    elif len(parts) == 1:
        qualifier, name = "", parts[0]
    else:
        return None

    target = _WEEKDAYS.get(name)
    if target is None:
        return None

    if qualifier in _BACKWARD_QUALIFIERS:
        backward = True
    elif qualifier in _FORWARD_QUALIFIERS:
        backward = False
    else:
        backward = prefer == "past"

    # Strictly before / after today: "tuesday" asked on a Tuesday means the
    # neighbouring one, never today — the user would have said "today".
    delta = (today.weekday() - target) % 7 if backward else (target - today.weekday()) % 7
    delta = delta or 7
    return today - timedelta(days=delta) if backward else today + timedelta(days=delta)


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

    - 'today' / 'tomorrow' / 'yesterday' -> relative to `today` (defaults to now)
    - 'next tuesday' / 'last friday' / a bare weekday -> see
      _resolve_relative_day; a bare weekday follows `prefer`
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

    relative = _resolve_relative_day(date_str, today, prefer)
    if relative is not None:
        return relative.isoformat()

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
    "taken as the next occurrence ('3pm', '15:00'), 'tomorrow 9am', weekday "
    "phrases ('tuesday 3pm', 'next friday at 9am', 'monday' — 9am if no time is "
    "given), or 'YYYY-MM-DD HH:MM'."
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
    s = re.sub(r"\s+at\s+", " ", s)  # "next friday at 9am" -> "next friday 9am"
    if not s:
        return None

    # Relative delay: "in 2 hours", "2h", "90m", "in 3 days".
    m = re.fullmatch(r"(?:in\s+)?(\d+)\s*([a-z]+)", s)
    if m and m.group(2) in _DELAY_UNIT_SECONDS:
        return now + timedelta(seconds=int(m.group(1)) * _DELAY_UNIT_SECONDS[m.group(2)])

    # "<day phrase> [clock]": 'tomorrow 9am', 'tuesday 3pm', 'next friday'.
    # The day resolves in _resolve_relative_day (never in the model, which gets
    # weekday arithmetic wrong); prefer="future" because a reminder is always
    # forward-looking. A day with no clock defaults to 9am rather than failing
    # the whole expression. Two words are tried before one so the qualifier in
    # "next friday" isn't mistaken for a time.
    words = s.split(" ")
    for take in (2, 1):
        if len(words) < take:
            continue
        day = _resolve_relative_day(" ".join(words[:take]), now.date(), prefer="future")
        if day is None:
            continue
        rest = " ".join(words[take:])
        clock = _parse_clock(rest) if rest else (9, 0)
        if clock:
            return datetime(day.year, day.month, day.day, clock[0], clock[1], tzinfo=tz)

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
