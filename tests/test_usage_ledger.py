"""Tests for agent/usage_ledger.py — the append-only model-usage ledger.

Two guarantees carry the whole feature, and both are asserted directly rather
than inferred from a happy path:

  * `record()` never raises. It is called on every model call from inside
    `_llm_chat`, so an exception here would take out a chat turn.
  * An unknown model costs `None`, not `$0`. "We don't know" and "it was free"
    are different answers and the summary counts them differently.

LEDGER_PATH is redirected to tmp_path by an autouse fixture in conftest.py, so
nothing here can touch the real logs/usage.jsonl.
"""

import json

import pytest

from agent import usage_ledger as ul


def rows():
    """Every row currently in the ledger, parsed."""
    if not ul.LEDGER_PATH.exists():
        return []
    return [json.loads(line) for line in
            ul.LEDGER_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]


# --- estimate_cost --------------------------------------------------------

def test_local_calls_are_free_without_consulting_the_price_table():
    # The Mac mini is already paid for. Note the model name is deliberately one
    # that is NOT in _PRICES: the ollama short-circuit must not depend on it.
    assert ul.estimate_cost("ollama", "gemma4:26b-mlx", 100000, 5000) == 0.0


def test_cloud_cost_is_input_and_output_priced_separately():
    # gemini-2.5-flash is $0.30/M in, $2.50/M out.
    # 1000 in = $0.0003, 100 out = $0.00025 -> $0.00055
    assert ul.estimate_cost("gemini", "gemini-2.5-flash", 1000, 100) == 0.00055


def test_a_pinned_version_matches_its_family_prefix():
    pinned = ul.estimate_cost("gemini", "gemini-2.5-flash-preview-09-2025", 1000, 100)
    assert pinned == ul.estimate_cost("gemini", "gemini-2.5-flash", 1000, 100)


def test_longest_matching_prefix_wins():
    # "gemini-2.5-pro" must not be priced by a shorter "gemini-2.5" style match.
    # 1000 in at $1.25/M + 100 out at $10/M = $0.00125 + $0.001 = $0.00225
    assert ul.estimate_cost("gemini", "gemini-2.5-pro", 1000, 100) == 0.00225


def test_an_unknown_model_is_unpriced_not_free():
    # THE point of the whole cost column: a model we have no rate for must read
    # as unknown. Returning 0.0 here would report a real bill as free.
    assert ul.estimate_cost("gemini", "gemini-9.9-nonesuch", 1000, 100) is None


def test_missing_counts_do_not_crash_the_estimate():
    # A failed call reports no tokens at all.
    assert ul.estimate_cost("gemini", "gemini-2.5-flash", None, None) == 0.0


# --- record ---------------------------------------------------------------

def test_record_writes_one_json_line_per_call():
    ul.record("wren", "morning_brief", "ollama", "gemma4:26b-mlx",
              prompt_tokens=1200, output_tokens=340, caller="complete_text")
    ul.record("wren", "wren", "ollama", "gemma4:26b-mlx",
              prompt_tokens=90, output_tokens=12, caller="advance")
    assert len(rows()) == 2


def test_the_row_carries_the_fields_the_page_draws():
    ul.record("wren", "daily_synthesis", "gemini", "gemini-2.5-flash",
              prompt_tokens=1000, output_tokens=100, thinking_tokens=64,
              num_ctx=49152, duration_ms=812, finish_reason="stop",
              caller="complete_text", tools_offered=0)
    row = rows()[0]
    assert row["agent"] == "wren"
    assert row["task"] == "daily_synthesis"
    assert row["caller"] == "complete_text"
    assert row["backend"] == "gemini"
    assert row["model"] == "gemini-2.5-flash"
    assert row["prompt_tokens"] == 1000
    assert row["output_tokens"] == 100
    assert row["thinking_tokens"] == 64
    assert row["num_ctx"] == 49152
    assert row["duration_ms"] == 812
    assert row["finish_reason"] == "stop"
    assert row["tools_offered"] == 0
    assert row["ok"] is True
    assert row["cost_usd"] == 0.00055
    assert row["ts"][:2] == "20"


def test_cost_passed_in_is_kept_not_re_estimated():
    # Claude Code reports what it was actually charged; we must not overwrite a
    # real number with a guess (and "claude-code" isn't in the table anyway).
    ul.record("wren", "build_worker", "anthropic", "claude-opus-5",
              prompt_tokens=5000, output_tokens=900, cost_usd=0.4213)
    assert rows()[0]["cost_usd"] == 0.4213


def test_a_failed_call_is_recorded_as_a_row(monkeypatch):
    ul.record("wren", "wren", "ollama", "gemma4:26b-mlx",
              ok=False, error="OllamaUnavailable", caller="advance")
    row = rows()[0]
    assert row["ok"] is False
    assert row["error"] == "OllamaUnavailable"


def test_record_swallows_a_write_failure(monkeypatch, caplog):
    # The guarantee that matters most: this runs inside every chat turn.
    def boom(*a, **kw):
        raise OSError("disk gone")
    monkeypatch.setattr(ul.LEDGER_PATH.__class__, "open", boom, raising=True)
    ul.record("wren", "wren", "ollama", "gemma4:26b-mlx", prompt_tokens=1)
    # No exception, and nothing written.
    assert rows() == []


def test_record_swallows_an_unserializable_value():
    # A backend could hand us anything; json.dumps must not be able to raise out.
    ul.record("wren", "wren", "ollama", "gemma4:26b-mlx",
              finish_reason=object())
    assert rows() == []


# --- pruning --------------------------------------------------------------

def test_a_small_file_is_never_rewritten(monkeypatch):
    monkeypatch.setenv("WREN_USAGE_MAX_BYTES", "5000000")
    for _ in range(5):
        ul.record("wren", "wren", "ollama", "m", prompt_tokens=1)
    assert len(rows()) == 5


def test_old_rows_are_dropped_once_the_file_is_large(monkeypatch):
    monkeypatch.setenv("WREN_USAGE_RETENTION_DAYS", "30")
    # Seed one ancient row and one recent one by hand, then trip the size cap.
    ul.LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    ul.LEDGER_PATH.write_text(
        json.dumps({"ts": "2020-01-01T00:00:00", "model": "old"}) + "\n"
        + json.dumps({"ts": "2999-01-01T00:00:00", "model": "new"}) + "\n",
        encoding="utf-8")
    monkeypatch.setenv("WREN_USAGE_MAX_BYTES", "1")   # anything non-empty is "large"
    ul.record("wren", "wren", "ollama", "just-written", prompt_tokens=1)
    models = [r.get("model") for r in rows()]
    assert "old" not in models          # aged out
    assert "new" in models              # inside the window
    assert "just-written" in models     # the row that triggered the prune survives


def test_a_row_with_no_timestamp_is_kept(monkeypatch):
    # "I can't tell how old this is" is not evidence that it is old.
    ul.LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    ul.LEDGER_PATH.write_text(
        json.dumps({"model": "no-ts"}) + "\n" + "not json at all\n",
        encoding="utf-8")
    monkeypatch.setenv("WREN_USAGE_MAX_BYTES", "1")
    ul.record("wren", "wren", "ollama", "m", prompt_tokens=1)
    text = ul.LEDGER_PATH.read_text(encoding="utf-8")
    assert "no-ts" in text
    assert "not json at all" in text


def test_a_junk_env_value_falls_back_to_the_default(monkeypatch):
    monkeypatch.setenv("WREN_USAGE_RETENTION_DAYS", "ninety")
    monkeypatch.setenv("WREN_USAGE_MAX_BYTES", "lots")
    assert ul._retention_days() == 90
    assert ul._max_bytes() == 5_000_000


def _tmp_leftovers():
    """Any temp file the prune failed to clean up, dot-prefixed ones included."""
    return sorted(p.name for p in ul.LEDGER_PATH.parent.glob("*.tmp"))


def test_a_prune_leaves_no_leftover_beside_the_ledger(monkeypatch):
    # logs/ is not blanket-gitignored, so a leftover shows up as an untracked
    # multi-megabyte file with nothing to clean it up. Dot-prefixed and unique,
    # the same shape agent/store.py:atomic_write_json uses.
    ul.LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    ul.LEDGER_PATH.write_text(
        json.dumps({"ts": "2999-01-01T00:00:00", "model": "new"}) + "\n",
        encoding="utf-8")
    monkeypatch.setenv("WREN_USAGE_MAX_BYTES", "1")
    ul.record("wren", "wren", "ollama", "m", prompt_tokens=1)
    assert _tmp_leftovers() == [], "prune left a temp file behind"


def test_a_crash_mid_prune_keeps_the_ledger_and_drops_the_temp_file(monkeypatch):
    # The replace is the step that can fail on a full disk. Both halves: the
    # rows already on disk survive, and the temp file is gone.
    ul.LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    ul.LEDGER_PATH.write_text(
        json.dumps({"ts": "2999-01-01T00:00:00", "model": "new"}) + "\n",
        encoding="utf-8")
    monkeypatch.setenv("WREN_USAGE_MAX_BYTES", "1")

    def boom(src, dst):
        raise OSError("no space left on device")

    monkeypatch.setattr(ul.os, "replace", boom)
    # record() swallows everything by contract, so the failure shows up in what
    # it left on disk, not in an exception.
    ul.record("wren", "wren", "ollama", "crashed-during", prompt_tokens=1)

    models = [r.get("model") for r in rows()]
    assert models == ["new", "crashed-during"], "the ledger lost rows"
    assert _tmp_leftovers() == [], "a failed prune left a temp file behind"
