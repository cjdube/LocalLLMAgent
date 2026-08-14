"""Tests for agent/tools/push_log.py — the log of pushes notify() delivered.

_STORE_PATH is redirected to tmp_path suite-wide by
tests/conftest.py::_isolate_remaining_config_stores, so these tests never touch
the production log. TIMEZONE is pinned because the window's cutoff is a local
calendar time (CLAUDE.md: UTC→local day windows) and the host's zone must not
decide whether a row is in range.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from agent.store import atomic_write_json
from agent.tools import push_log


@pytest.fixture(autouse=True)
def _pin_timezone(monkeypatch):
    monkeypatch.setenv("TIMEZONE", "America/New_York")


@pytest.fixture
def seed():
    """Write a row stamped `days_ago` days back, bypassing record() so a test can
    place a row outside the window record() would ever produce."""
    def write(days_ago: float, message: str, title: str | None = None) -> str:
        ts = (push_log._now() - timedelta(days=days_ago)).isoformat(timespec="seconds")
        data = push_log._load()
        data["pushes"].append(
            {"ts": ts, "title": title, "message": message, "priority": None})
        atomic_write_json(push_log._STORE_PATH, data)
        return ts
    return write


# --------------------------------------------------------------------------- #
# Recording
# --------------------------------------------------------------------------- #

def test_records_a_push_and_reads_it_back():
    push_log.record("Call the dentist", title="Reminder", priority="high")

    rows = push_log.list_notifications()["notifications"]

    assert len(rows) == 1
    assert rows[0]["message"] == "Call the dentist"
    assert rows[0]["title"] == "Reminder"


def test_stamps_the_row_in_the_local_zone():
    # A UTC stamp would shift an evening push into the next day, so every
    # "did you send me anything yesterday?" near the boundary would misreport.
    push_log.record("evening push")

    ts = push_log._load()["pushes"][0]["ts"]

    assert datetime.fromisoformat(ts).utcoffset() == \
        datetime.now(ZoneInfo("America/New_York")).utcoffset()


def test_a_title_is_optional():
    push_log.record("no title here")

    assert push_log.list_notifications()["notifications"][0]["title"] is None


# --------------------------------------------------------------------------- #
# Reading: order, window, limit
# --------------------------------------------------------------------------- #

def test_newest_first(seed):
    seed(3, "oldest")
    seed(1, "newest")
    seed(2, "middle")

    messages = [r["message"] for r in push_log.list_notifications()["notifications"]]

    assert messages == ["newest", "middle", "oldest"]


def test_rows_outside_the_window_are_left_out(seed):
    seed(1, "inside")
    seed(9, "outside")

    messages = [r["message"] for r in push_log.list_notifications(days=3)["notifications"]]

    assert messages == ["inside"]


def test_days_is_clamped_to_the_stores_retention(seed):
    # A caller asking for a year must not be told the window covered one; the
    # store only keeps 30 days.
    assert push_log.list_notifications(days=365)["days"] == push_log.MAX_DAYS
    assert push_log.list_notifications(days=0)["days"] == push_log.MIN_DAYS
    assert push_log.list_notifications(days="lots")["days"] == push_log.DEFAULT_DAYS


def test_limit_takes_the_newest_rows(seed):
    seed(3, "oldest")
    seed(2, "middle")
    seed(1, "newest")

    rows = push_log.list_notifications(limit=2)["notifications"]

    assert [r["message"] for r in rows] == ["newest", "middle"]


def test_limit_is_clamped():
    # A 0 must not silently mean "nothing sent" — that reads as a wrong answer.
    push_log.record("one")

    assert len(push_log.list_notifications(limit=0)["notifications"]) == 1
    assert len(push_log.list_notifications(limit=10_000)["notifications"]) == 1


# --------------------------------------------------------------------------- #
# summary — the field the model is told to relay verbatim
# --------------------------------------------------------------------------- #

def test_summary_carries_every_row(seed):
    seed(1, "Call the dentist", title="Reminder")
    seed(2, "morning brief failed", title="Wren")

    summary = push_log.list_notifications()["summary"]

    assert "Call the dentist" in summary
    assert "morning brief failed" in summary
    assert "2 notification(s)" in summary


def test_summary_says_an_empty_window_is_normal():
    # The model relays this instead of composing an apology — and must not read
    # it as a fault worth reporting.
    summary = push_log.list_notifications()["summary"]

    assert "Nothing was pushed" in summary
    assert "normal" in summary


# --------------------------------------------------------------------------- #
# Pruning + degradation
# --------------------------------------------------------------------------- #

def test_pruning_drops_rows_past_retention_on_write(seed):
    seed(45, "ancient")
    seed(1, "recent")

    push_log.record("newest")

    messages = [r["message"] for r in push_log._load()["pushes"]]
    assert "ancient" not in messages
    assert messages == ["recent", "newest"]


def test_pruning_caps_the_row_count(monkeypatch):
    monkeypatch.setattr(push_log, "_MAX_ROWS", 3)
    for i in range(5):
        push_log.record(f"push {i}")

    messages = [r["message"] for r in push_log._load()["pushes"]]

    assert messages == ["push 2", "push 3", "push 4"]


def test_an_unstamped_row_is_dropped_rather_than_reported(seed):
    seed(1, "good row")
    data = push_log._load()
    data["pushes"].append({"message": "no timestamp", "title": None})
    atomic_write_json(push_log._STORE_PATH, data)

    messages = [r["message"] for r in push_log.list_notifications()["notifications"]]

    assert messages == ["good row"]


def test_a_missing_store_is_nothing_sent_not_an_error():
    # Push can be switched off entirely (NTFY_URL unset), so an absent log is a
    # legitimate state — it must not surface as a broken tool.
    result = push_log.list_notifications()

    assert result["notifications"] == []
    assert "error" not in result
