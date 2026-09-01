"""Tests for chat/usage.py — the reader behind the /activity page.

Fixtures are written as ledger files on disk, not as row dicts, because that is
what the module actually consumes: the file-per-agent layout, the string cutoff,
and the malformed-line tolerance are all properties of parsing a file and would
be tested away by handing summarize() a clean list.

Two behaviours here are the reason the module exists in this shape, and each has
its own test: an unpriced call must NOT be counted as free, and every day in the
window must get a column even when nothing ran.
"""

import json
from datetime import datetime, timedelta

import pytest

from chat import insights
from chat import usage


NOW = datetime(2026, 9, 1, 18, 0, 0)


def write_ledger(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def row(**kw):
    """A plausible local Ollama row; override anything."""
    base = {
        "ts": NOW.isoformat(timespec="seconds"),
        "agent": "wren", "task": "wren", "caller": "advance",
        "backend": "ollama", "model": "gemma4:26b-mlx",
        "prompt_tokens": 1000, "output_tokens": 100, "thinking_tokens": None,
        "num_ctx": 49152, "duration_ms": 900, "finish_reason": "stop",
        "tools_offered": 12, "ok": True, "error": None, "cost_usd": 0.0,
    }
    base.update(kw)
    return base


@pytest.fixture(autouse=True)
def ledger_dir(tmp_path, monkeypatch):
    """Point the reader at a tmp logs/ and clear its cache between tests.

    The cache keys on (mtime, size), which a test writing two different files
    inside the same second can collide on — so it is emptied rather than relied
    upon here. tests/test_usage cover the cache itself separately.
    """
    monkeypatch.setattr(insights, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setenv(insights.EXTERNAL_ROOTS_ENV, "")
    usage._CACHE.clear()
    return tmp_path / "logs"


def test_reads_wrens_own_ledger(ledger_dir):
    write_ledger(ledger_dir / "usage.jsonl", [row(), row()])
    out = usage.summarize(7, now=NOW)
    assert out["totals"]["calls"] == 2
    assert out["totals"]["tokens"] == 2200
    assert out["totals"]["prompt_tokens"] == 2000
    assert out["totals"]["output_tokens"] == 200


def test_a_missing_ledger_reads_as_no_rows(ledger_dir):
    # Nothing has been written yet — the page must draw, not error.
    out = usage.summarize(7, now=NOW)
    assert out["totals"]["calls"] == 0
    assert out["totals"]["median_ms"] is None
    assert len(out["by_day"]) == 7


def test_rows_older_than_the_window_are_dropped(ledger_dir):
    old = (NOW - timedelta(days=40)).isoformat(timespec="seconds")
    write_ledger(ledger_dir / "usage.jsonl", [row(ts=old), row()])
    assert usage.summarize(7, now=NOW)["totals"]["calls"] == 1
    assert usage.summarize(90, now=NOW)["totals"]["calls"] == 2


def test_a_malformed_line_costs_one_row_not_the_file(ledger_dir):
    path = ledger_dir / "usage.jsonl"
    write_ledger(path, [row(), row()])
    # A half-written row, which is what a killed process leaves behind.
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"ts": "2026-09-01T10:00:00", "prompt_to\n')
    assert usage.summarize(7, now=NOW)["totals"]["calls"] == 2


def test_a_sibling_ledger_is_federated_and_stamped_by_its_file(ledger_dir, tmp_path,
                                                              monkeypatch):
    sibling = tmp_path / "ScribeJay"
    # Note the row claims agent "wren" — the reader must overwrite it with the
    # name of the root the file came from, so a sibling that copies the writer
    # without editing its agent name still lands in its own bucket.
    write_ledger(sibling / "logs" / "usage.jsonl", [row(agent="wren", model="gemma-sj")])
    write_ledger(ledger_dir / "usage.jsonl", [row()])
    monkeypatch.setenv(insights.EXTERNAL_ROOTS_ENV, f"scribejay={sibling}")
    usage._CACHE.clear()
    out = usage.summarize(7, now=NOW)
    agents = {a["agent"]: a for a in out["by_agent"]}
    assert set(agents) == {"wren", "scribejay"}
    assert agents["scribejay"]["calls"] == 1
    assert agents["scribejay"]["title"] == "ScribeJay"


def test_an_uninstrumented_sibling_is_not_an_error(ledger_dir, tmp_path, monkeypatch):
    sibling = tmp_path / "ObsidianWikiAgent"
    sibling.mkdir()                       # the repo exists; no ledger in it yet
    write_ledger(ledger_dir / "usage.jsonl", [row()])
    monkeypatch.setenv(insights.EXTERNAL_ROOTS_ENV, f"wiki={sibling}")
    usage._CACHE.clear()
    out = usage.summarize(7, now=NOW)
    assert out["totals"]["calls"] == 1
    assert [a["agent"] for a in out["by_agent"]] == ["wren"]


# --- the two headline behaviours -----------------------------------------

def test_an_unpriced_call_is_counted_separately_not_as_free(ledger_dir):
    write_ledger(ledger_dir / "usage.jsonl", [
        row(backend="gemini", model="mystery", cost_usd=None),
        row(backend="gemini", model="gemini-2.5-flash", cost_usd=0.25),
    ])
    totals = usage.summarize(7, now=NOW)["totals"]
    assert totals["cost_usd"] == 0.25       # the unknown one is NOT summed in
    assert totals["unpriced_calls"] == 1    # ...it is reported on its own
    assert totals["local_calls"] == 0


def test_every_day_in_the_window_gets_a_column(ledger_dir):
    write_ledger(ledger_dir / "usage.jsonl", [row()])
    out = usage.summarize(7, now=NOW)
    days = [d["day"] for d in out["by_day"]]
    assert len(days) == 7
    assert days[-1] == "2026-09-01"          # today is last
    assert days[0] == "2026-08-26"
    assert days == sorted(days)              # oldest first, no gaps
    assert out["by_day"][0]["models"] == {}  # a quiet day is empty, not absent
    assert out["by_day"][-1]["models"] == {"gemma4:26b-mlx": 1100}


# --- the rest of the summary ---------------------------------------------

def test_local_calls_are_counted_so_a_cloud_bill_reads_in_context(ledger_dir):
    write_ledger(ledger_dir / "usage.jsonl", [
        row(), row(), row(backend="gemini", model="gemini-2.5-flash", cost_usd=0.01)])
    totals = usage.summarize(7, now=NOW)["totals"]
    assert totals["local_calls"] == 2
    assert totals["calls"] == 3


def test_both_backends_words_for_hitting_the_cap_are_counted(ledger_dir):
    # 'length' is Ollama's, 'MAX_TOKENS' is Gemini's; they mean the same thing.
    write_ledger(ledger_dir / "usage.jsonl", [
        row(finish_reason="length"),
        row(finish_reason="FinishReason.MAX_TOKENS"),
        row(finish_reason="stop"),
    ])
    assert usage.summarize(7, now=NOW)["totals"]["cut_off"] == 2


def test_failed_calls_are_counted(ledger_dir):
    write_ledger(ledger_dir / "usage.jsonl", [
        row(ok=False, error="OllamaUnavailable", prompt_tokens=None,
            output_tokens=None, duration_ms=None),
        row(),
    ])
    totals = usage.summarize(7, now=NOW)["totals"]
    assert totals["failed"] == 1
    # A null token count must total as zero, not poison the sum.
    assert totals["tokens"] == 1100


def test_median_duration_ignores_calls_that_reported_none(ledger_dir):
    write_ledger(ledger_dir / "usage.jsonl", [
        row(duration_ms=100), row(duration_ms=300), row(duration_ms=None)])
    assert usage.summarize(7, now=NOW)["totals"]["median_ms"] == 200


def test_thinking_tokens_are_totalled(ledger_dir):
    write_ledger(ledger_dir / "usage.jsonl", [
        row(thinking_tokens=64), row(thinking_tokens=None)])
    assert usage.summarize(7, now=NOW)["totals"]["thinking_tokens"] == 64


def test_buckets_are_sorted_biggest_first(ledger_dir):
    write_ledger(ledger_dir / "usage.jsonl", [
        row(task="small", prompt_tokens=10, output_tokens=0),
        row(task="big", prompt_tokens=9000, output_tokens=0),
        row(task="big", prompt_tokens=1, output_tokens=0),
    ])
    out = usage.summarize(7, now=NOW)
    assert [t["task"] for t in out["by_task"]] == ["big", "small"]
    assert out["by_task"][0]["calls"] == 2
    assert out["by_task"][0]["tokens"] == 9001


def test_by_backend_separates_local_from_cloud(ledger_dir):
    write_ledger(ledger_dir / "usage.jsonl", [
        row(), row(backend="anthropic", model="claude-opus-5", cost_usd=0.42)])
    backends = {b["backend"]: b for b in usage.summarize(7, now=NOW)["by_backend"]}
    assert set(backends) == {"ollama", "anthropic"}
    assert backends["anthropic"]["cost_usd"] == 0.42


def test_an_unnamed_root_falls_back_to_title_case():
    assert usage.agent_title("wren") == "Wren"
    assert usage.agent_title("scribejay") == "ScribeJay"
    assert usage.agent_title("someagent") == "Someagent"


def test_the_cache_invalidates_when_a_row_is_appended(ledger_dir):
    path = ledger_dir / "usage.jsonl"
    write_ledger(path, [row()])
    assert usage.summarize(7, now=NOW)["totals"]["calls"] == 1
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row()) + "\n")
    # No cache clear here on purpose — the (mtime, size) signature must notice.
    assert usage.summarize(7, now=NOW)["totals"]["calls"] == 2
