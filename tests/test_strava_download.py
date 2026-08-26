"""Tests for the deterministic Strava-to-calendar field-map in scribejay.strava_download.

fetch_strava and log_calendar_event are monkeypatched, so no network runs —
the tests exercise the mapping, the overnight-rollover guard, the skip/error
paths, and the source_id idempotency contract.
"""

from scribejay import strava_download


def _activity(**overrides) -> dict:
    base = {
        "strava_id": 12345,
        "name": "Morning Run",
        "type": "Run",
        "date": "2026-07-08",
        "start_time": "08:00",
        "end_time": "08:45",
        "distance_km": 5.0,
        "duration_minutes": 45,
        "elevation_gain_m": 30,
    }
    base.update(overrides)
    return base


def _patch(monkeypatch, *, activities=None, error=None):
    """Wire fetch_strava to return the given activities/error and capture every
    log_calendar_event call. Returns the list of captured kwargs."""
    fetch_result = {"date": "2026-07-08", "activity_count": 0, "activities": []}
    if error is not None:
        fetch_result = {"activity_count": 0, "activities": [], "error": error}
    elif activities is not None:
        fetch_result = {
            "date": "2026-07-08",
            "activity_count": len(activities),
            "activities": activities,
        }

    monkeypatch.setattr(strava_download, "fetch_strava", lambda date: fetch_result)

    calls = []

    def fake_log(**kwargs):
        calls.append(kwargs)
        return {"event_id": f"evt-{len(calls)}", "html_link": "https://cal/x"}

    monkeypatch.setattr(strava_download, "log_calendar_event", fake_log)
    return calls


def test_maps_activity_fields_to_event(monkeypatch):
    calls = _patch(monkeypatch, activities=[_activity()])
    assert strava_download.main() == 0
    assert len(calls) == 1
    call = calls[0]
    assert call["summary"] == "Morning Run"
    assert call["start"] == "2026-07-08T08:00:00"
    assert call["end"] == "2026-07-08T08:45:00"
    assert call["color_id"] == strava_download.FITNESS_COLOR_ID == "4"


def test_source_id_is_the_real_strava_id_as_string(monkeypatch):
    calls = _patch(monkeypatch, activities=[_activity(strava_id=98765)])
    assert strava_download.main() == 0
    assert calls[0]["source_id"] == "98765"


def test_zero_activities_writes_nothing(monkeypatch):
    calls = _patch(monkeypatch, activities=[])
    assert strava_download.main() == 0
    assert calls == []


def test_missing_times_skips_that_activity_only(monkeypatch):
    calls = _patch(
        monkeypatch,
        activities=[
            _activity(strava_id=1, start_time=None, end_time=None),
            _activity(strava_id=2, name="Evening Ride"),
        ],
    )
    assert strava_download.main() == 0
    assert len(calls) == 1
    assert calls[0]["summary"] == "Evening Ride"


def test_overnight_activity_rolls_end_date_forward(monkeypatch):
    calls = _patch(
        monkeypatch,
        activities=[_activity(start_time="23:30", end_time="00:15")],
    )
    assert strava_download.main() == 0
    assert calls[0]["start"] == "2026-07-08T23:30:00"
    assert calls[0]["end"] == "2026-07-09T00:15:00"


def test_fetch_error_returns_failure_and_writes_nothing(monkeypatch):
    calls = _patch(monkeypatch, error="Strava token refresh failed (401)")
    # Neutralize the failure push so the test never hits a real ntfy server
    # (config/.env may define NTFY_URL on the dev/prod machine).
    monkeypatch.setattr(strava_download, "notify_failure", lambda *a, **k: None)
    assert strava_download.main() == 1
    assert calls == []


# --------------------------------------------------------------------------- #
# partial-failure alerting
# --------------------------------------------------------------------------- #

def test_partial_failure_pushes_alert_but_exits_zero(monkeypatch):
    alerts = []
    monkeypatch.setattr(strava_download, "notify_failure",
                        lambda name, detail, logger=None: alerts.append(str(detail)))
    # One good activity, one with no start_time (skipped by _log_activity).
    _patch(monkeypatch, activities=[_activity(), _activity(strava_id=999, start_time=None)])

    assert strava_download.main() == 0  # the logged one is done; re-runs can't duplicate it
    assert any("1 of 2" in a for a in alerts)  # ...but the miss is pushed, not silent


def test_full_success_pushes_no_alert(monkeypatch):
    alerts = []
    monkeypatch.setattr(strava_download, "notify_failure",
                        lambda name, detail, logger=None: alerts.append(str(detail)))
    _patch(monkeypatch, activities=[_activity()])
    assert strava_download.main() == 0
    assert alerts == []
