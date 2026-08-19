"""Tests for agent/tools/push_log.py — the log of pushes notify() delivered.

_STORE_PATH is redirected to tmp_path suite-wide by
tests/conftest.py::_isolate_remaining_config_stores, so these tests never touch
the production log. TIMEZONE is pinned because the window's cutoff is a local
calendar time (CLAUDE.md: UTC→local day windows) and the host's zone must not
decide whether a row is in range.
"""

import json
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

    rows = push_log._load()["pushes"]

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

    assert push_log._load()["pushes"][0]["title"] is None
    # And the rendered line carries no empty bold marker where a title would be.
    assert push_log.list_notifications()["summary"].endswith("— no title here")


# --------------------------------------------------------------------------- #
# Reading: order, window, limit
# --------------------------------------------------------------------------- #

def test_newest_first(seed):
    seed(3, "oldest")
    seed(1, "newest")
    seed(2, "middle")

    summary = push_log.list_notifications()["summary"]

    assert summary.index("newest") < summary.index("middle") < summary.index("oldest")


def test_rows_outside_the_window_are_left_out(seed):
    seed(1, "inside")
    seed(9, "outside")

    result = push_log.list_notifications(days=3)

    assert result["shown"] == 1
    assert "inside" in result["summary"] and "outside" not in result["summary"]


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

    result = push_log.list_notifications(limit=2)

    assert result["shown"] == 2
    assert result["summary"].index("newest") < result["summary"].index("middle")
    assert "oldest" not in result["summary"]


def test_limit_is_clamped():
    # A 0 must not silently mean "nothing sent" — that reads as a wrong answer.
    push_log.record("one")

    assert push_log.list_notifications(limit=0)["shown"] == 1
    assert push_log.list_notifications(limit=10_000)["shown"] == 1


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

    result = push_log.list_notifications()

    assert result["shown"] == 1
    assert "good row" in result["summary"] and "no timestamp" not in result["summary"]


def test_a_missing_store_is_nothing_sent_not_an_error():
    # Push can be switched off entirely (NTFY_URL unset), so an absent log is a
    # legitimate state — it must not surface as a broken tool.
    result = push_log.list_notifications()

    assert result["shown"] == 0
    assert "Nothing was pushed" in result["summary"]
    assert "error" not in result


# --- the relayed summary must not state the shown count as the real one ------
# _render counted the rows AFTER the limit slice, so the header said "20
# notification(s) from the last 30 days" when 41 were sent. Not an omission: the
# model is told to relay this block verbatim, so it repeated the wrong number.

def test_a_limited_call_reports_the_true_total_in_the_summary(seed):
    for i in range(10):
        seed(1, f"push number {i}")

    result = push_log.list_notifications(days=7, limit=3)

    assert result["total"] == 10          # everything in the window
    assert result["shown"] == 3
    header = result["summary"].splitlines()[0]
    assert "of 10 notification(s)" in header
    assert not header.startswith("3 notification(s)")


def test_an_unlimited_call_states_a_plain_count(seed):
    for i in range(4):
        seed(1, f"push number {i}")

    header = push_log.list_notifications(days=7)["summary"].splitlines()[0]
    assert header.startswith("4 notification(s)")
    assert "most recent of" not in header


def test_the_rows_are_never_shipped_alongside_the_summary(seed):
    # TOOL_SCHEMA tells the model to relay `summary` verbatim and never compose
    # the list itself, so a second machine-readable copy has no reader. It was
    # 4463 chars against summary's 3513 on a normal week — more than half the
    # payload, for nothing — and the only field it added was the raw ISO stamp,
    # the one form the model must never do date math on.
    seed(1, "one"), seed(1, "two")

    result = push_log.list_notifications(days=7)

    assert set(result) == {"summary", "total", "shown", "days"}
    assert "2026-" not in result["summary"]  # human stamps only, no ISO


def test_an_empty_window_is_unchanged():
    result = push_log.list_notifications(days=7)
    assert result["total"] == 0
    assert "Nothing was pushed" in result["summary"]


# --- bounding the payload ----------------------------------------------------
# MAX_LIMIT caps the row COUNT, not the size, so a plain 20-row week overran the
# agent loop's backstop — which then took a blind slice off the tail.

def test_a_busy_week_now_fits_whole(seed):
    # The real overrun: a plain 20-row week at the tool's own default limit.
    for i in range(20):
        seed(1, f"push number {i}: " + "detail " * 60)

    result = push_log.list_notifications(days=7)

    assert result["shown"] == 20
    for i in range(20):
        assert f"push number {i}" in result["summary"]


def test_the_worst_case_stays_inside_the_loop_backstop(seed):
    # The number in loop.TOOL_RESULT_CHAR_CAPS is only honest if the tool's own
    # budget actually holds the worst case underneath it: MAX_LIMIT rows, each
    # message the full length notify() will record (_MAX_MESSAGE_CHARS).
    from agent import loop

    for i in range(push_log.MAX_LIMIT):
        seed(1, f"push {i} " + "d" * 500, title="A long-ish notification title")

    result = push_log.list_notifications(days=7, limit=push_log.MAX_LIMIT)

    assert len(json.dumps(result)) <= loop.TOOL_RESULT_CHAR_CAPS["list_notifications"]
    # A reduced answer must read as reduced, not as everything there was.
    assert result["total"] == push_log.MAX_LIMIT
    assert result["shown"] < push_log.MAX_LIMIT
    assert f"of {push_log.MAX_LIMIT} notification(s)" in result["summary"]


def test_a_reduced_answer_keeps_the_newest_rows(seed):
    for i in range(push_log.MAX_LIMIT):
        seed(days_ago=1 + i / 100, message=f"push {i} " + "d" * 500)

    summary = push_log.list_notifications(days=7, limit=push_log.MAX_LIMIT)["summary"]

    assert "push 0" in summary          # newest survives
    assert "push 99" not in summary     # oldest is what goes
