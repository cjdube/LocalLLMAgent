"""Tests for agent/tools/calendar.py's source_id dedupe — the mechanism behind
the README's "re-runs never create duplicates" guarantee for strava_download. The
Google service is a stub; per the project's live-API precedent everything else
in this module stays untested."""

import pytest

from agent.tools import calendar as cal


class _Exec:
    def __init__(self, payload):
        self._payload = payload

    def execute(self):
        return self._payload


class _FakeEvents:
    """Records list/insert calls; returns canned payloads."""

    def __init__(self, existing_items):
        self.existing_items = existing_items
        self.list_kwargs = []
        self.inserted = []

    def list(self, **kwargs):
        self.list_kwargs.append(kwargs)
        return _Exec({"items": self.existing_items})

    def insert(self, calendarId, body):
        self.inserted.append(body)
        return _Exec({"id": "new-event", "htmlLink": "https://cal/new-event"})


class _FakeService:
    def __init__(self, events):
        self._events = events

    def events(self):
        return self._events


@pytest.fixture
def fake_events(monkeypatch):
    holder = {"events": _FakeEvents(existing_items=[])}
    monkeypatch.setattr(cal, "build_service",
                        lambda api, version: _FakeService(holder["events"]))
    return holder


def test_log_event_with_source_id_skips_when_already_logged(fake_events):
    fake_events["events"] = events = _FakeEvents(existing_items=[
        {"id": "existing-1", "htmlLink": "https://cal/existing-1"},
    ])

    result = cal.log_calendar_event("Run", "2026-07-10T08:00:00", "2026-07-10T09:00:00",
                                    source_id="strava-42")

    assert result["skipped"] == "event already logged for this source_id"
    assert result["event_id"] == "existing-1"
    assert events.inserted == []  # the duplicate was never created
    assert events.list_kwargs[0]["privateExtendedProperty"] == "source_id=strava-42"


def test_log_event_with_source_id_stamps_the_extended_property(fake_events):
    events = fake_events["events"]

    result = cal.log_calendar_event("Run", "2026-07-10T08:00:00", "2026-07-10T09:00:00",
                                    source_id="strava-42", color_id="4")

    assert result["event_id"] == "new-event"
    assert result["html_link"] == "https://cal/new-event"
    body = events.inserted[0]
    # The property queried by the dedupe lookup must be the one stamped here —
    # this pairing IS the idempotency guarantee.
    assert body["extendedProperties"]["private"]["source_id"] == "strava-42"
    assert body["colorId"] == "4"


def test_log_event_without_source_id_never_queries(fake_events):
    events = fake_events["events"]
    result = cal.log_calendar_event("Lunch", "2026-07-10T12:00:00", "2026-07-10T13:00:00")
    assert result["event_id"] == "new-event"
    assert events.list_kwargs == []  # no pointless dedupe round-trip
    assert "extendedProperties" not in events.inserted[0]


def test_events_in_range_surfaces_source_id(fake_events):
    # The other half of the dedupe pairing: a reader has to be able to tell an
    # event Wren stamped from one the user made by hand. calendar_colorizer
    # depends on this to leave the session blocks it must not recolor alone.
    fake_events["events"] = _FakeEvents(existing_items=[
        {"id": "e1", "summary": "AI · Wren — added the digest",
         "start": {"dateTime": "2026-07-10T08:00:00-04:00"},
         "end": {"dateTime": "2026-07-10T09:00:00-04:00"},
         "extendedProperties": {"private": {"source_id": "claude-time:2026-07-10:0800"}}},
        {"id": "e2", "summary": "Dentist",
         "start": {"dateTime": "2026-07-10T10:00:00-04:00"},
         "end": {"dateTime": "2026-07-10T11:00:00-04:00"}},
    ])

    result = cal.get_events_in_range("2026-07-10T00:00:00", "2026-07-10T23:59:59")

    assert [e["source_id"] for e in result["events"]] == [
        "claude-time:2026-07-10:0800", None,
    ]


# --- get_events_by_date: the day it looked at comes back with the events -------
#
# The model narrated its own guess of the day ("Tuesday, August 19th" for a
# Wednesday) alongside a correct-looking empty result. Returning the resolved
# date makes a mis-aimed lookup visible in the reply instead of self-consistent.

@pytest.fixture
def pinned_today(monkeypatch):
    """Freeze 'now' at Friday 2026-08-14 in a fixed zone, so weekday resolution
    is deterministic rather than inheriting the host's clock and timezone."""
    monkeypatch.setenv("TIMEZONE", "America/New_York")

    class _FrozenDatetime(cal.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 8, 14, 12, 0, tzinfo=tz)

    monkeypatch.setattr(cal, "datetime", _FrozenDatetime)


def test_weekday_phrase_resolves_and_is_echoed_back(fake_events, pinned_today):
    result = cal.get_events_by_date("next tuesday", "next tuesday")

    # The reported bug: the model said August 19th, a Wednesday.
    assert result["resolved_start"] == "2026-08-18"
    assert result["resolved_end"] == "2026-08-18"
    assert result["range"] == "Tuesday, August 18, 2026"


def test_range_spanning_days_names_both_ends(fake_events, pinned_today):
    result = cal.get_events_by_date("next monday", "next friday")
    assert result["range"] == "Monday, August 17, 2026 through Friday, August 21, 2026"


def test_events_are_still_returned_alongside_the_resolved_date(fake_events, pinned_today):
    fake_events["events"] = _FakeEvents(existing_items=[
        {"id": "e1", "summary": "AI Tinkerers Manchester",
         "start": {"dateTime": "2026-08-18T17:30:00-04:00"},
         "end": {"dateTime": "2026-08-18T19:45:00-04:00"}},
    ])

    result = cal.get_events_by_date("next tuesday", "next tuesday")

    assert result["event_count"] == 1
    assert result["events"][0]["summary"] == "AI Tinkerers Manchester"


def test_backwards_range_is_an_error_not_an_empty_day(fake_events, pinned_today):
    # Seen live building "the week of next Monday": start next Monday (the 17th),
    # end the nearest Sunday (the 16th). An empty result there reads as a free week.
    result = cal.get_events_by_date("next monday", "sunday")

    assert "runs backwards" in result["error"]
    assert "events" not in result


def test_unresolvable_date_returns_an_error_not_a_crash(fake_events, pinned_today):
    # resolve_date() passes an unrecognised phrase through untouched; that must
    # degrade to an error the model can relay, not a ValueError that 500s the turn.
    result = cal.get_events_by_date("sometime next week", "sometime next week")

    assert "sometime next week" in result["error"]
    assert "events" not in result


# --- get_events_by_date is bounded ------------------------------------------
# Uncapped, a 7-week ask returned 181 events and 39KB against the loop's
# 8000-char cap: the model saw a fifth of the calendar and described it as the
# calendar. get_events_in_range stays whole — the colorizer needs every event.

def _busy(n, summary_len=40):
    """n events in the Google API's own shape — nested start/end, which is what
    _FakeEvents hands back and get_events_in_range unpacks."""
    return [{"id": f"evt-{i:04d}", "summary": "M" * summary_len,
             "start": {"dateTime": f"2026-08-{(i % 28) + 1:02d}T09:00:00-04:00"},
             "end": {"dateTime": f"2026-08-{(i % 28) + 1:02d}T10:00:00-04:00"},
             "colorId": "4", "status": "confirmed"}
            for i in range(n)]


def test_a_long_range_is_capped_and_says_so(fake_events, pinned_today):
    fake_events["events"] = _FakeEvents(existing_items=_busy(200))

    result = cal.get_events_by_date("2026-08-01", "2026-08-28")

    assert result["event_count"] == 200            # the true total survives
    assert result["events_shown"] < 200
    assert len(result["events"]) == result["events_shown"]
    # The far end of the range is what's missing, and that reads like free time.
    assert "do not describe it as free" in result["partial"].lower()


def test_the_capped_result_fits_the_tool_result_cap(fake_events, pinned_today):
    import json

    from agent.loop import MAX_TOOL_RESULT_CHARS
    # Long titles are what broke a count-only cap: the same 50 events ran 7827
    # chars on one range and 8849 on another.
    fake_events["events"] = _FakeEvents(existing_items=_busy(200, summary_len=90))

    result = cal.get_events_by_date("2026-08-01", "2026-08-28")
    assert len(json.dumps(result)) < MAX_TOOL_RESULT_CHARS


def test_chat_events_drop_the_fields_only_the_tasks_use(fake_events, pinned_today):
    fake_events["events"] = _FakeEvents(existing_items=_busy(3))

    event = cal.get_events_by_date("2026-08-01", "2026-08-28")["events"][0]

    assert set(event) == {"id", "summary", "start", "end"}
    # id stays: recolor_event takes one.
    assert event["id"] == "evt-0000"


def test_a_short_range_comes_back_whole_with_no_partial_note(fake_events, pinned_today):
    fake_events["events"] = _FakeEvents(existing_items=_busy(3))

    result = cal.get_events_by_date("2026-08-01", "2026-08-28")

    assert result["event_count"] == 3 and result["events_shown"] == 3
    assert "partial" not in result


def test_get_events_in_range_is_left_uncapped_for_the_tasks(fake_events):
    # calendar_colorizer and daily_chrome_learnings need every event and every
    # field; only the chat wrapper has a context window to protect.
    fake_events["events"] = _FakeEvents(existing_items=_busy(200))

    result = cal.get_events_in_range("2026-08-01T00:00:00", "2026-08-28T23:59:59")

    assert result["event_count"] == 200 and len(result["events"]) == 200
    assert "colorId" in result["events"][0] and "source_id" in result["events"][0]


# --- the human "when" echoed back to the model --------------------------------
# A write result of two opaque ids gave the model no evidence its event existed,
# so it re-issued the write and drew a second confirmation card. Same convention
# as _human_due in google_tasks.py: the tool states the time it used.

def test_log_event_echoes_back_what_it_wrote(fake_events, monkeypatch):
    monkeypatch.setenv("TIMEZONE", "America/New_York")

    result = cal.log_calendar_event("do yardwork", "2026-08-19T10:00:00",
                                    "2026-08-19T11:00:00")

    assert result["created"] is True
    assert result["summary"] == "do yardwork"
    assert result["when"] == "Wednesday, August 19, 2026, 10:00 AM to 11:00 AM"


def test_when_spells_out_both_days_for_an_overnight_event(fake_events, monkeypatch):
    monkeypatch.setenv("TIMEZONE", "America/New_York")

    result = cal.log_calendar_event("red-eye", "2026-08-19T22:00:00",
                                    "2026-08-20T06:00:00")

    assert result["when"] == ("Wednesday, August 19, 2026, 10:00 PM to "
                              "Thursday, August 20, 2026, 6:00 AM")


def test_an_unparseable_time_degrades_instead_of_failing_the_write(fake_events):
    # "when" is a display string; a write that Google accepted must not fail on it.
    result = cal.log_calendar_event("odd", "whenever", "later")

    assert result["created"] is True
    assert result["when"] == "whenever to later"
