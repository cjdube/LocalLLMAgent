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

    assert result == {"event_id": "new-event", "html_link": "https://cal/new-event"}
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
