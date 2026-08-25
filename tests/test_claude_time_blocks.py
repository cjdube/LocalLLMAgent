"""Tests for tasks/claude_time_blocks.py — the timeline segmentation, the entry
text Python owns, and the calendar write's idempotency key. The model call, the
model pre-load, the calendar write and the push are all monkeypatched; nothing
touches the network.

TIMEZONE is pinned rather than inherited: Claude Code stamps its session logs in
UTC and the day window is local, so a host in another zone would otherwise move
every block (and the source_id derived from its start).
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone

import pytest

from tasks import _chat_transcripts as ct
from tasks import claude_time_blocks as ctb

_LOGGER = logging.getLogger("test_claude_time_blocks")

TZ = timezone(timedelta(hours=-4))
DAY = datetime(2026, 8, 5, tzinfo=TZ).date()


@pytest.fixture(autouse=True)
def _pin_timezone(monkeypatch):
    monkeypatch.setenv("TIMEZONE", "America/New_York")


def _at(hour, minute=0, project="Wren", session="s1", text="User: do the thing"):
    return {"ts": datetime(2026, 8, 5, hour, minute, tzinfo=TZ), "project": project,
            "slug": "add-the-digest", "session": session, "text": text}


def _never_called(*a, **k):
    raise AssertionError("must not be called on a day with no blocks")


# --------------------------------------------------------------------------- #
# segment
# --------------------------------------------------------------------------- #

def test_segment_splits_on_an_idle_gap():
    events = [_at(9, 0), _at(9, 15), _at(9, 30),      # one stretch
              _at(11, 0), _at(11, 15)]                 # after a 90-minute break
    blocks = ctb.segment(events, gap_minutes=20, min_minutes=10)

    assert [(b["start"].hour, b["start"].minute) for b in blocks] == [(9, 0), (11, 0)]
    assert [(b["end"].hour, b["end"].minute) for b in blocks] == [(9, 30), (11, 15)]


def test_segment_merges_concurrent_sessions_into_one_block():
    # The reason blocks are pooled rather than per-session: these two overlap, and
    # one event each would double-book an hour that was only ever one.
    events = [_at(9, 0, project="Wren", session="s1"),
              _at(9, 10, project="WeighAnchor", session="s2"),
              _at(9, 25, project="Wren", session="s1"),
              _at(9, 40, project="WeighAnchor", session="s2")]

    blocks = ctb.segment(events, gap_minutes=20, min_minutes=10)

    assert len(blocks) == 1
    assert ctb.block_projects(blocks[0]) == ["Wren", "WeighAnchor"]


def test_segment_drops_a_block_below_the_floor():
    # A 90-second glance is not a calendar entry. Measured on the real span, so
    # the rounding below can't promote it over the floor.
    events = [_at(9, 0), _at(9, 1), _at(14, 0), _at(14, 15), _at(14, 30)]
    blocks = ctb.segment(events, gap_minutes=20, min_minutes=10)

    assert len(blocks) == 1
    assert blocks[0]["start"].hour == 14


def test_segment_rounds_edges_out_to_five_minutes():
    events = [_at(13, 41), _at(15, 31)]
    (block,) = ctb.segment(events, gap_minutes=180, min_minutes=10)

    assert (block["start"].hour, block["start"].minute) == (13, 40)
    assert (block["end"].hour, block["end"].minute) == (15, 35)


def test_segment_of_a_quiet_day_is_empty():
    assert ctb.segment([], gap_minutes=20, min_minutes=10) == []


# --------------------------------------------------------------------------- #
# entry text (Python's, not the model's)
# --------------------------------------------------------------------------- #

def test_summary_names_every_project_in_the_block():
    (block,) = ctb.segment([_at(9, 0, project="Wren"), _at(9, 20, project="WeighAnchor")])
    assert ctb.block_summary(block, "shipped the digest") == \
        "AI · Wren, WeighAnchor — shipped the digest"


def test_description_lists_each_session_with_its_own_span():
    (block,) = ctb.segment([
        _at(9, 0, project="Wren", session="s1"),
        _at(9, 10, project="WeighAnchor", session="s2"),
        _at(9, 25, project="Wren", session="s1"),
    ])
    description = ctb.block_description(block)

    assert "Wren · add-the-digest — 9:00 to 9:25 AM" in description
    assert "WeighAnchor · add-the-digest — 9:10 to 9:10 AM" in description
    assert description.endswith("Logged by Wren from Claude Code's local session logs.")


# --------------------------------------------------------------------------- #
# the blurb, and what happens when the model gives us nothing
# --------------------------------------------------------------------------- #

def test_blurb_takes_the_models_first_line(monkeypatch):
    monkeypatch.setattr(ctb, "complete_text",
                        lambda **k: '"added the weekly digest email"\n')
    (block,) = ctb.segment([_at(9, 0), _at(9, 20)])

    assert ctb._blurb(block, None, 6000, _LOGGER) == "added the weekly digest email"


def test_blurb_falls_back_loudly_on_an_empty_response(monkeypatch, caplog):
    # An empty response is what a model that spent its whole budget thinking
    # returns. A block silently titled "working session" would read as an
    # ordinary quiet day rather than a broken prompt, so it has to be logged.
    monkeypatch.setattr(ctb, "complete_text", lambda **k: "   \n")
    (block,) = ctb.segment([_at(9, 0), _at(9, 20)])

    with caplog.at_level(logging.WARNING):
        assert ctb._blurb(block, None, 6000, _LOGGER) == "working session"
    assert "No usable blurb" in caplog.text


def test_blurb_skips_the_model_when_the_block_has_no_text(monkeypatch, caplog):
    monkeypatch.setattr(ctb, "complete_text", _never_called)
    (block,) = ctb.segment([_at(9, 0, text=None), _at(9, 20, text=None)])

    with caplog.at_level(logging.WARNING):
        assert ctb._blurb(block, None, 6000, _LOGGER) == "working session"
    assert "no user/assistant text" in caplog.text


# --------------------------------------------------------------------------- #
# the calendar write
# --------------------------------------------------------------------------- #

def test_source_id_is_derived_from_the_block_start(monkeypatch):
    logged = []
    monkeypatch.setattr(ctb, "log_calendar_event",
                        lambda **k: (logged.append(k) or {"event_id": "e1"}))
    (block,) = ctb.segment([_at(13, 41), _at(14, 30), _at(15, 31)], gap_minutes=90)

    ctb._log_block(block, DAY, "shipped the digest", _LOGGER)

    call = logged[0]
    # Derived from the (rounded) start, so a re-run or a backfill over the same
    # day finds the event it already made instead of duplicating it.
    assert call["source_id"] == "claude-time:2026-08-05:1340"
    assert call["start"] == "2026-08-05T13:40:00"
    assert call["end"] == "2026-08-05T15:35:00"
    assert call["color_id"] == ctb._COLOR_ID


def test_a_failed_insert_does_not_cost_the_rest_of_the_day(monkeypatch):
    monkeypatch.setattr(ctb, "complete_text", lambda **k: "did a thing")
    monkeypatch.setattr(ctb, "log_calendar_event",
                        lambda **k: {"error": "API exploded"} if "0900" in k["source_id"]
                        else {"event_id": "e2"})
    events = [_at(9, 0), _at(9, 15), _at(14, 0), _at(14, 15)]
    monkeypatch.setattr(ctb, "fetch_session_activity", lambda *a: events)

    logged = ctb._run_for_day(None, None, DAY, 20, 10, 6000, None,
                              lambda: None, False, _LOGGER)

    assert [b["start"].hour for b in logged] == [14]


def test_dry_run_writes_nothing(monkeypatch):
    monkeypatch.setattr(ctb, "complete_text", lambda **k: "did a thing")
    monkeypatch.setattr(ctb, "log_calendar_event", _never_called)
    monkeypatch.setattr(ctb, "fetch_session_activity", lambda *a: [_at(9, 0), _at(9, 15)])

    assert ctb._run_for_day(None, None, DAY, 20, 10, 6000, None,
                            lambda: None, True, _LOGGER) == []


def test_quiet_day_never_warms_the_model(monkeypatch):
    # A day with no sessions must not pay the ~17GB local model load — the same
    # early-return contract calendar_colorizer keeps.
    monkeypatch.setattr(ctb, "fetch_session_activity", lambda *a: [])
    monkeypatch.setattr(ctb, "complete_text", _never_called)
    monkeypatch.setattr(ctb, "log_calendar_event", _never_called)

    assert ctb._run_for_day(None, None, DAY, 20, 10, 6000, None,
                            _never_called, False, _LOGGER) == []


# --------------------------------------------------------------------------- #
# end to end, from real-shaped session logs
# --------------------------------------------------------------------------- #

def _write_session(name, stamps, project="/Users/x/Projects/Wren"):
    project_dir = ct.CLAUDE_PROJECTS_DIR / "-Users-x-Projects-Wren"
    project_dir.mkdir(parents=True, exist_ok=True)
    path = project_dir / name
    path.write_text("\n".join(json.dumps({
        "timestamp": ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "cwd": project, "slug": "add-the-digest",
        "message": {"role": "user", "content": "do the thing"},
    }) for ts in stamps))
    latest = max(stamps).timestamp()
    os.utime(path, (latest, latest))


def test_main_logs_one_event_per_block(monkeypatch):
    day_start = datetime(2026, 8, 5, 0, 0, tzinfo=TZ)
    _write_session("morning.jsonl", [day_start.replace(hour=9) + timedelta(minutes=m)
                                     for m in (0, 15, 30)])
    # A concurrent session inside the same stretch, then a separate afternoon one.
    _write_session("overlap.jsonl", [day_start.replace(hour=9, minute=20)])
    _write_session("afternoon.jsonl", [day_start.replace(hour=14) + timedelta(minutes=m)
                                       for m in (0, 15, 30)])

    monkeypatch.setattr(ctb, "warm_model", lambda **k: True)
    monkeypatch.setattr(ctb, "complete_text", lambda **k: "shipped the digest")
    logged = []
    monkeypatch.setattr(ctb, "log_calendar_event",
                        lambda **k: (logged.append(k) or {"event_id": "e"}))
    monkeypatch.setattr("sys.argv", ["claude_time_blocks", "--date", "2026-08-05"])

    assert ctb.main() == 0

    assert [c["source_id"] for c in logged] == [
        "claude-time:2026-08-05:0900", "claude-time:2026-08-05:1400",
    ]
    assert logged[0]["summary"] == "AI · Wren — shipped the digest"
