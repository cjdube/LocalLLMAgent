"""Tests for agent/tools/strava.py — the refresh-token exchange, the activity
formatting/mapping, and fetch_strava's target-date filtering and error
handling. The Strava HTTP calls are stubbed.

_get_activities is clock- and timezone-coupled (a 30-day cutoff from
datetime.now(), and start times converted via .astimezone()), so its test uses
a start time relative to now and asserts only timezone-independent fields;
date filtering is covered through fetch_strava with a stubbed internal."""

from datetime import datetime, timedelta, timezone

from agent.tools import strava


class _Resp:
    def __init__(self, payload, ok=True, status=200):
        self._payload = payload
        self.ok = ok
        self.status_code = status
        self.text = "error body"

    def json(self):
        return self._payload


def test_get_access_token_returns_token(monkeypatch):
    monkeypatch.setattr(strava.requests, "post",
                        lambda *a, **k: _Resp({"access_token": "tok"}))
    assert strava._get_access_token("id", "secret", "refresh") == "tok"


def test_get_access_token_raises_on_failure(monkeypatch):
    monkeypatch.setattr(strava.requests, "post",
                        lambda *a, **k: _Resp({}, ok=False, status=401))
    try:
        strava._get_access_token("id", "secret", "refresh")
        assert False, "should have raised"
    except RuntimeError as e:
        assert "401" in str(e)


def test_get_activities_maps_and_rounds(monkeypatch):
    monkeypatch.setattr(strava, "_get_access_token", lambda *a, **k: "tok")
    # One day ago so the 30-day cutoff never filters it, whenever the test runs.
    recent = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT12:00:00Z")
    monkeypatch.setattr(strava.requests, "get", lambda *a, **k: _Resp([{
        "id": 7, "name": "Ride", "type": "Ride", "start_date": recent,
        "elapsed_time": 3660, "distance": 12345.6, "total_elevation_gain": 88,
    }]))
    out = strava._get_activities("id", "secret", "refresh")
    assert len(out) == 1
    assert out[0]["distance_km"] == 12.35   # 12345.6 m -> km, 2dp
    assert out[0]["duration_minutes"] == 61  # 3660 s // 60
    assert out[0]["elevation_gain_m"] == 88
    assert out[0]["name"] == "Ride" and out[0]["type"] == "Ride"


def test_get_activities_raises_without_credentials(monkeypatch):
    # No client id/secret/refresh via arg or env -> a clear RuntimeError, which
    # fetch_strava turns into an error dict (see below).
    for var in ("STRAVA_CLIENT_ID", "STRAVA_CLIENT_SECRET", "STRAVA_REFRESH_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    try:
        strava._get_activities(None, None, None)
        assert False, "should have raised"
    except RuntimeError as e:
        assert "Strava credentials" in str(e)


def test_fetch_strava_filters_to_target_date(monkeypatch):
    monkeypatch.setattr(strava, "_get_activities", lambda *a, **k: [
        {"id": 1, "name": "Run A", "type": "Run", "date": "2026-07-08",
         "start_time": "06:30", "end_time": "07:30",
         "distance_km": 5.0, "duration_minutes": 60, "elevation_gain_m": 10},
        {"id": 2, "name": "Run B", "type": "Run", "date": "2026-07-09",
         "start_time": "06:30", "end_time": "07:30",
         "distance_km": 3.0, "duration_minutes": 30, "elevation_gain_m": 5},
    ])
    out = strava.fetch_strava("2026-07-08")
    assert out["date"] == "2026-07-08"
    assert out["activity_count"] == 1
    assert out["activities"][0]["strava_id"] == 1
    assert out["activities"][0]["name"] == "Run A"


def test_fetch_strava_returns_error_dict_on_failure(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("Missing Strava credentials")
    monkeypatch.setattr(strava, "_get_activities", boom)
    out = strava.fetch_strava("today")
    assert out["activity_count"] == 0 and out["activities"] == []
    assert "Missing Strava credentials" in out["error"]
