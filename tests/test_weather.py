"""Tests for agent/tools/weather.py's pure helpers and error mapping. The
OpenWeatherMap fetch itself is stubbed; per the live-API precedent only the
network-free logic is exercised."""

import requests

from agent.tools import weather


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def test_clamp_days_bounds():
    assert weather._clamp_days(0) == 1
    assert weather._clamp_days(1) == 1
    assert weather._clamp_days(3) == 3
    assert weather._clamp_days(99) == weather.MAX_DAYS


def test_normalize_location_appends_us_to_city_state():
    # The heuristic treats any 2-letter second token as a US state (per its
    # docstring) — "City, CC" country forms are inherently ambiguous to it.
    assert weather._normalize_location("Portland, OR") == "Portland,OR,US"
    assert weather._normalize_location("Portland,OR,US") == "Portland,OR,US"
    assert weather._normalize_location("Montreal, QC, CA") == "Montreal,QC,CA"


def _entry(dt, temp, desc="clear sky", pop=0.0):
    return {"dt": dt, "main": {"temp": temp, "feels_like": temp, "humidity": 50},
            "weather": [{"description": desc}], "pop": pop, "wind": {"speed": 5}}


def test_parse_summarizes_current_and_next_24h():
    raw = {"list": [_entry(1_700_000_000 + i * 10800, 60 + i, pop=0.5 if i == 2 else 0)
                    for i in range(8)],
           "city": {"name": "Portland", "country": "US", "timezone": -18000}}
    out = weather.parse(raw, days=1)
    assert out["location"] == "Portland, US"
    assert out["current"]["temp_f"] == 60
    assert out["next_24h"]["high_f"] == 67 and out["next_24h"]["low_f"] == 60
    assert "daily_forecast" not in out


def test_parse_multi_day_buckets_by_location_timezone():
    raw = {"list": [_entry(1_700_000_000 + i * 10800, 60) for i in range(16)],
           "city": {"name": "Portland", "country": "US", "timezone": -18000}}
    out = weather.parse(raw, days=2)
    assert len(out["daily_forecast"]) >= 2
    assert all({"date", "high_f", "low_f", "summary"} <= set(d) for d in out["daily_forecast"])


def test_parse_empty_list_raises():
    try:
        weather.parse({"list": []})
        assert False, "should have raised"
    except ValueError:
        pass


# --------------------------------------------------------------------------- #
# fetch_weather error mapping — through the shared _http.http_error
# --------------------------------------------------------------------------- #

def _raise(exc):
    def inner(*a, **k):
        raise exc
    return inner


def test_http_error_maps_status(monkeypatch):
    monkeypatch.setenv("OPENWEATHERMAP_API_KEY", "k")
    resp = requests.Response()
    resp.status_code = 401
    monkeypatch.setattr(weather, "fetch_forecast",
                        _raise(requests.exceptions.HTTPError(response=resp)))
    out = weather.fetch_weather("Portland,OR,US")
    assert out["error"].startswith("HTTP 401")


def test_network_error_maps_uniformly(monkeypatch):
    monkeypatch.setenv("OPENWEATHERMAP_API_KEY", "k")
    monkeypatch.setattr(weather, "fetch_forecast",
                        _raise(requests.exceptions.ConnectionError("refused")))
    out = weather.fetch_weather("Portland,OR,US")
    assert out["error"].startswith("network error")


def test_missing_key_short_circuits(monkeypatch):
    monkeypatch.delenv("OPENWEATHERMAP_API_KEY", raising=False)
    out = weather.fetch_weather("Portland,OR,US", api_key=None)
    assert "OPENWEATHERMAP_API_KEY" in out["error"]
