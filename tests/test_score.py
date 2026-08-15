"""Tests for the bake-off aggregator."""

import json

from evals import score


def _chat(model, case, rep=1, **checks):
    base = {"called_expected": None, "args_ok": None, "final_ok": None,
            "no_fabrication_ok": None, "no_tool_expected_ok": None}
    base.update(checks)
    return {"model": model, "case": case, "rep": rep, "path": "chat",
            "elapsed_s": 1.0, "outcome": "final", "score": base,
            "eval_tokens": 10, "loop_warnings": []}


def _task(model, case, rep=1, **checks):
    base = {"non_empty": True, "parsed_ok": True, "complete": True,
            "result_count": 1, "expect_count": 1, "raw_chars": 10}
    base.update(checks)
    return {"model": model, "case": case, "rep": rep, "path": "tasks",
            "elapsed_s": 2.0, "outcome": "final", "score": base,
            "eval_tokens": 20, "loop_warnings": []}


def test_rate_ignores_checks_a_case_did_not_declare():
    """A None check means "not applicable", not "failed" — otherwise a no-tool
    case would drag down every model's argument-accuracy rate."""
    records = [_chat("m", "a", called_expected=True),
               _chat("m", "b", called_expected=False),
               _chat("m", "c")]  # all None
    assert score.rate(records, "called_expected") == (1, 2)


def test_rate_is_zero_over_zero_when_nothing_applies():
    assert score.rate([_chat("m", "a")], "args_ok") == (0, 0)


def test_summarize_splits_by_model_and_path():
    records = [_chat("m1", "a", called_expected=True),
               _chat("m2", "a", called_expected=False),
               _task("m1", "t")]
    summary = score.summarize(records)

    assert summary["m1"]["chat"]["checks"]["called_expected"] == (1, 1)
    assert summary["m2"]["chat"]["checks"]["called_expected"] == (0, 1)
    assert summary["m1"]["tasks"]["runs"] == 1
    assert "tasks" not in summary["m2"]


def test_summarize_counts_errors_and_warnings():
    bad = _chat("m", "a", called_expected=False)
    bad["outcome"] = "unavailable"
    bad["loop_warnings"] = ["generation reached num_predict"]
    summary = score.summarize([bad, _chat("m", "b", called_expected=True)])["m"]["chat"]

    assert summary["errors"] == 1
    assert summary["warned"] == 1


def test_summarize_reports_median_and_slowest():
    slow = _chat("m", "b", called_expected=True)
    slow["elapsed_s"] = 90.0
    summary = score.summarize([_chat("m", "a", called_expected=True), slow])["m"]["chat"]

    assert summary["median_s"] == 45.5
    assert summary["max_s"] == 90.0


def test_grid_reports_reps_passed_not_an_average(capsys):
    """The whole reason the grid exists: 2-of-3 and 3-of-3 must look different,
    because run-to-run variance is the failure mode this repo keeps hitting."""
    records = [_chat("m", "flaky", rep=1, called_expected=True),
               _chat("m", "flaky", rep=2, called_expected=False),
               _chat("m", "flaky", rep=3, called_expected=True)]
    score.print_grid(records)

    out = capsys.readouterr().out
    assert "2/3" in out
    assert "inconsistent" in out


def test_grid_does_not_flag_a_case_that_always_passes(capsys):
    score.print_grid([_chat("m", "solid", rep=r, called_expected=True) for r in (1, 2)])
    assert "inconsistent" not in capsys.readouterr().out


def test_failures_print_what_the_model_actually_did(capsys):
    bad = _chat("m", "games_vague", called_expected=True, no_fabrication_ok=False)
    bad["score"]["fabricated"] = ["wordle"]
    bad["score"]["tools_called"] = []
    bad["final"] = "We could play Wordle."
    score.print_failures([bad], limit=10)

    out = capsys.readouterr().out
    assert "games_vague" in out and "wordle" in out and "Wordle" in out


def test_failures_says_so_when_there_are_none(capsys):
    score.print_failures([_chat("m", "a", called_expected=True)], limit=10)
    assert "No failures" in capsys.readouterr().out


def test_main_reads_a_raw_file_and_prints(tmp_path, capsys):
    raw = tmp_path / "raw_x.json"
    raw.write_text(json.dumps([_chat("m", "a", called_expected=True), _task("m", "t")]))

    assert score.main([str(raw)]) == 0

    out = capsys.readouterr().out
    assert "CHAT" in out and "SCHEDULED TASKS" in out and "PER-CASE" in out
