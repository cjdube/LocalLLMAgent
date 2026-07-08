"""Tests for agent.tools.chrome_history.fetch_chrome_history's date handling.

The fix under test: day boundaries are resolved in the *local* timezone (not
UTC), and days are resolved in Python (via agent.dates) rather than trusting the
model. TIMEZONE is pinned so the local zone is deterministic, and _query_history
is stubbed so no real Chrome DB is touched — the assertions are about the
datetimes the tool hands it.
"""

import agent.tools.chrome_history as ch


def _capture_query(monkeypatch):
    """Stub _query_history to record the (start, end) datetimes and return no
    rows, so fetch_chrome_history runs without a real Chrome History file."""
    captured = {}

    def fake_query(start, end):
        captured["start"] = start
        captured["end"] = end
        return []

    monkeypatch.setattr(ch, "_query_history", fake_query)
    return captured


def test_days_ago_builds_local_two_day_window(monkeypatch):
    monkeypatch.setenv("TIMEZONE", "America/New_York")
    captured = _capture_query(monkeypatch)

    result = ch.fetch_chrome_history(days_ago=1)

    # Boundaries are timezone-aware and in the pinned local zone, not UTC.
    assert str(captured["start"].tzinfo) == "America/New_York"
    assert str(captured["end"].tzinfo) == "America/New_York"
    # Full-day span: 00:00:00 -> 23:59:59, from yesterday to today.
    assert (captured["start"].hour, captured["start"].minute, captured["start"].second) == (0, 0, 0)
    assert (captured["end"].hour, captured["end"].minute, captured["end"].second) == (23, 59, 59)
    assert (captured["end"].date() - captured["start"].date()).days == 1
    assert result["total_meaningful_visits"] == 0
    assert "range" in result


def test_explicit_range_uses_local_offset_not_utc(monkeypatch):
    monkeypatch.setenv("TIMEZONE", "America/New_York")
    captured = _capture_query(monkeypatch)

    ch.fetch_chrome_history("2026-06-01", "2026-06-07")

    # June in New York is EDT (UTC-04:00) — the old code stamped these as UTC.
    assert captured["start"].isoformat() == "2026-06-01T00:00:00-04:00"
    assert captured["end"].isoformat() == "2026-06-07T23:59:59.999999-04:00"


def test_bare_month_day_is_resolved_in_python(monkeypatch):
    # A bare "MM-DD" must be accepted and given a year (past-biased), not left
    # for the model to guess — same contract as fetch_strava.
    monkeypatch.setenv("TIMEZONE", "America/New_York")
    captured = _capture_query(monkeypatch)

    result = ch.fetch_chrome_history("06-01", "06-07")

    assert captured["start"].month == 6 and captured["start"].day == 1
    # Year was filled in (4-digit) rather than passed through verbatim.
    assert result["range"].startswith("20")


def test_requires_days_ago_or_full_range(monkeypatch):
    _capture_query(monkeypatch)
    assert "error" in ch.fetch_chrome_history()
    assert "error" in ch.fetch_chrome_history(start="2026-06-01")  # end missing
