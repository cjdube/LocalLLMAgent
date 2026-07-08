"""Tests for the pure helpers in agent.tools.google_tasks.

_due_soon_cutoff and the no-override branch of _resolve_tasklist_id are
tested here — everything else talks directly to the live Google Tasks API
and isn't unit-tested, matching the project's existing precedent of not
testing calendar.py's live-API functions either.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

from agent.tools.google_tasks import _due_soon_cutoff, _resolve_tasklist_id

EASTERN = ZoneInfo("America/New_York")


def test_due_soon_cutoff_is_end_of_local_day_two_days_out():
    now = datetime(2026, 7, 7, 9, 0, 0, tzinfo=EASTERN)  # Tuesday 9am ET
    cutoff = _due_soon_cutoff(48, now)
    # 9am Tue + 48h = 9am Thu -> end of Thursday local day.
    assert cutoff == "2026-07-10T03:59:59.999999Z"


def test_due_soon_cutoff_returns_utc_z_suffix():
    now = datetime(2026, 7, 7, 9, 0, 0, tzinfo=EASTERN)
    cutoff = _due_soon_cutoff(48, now)
    assert cutoff.endswith("Z")
    assert "+00:00" not in cutoff


def test_due_soon_cutoff_default_48h_matches_explicit_call():
    now = datetime(2026, 7, 7, 9, 0, 0, tzinfo=EASTERN)
    assert _due_soon_cutoff(48, now) == _due_soon_cutoff(48, now)


def test_due_soon_cutoff_zero_hours_is_end_of_today():
    now = datetime(2026, 7, 7, 9, 0, 0, tzinfo=EASTERN)
    cutoff = _due_soon_cutoff(0, now)
    # End of Tuesday 2026-07-07 in EDT (UTC-4) is 2026-07-08 03:59:59.999999 UTC.
    assert cutoff == "2026-07-08T03:59:59.999999Z"


# --------------------------------------------------------------------------- #
# _resolve_tasklist_id (no-list_name branch only — doesn't touch the network)
# --------------------------------------------------------------------------- #

def test_resolve_tasklist_id_defaults_to_at_default(monkeypatch):
    monkeypatch.delenv("GOOGLE_TASKLIST_ID", raising=False)
    assert _resolve_tasklist_id() == "@default"


def test_resolve_tasklist_id_honors_env_override(monkeypatch):
    monkeypatch.setenv("GOOGLE_TASKLIST_ID", "some-list-id")
    assert _resolve_tasklist_id() == "some-list-id"
