"""Tests for chat/routes_usage.py — the Flask edge of the /activity page.

The aggregation is covered by tests/test_usage.py and the drawing by
tests/usage-chart.test.js. What's tested here is only what the route adds: the
auth gate, and that `days` is clamped rather than validated (it's view state — a
nonsense value should narrow the window, not 400 the page that asked for it).

summarize is stubbed to a recorder, so these assert on what the route passed
down, not on ledger parsing.
"""

import os

os.environ.setdefault("WREN_CHAT_TOKEN", "test-token")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")

import pytest

from chat import routes_usage as ru
from chat import server as srv


@pytest.fixture
def auth_client():
    srv.app.config["TESTING"] = True
    with srv.app.test_client() as c:
        with c.session_transaction() as sess:
            sess["authenticated"] = True
            sess["sid"] = "test-sid"
        yield c


@pytest.fixture
def recorded(monkeypatch):
    """Capture the days summarize() was asked for; return an empty-but-valid payload."""
    seen = {}

    def _summarize(days):
        seen["days"] = days
        return {"days": days, "totals": {}, "by_day": [], "by_model": [],
                "by_agent": [], "by_task": [], "by_backend": []}
    monkeypatch.setattr(ru, "summarize", _summarize)
    return seen


def test_unauthenticated_is_401_not_a_payload(recorded):
    srv.app.config["TESTING"] = True
    with srv.app.test_client() as c:
        resp = c.get("/api/usage")
    assert resp.status_code == 401
    assert resp.get_json()["error"] == "not authenticated"
    # And the ledger was never read.
    assert "days" not in recorded


def test_the_default_window_is_a_week(auth_client, recorded):
    assert auth_client.get("/api/usage").status_code == 200
    assert recorded["days"] == 7


def test_the_page_buttons_pass_through(auth_client, recorded):
    for days in (7, 30, 90):
        auth_client.get(f"/api/usage?days={days}")
        assert recorded["days"] == days


def test_a_too_large_window_is_clamped_not_rejected(auth_client, recorded):
    resp = auth_client.get("/api/usage?days=9999")
    assert resp.status_code == 200
    assert recorded["days"] == ru.MAX_DAYS


def test_zero_and_negative_are_clamped_up(auth_client, recorded):
    auth_client.get("/api/usage?days=0")
    assert recorded["days"] == ru.DEFAULT_DAYS   # 0 is falsy -> the default
    auth_client.get("/api/usage?days=-5")
    assert recorded["days"] == ru.MIN_DAYS


def test_a_non_numeric_window_falls_back_to_the_default(auth_client, recorded):
    resp = auth_client.get("/api/usage?days=lots")
    assert resp.status_code == 200
    assert recorded["days"] == ru.DEFAULT_DAYS


def test_the_payload_is_the_summary_verbatim(auth_client, recorded):
    body = auth_client.get("/api/usage?days=30").get_json()
    assert body["days"] == 30
    assert set(body) >= {"totals", "by_day", "by_model", "by_agent", "by_task"}
