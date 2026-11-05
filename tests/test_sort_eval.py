"""The wiki sort test harness: parsing, the retry, and the scoring. No model."""

import json

from evals import run_eval, sort_eval

GROUPS = [{"id": "a", "title": "Alpha", "line": "First.", "pages": ["p1", "p2"]},
          {"id": "b", "title": "Beta", "line": "Second.", "pages": ["p3"]}]
SUMMARIES = {"p1": "one", "p2": "two", "p3": "three"}


def test_load_key_leaves_out_groups_sorted_by_prefix(tmp_path):
    path = tmp_path / "key.json"
    path.write_text(json.dumps({"groups": [
        {"id": "dated", "title": "D", "line": "", "prefix": "daily-"}, *GROUPS]}))

    groups, answers = sort_eval.load_key(path)

    assert [g["id"] for g in groups] == ["a", "b"]
    assert answers == {"p1": 0, "p2": 0, "p3": 1}


def test_parse_pairs_reads_each_line_and_tolerates_decoration():
    raw = "1 2\nPage 2: Group 1\n3 -> 2\nHere is the sort."
    assert sort_eval.parse_pairs(raw, 3, 2) == {1: 2, 2: 1, 3: 2}


def test_parse_pairs_drops_out_of_range_and_keeps_the_first_answer():
    raw = "1 2\n1 1\n9 1\n2 9\n0 1"
    assert sort_eval.parse_pairs(raw, 3, 2) == {1: 2}


def test_prompt_numbers_groups_and_pages_from_one():
    prompt = sort_eval.build_prompt(["p3"], GROUPS, SUMMARIES)
    assert "1. Alpha: First.\n2. Beta: Second." in prompt
    assert "PAGES\n1. p3: three" in prompt


def test_trial_retries_missing_pages_once_then_counts_them_unplaced(monkeypatch):
    prompts = []

    def fake(system, user, **kwargs):
        assert kwargs["think"] is False
        prompts.append(user)
        pages = user.split("PAGES\n")[1].splitlines()
        # Place only p1, whichever number it was given. The rest never come back.
        return "\n".join(f"{i} 1" for i, line in enumerate(pages, 1) if " p1:" in f" {line[3:]}")

    monkeypatch.setattr(sort_eval, "complete_text", fake)

    trial = sort_eval.run_trial(["p1", "p2", "p3"], GROUPS, SUMMARIES, "m", 1, 5.0)
    score = sort_eval.score_trial(trial, {"p1": 0, "p2": 0, "p3": 1})

    assert len(prompts) == 2  # one batch, then exactly one retry
    assert "p1" not in prompts[1] and "p2" in prompts[1] and "p3" in prompts[1]
    assert trial["missing_before_retry"] == 2 and trial["retries"] == 1
    assert score["placed"] == 1 and score["unplaced"] == ["p2", "p3"]
    assert any("parsed" in w for w in trial["warnings"])  # the silent-loss rule


def test_a_failed_call_reaches_the_retry_instead_of_ending_the_trial(monkeypatch):
    calls = []

    def fake(system, user, **kwargs):
        calls.append(user)
        if len(calls) == 1:
            raise TimeoutError("slow")
        return "1 1\n2 1\n3 2"

    monkeypatch.setattr(sort_eval, "complete_text", fake)

    trial = sort_eval.run_trial(["p1", "p2", "p3"], GROUPS, SUMMARIES, "m", 1, 5.0)

    assert len(trial["placed"]) == 3
    assert trial["warnings"]


def test_score_counts_matches_against_the_answer_key():
    trial = {"placed": {"p1": 0, "p2": 1, "p3": 1}}
    score = sort_eval.score_trial(trial, {"p1": 0, "p2": 0, "p3": 1})
    assert score["matched"] == 2
    assert score["confused"] == {(0, 1): 1}


def test_disagreements_lists_only_misses():
    records = [{"model": "m", "trial": 1, "placed": {"p1": 0, "p2": 1}}]
    assert sort_eval.disagreements(records, {"p1": 0, "p2": 0}) == [("m", 1, "p2", 0, 1)]


def test_results_stay_out_of_the_raw_files_evals_score_reads(monkeypatch, tmp_path):
    monkeypatch.setattr(sort_eval, "_require_vault", lambda: (tmp_path, None))
    monkeypatch.setattr(sort_eval, "_page_facts", lambda v: {
        n: [s, 1, 1, 1, ""] for n, s in SUMMARIES.items()})
    monkeypatch.setattr(sort_eval, "warm_model", lambda **k: True)
    monkeypatch.setattr(run_eval, "unload", lambda m: True)
    monkeypatch.setattr(sort_eval, "complete_text", lambda s, u, **k: "1 1\n2 1\n3 1")
    key = tmp_path / "key.json"
    key.write_text(json.dumps({"groups": GROUPS}))

    assert sort_eval.main(["--key", str(key), "--models", "m", "--trials", "1"]) == 0

    assert not list(run_eval.RESULTS_DIR.glob("raw_*.json"))
    assert len(list(run_eval.RESULTS_DIR.glob("sort_*.json"))) == 1
