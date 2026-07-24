"""Tests for agent.escalations — the manual frontier-escalation log.

The store path is redirected to tmp_path suite-wide by tests/conftest.py, so
these exercise the real append/prune/shape without touching config/.
"""

from agent import escalations
from agent.store import load_json


def _record(**over):
    args = dict(request="summarize my week", local_reply="weak local answer",
                prompt_tokens=1234, backend="gemini", model="gemini-2.5-flash (gemini)",
                outcome="ok")
    args.update(over)
    return escalations.record_escalation(**args)


def test_record_appends_a_fully_shaped_row():
    rec = _record()
    assert set(rec) == {"ts", "request", "local_reply", "prompt_tokens",
                        "backend", "model", "outcome"}
    stored = load_json(escalations._STORE_PATH, {"escalations": []})["escalations"]
    assert stored == [rec]
    # ts is an ISO-8601 UTC stamp (the timestamp policy: store UTC, not a slice).
    assert rec["ts"].endswith("+00:00")


def test_records_accumulate_newest_last():
    _record(request="first")
    _record(request="second")
    stored = load_json(escalations._STORE_PATH, {"escalations": []})["escalations"]
    assert [r["request"] for r in stored] == ["first", "second"]


def test_prunes_to_max_records_keeping_the_newest(monkeypatch):
    monkeypatch.setattr(escalations, "MAX_RECORDS", 3)
    for i in range(5):
        _record(request=f"q{i}")
    stored = load_json(escalations._STORE_PATH, {"escalations": []})["escalations"]
    assert [r["request"] for r in stored] == ["q2", "q3", "q4"]


def test_error_outcome_is_recorded_verbatim():
    rec = _record(outcome="error:timeout")
    assert rec["outcome"] == "error:timeout"
