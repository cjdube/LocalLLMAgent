"""Tests for the model bake-off runner.

Every model call is monkeypatched — nothing here loads a model or opens a
socket. What's under test is the scoring and the safety of the stub dispatch,
because those are what a real run's conclusions rest on.
"""

import logging

import pytest

from agent.toolset import WRITE_TOOLS
from evals import run_eval
from evals.cases_chat import CASES as CHAT_CASES
from evals.cases_tasks import CASES as TASK_CASES


# --------------------------------------------------------------------------- #
# Stub dispatch
# --------------------------------------------------------------------------- #

def test_dispatch_covers_every_real_tool_and_executes_none():
    """A missing key would make agent/loop.py return "unknown tool" and derail
    the turn, scoring the harness's gap as the model's failure."""
    from agent.toolset import DISPATCH

    dispatch = run_eval.make_dispatch({}, [])
    assert set(DISPATCH) <= set(dispatch)
    # Calling any of them returns canned data rather than reaching a real API.
    assert dispatch["fetch_strava"](days=7) == run_eval.DEFAULT_TOOL_RESULT
    assert dispatch["send_email"](subject="x", body="y") == run_eval.DEFAULT_TOOL_RESULT


def test_dispatch_returns_the_cases_canned_result():
    case = {"tool_results": {"fetch_weather": {"today": {"high_f": 78}}}}
    dispatch = run_eval.make_dispatch(case, [])
    assert dispatch["fetch_weather"]() == {"today": {"high_f": 78}}
    # A tool the case didn't name still answers, so one stray call can't end the turn.
    assert dispatch["search_web"](query="x") == run_eval.DEFAULT_TOOL_RESULT


def test_load_tools_appends_to_the_live_tools_list():
    """Mirrors chat/server.py: load_tools mutates THIS turn's list in place, so
    advance() re-sends the enlarged set on its next iteration."""
    tools = [{"function": {"name": "fetch_weather"}}]
    dispatch = run_eval.make_dispatch({}, tools)

    result = dispatch["load_tools"](group="games")

    assert "list_games" in result["tools_now_available"]
    assert "list_games" in {t["function"]["name"] for t in tools}


def test_load_tools_rejects_an_unknown_group():
    assert "error" in run_eval.make_dispatch({}, [])["load_tools"](group="nonsense")


def test_tool_calls_made_ignores_load_tools():
    messages = [
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "load_tools", "arguments": {"group": "games"}}}]},
        {"role": "assistant", "tool_calls": [
            {"function": {"name": "list_games", "arguments": {}}}]},
    ]
    assert run_eval.tool_calls_made(messages) == [{"name": "list_games", "arguments": {}}]


# --------------------------------------------------------------------------- #
# Chat scoring
# --------------------------------------------------------------------------- #

def test_score_chat_accepts_the_expected_tool():
    case = {"expect_tool": "fetch_weather"}
    score = run_eval.score_chat(case, [{"name": "fetch_weather", "arguments": {}}], "78F")
    assert score["called_expected"] is True


def test_score_chat_rejects_the_wrong_tool():
    case = {"expect_tool": "fetch_weather"}
    score = run_eval.score_chat(case, [{"name": "search_web", "arguments": {}}], "")
    assert score["called_expected"] is False


def test_score_chat_accepts_any_of_several_tools():
    case = {"expect_tool": "get_tasks_due_soon",
            "expect_any_of": ["get_tasks_due_soon", "get_tasks"]}
    score = run_eval.score_chat(case, [{"name": "get_tasks", "arguments": {}}], "")
    assert score["called_expected"] is True


def test_score_chat_flags_a_tool_called_when_none_was_wanted():
    case = {"expect_tool": None}
    called = run_eval.score_chat(case, [{"name": "search_web", "arguments": {}}], "hi")
    quiet = run_eval.score_chat(case, [], "hi")
    assert called["no_tool_expected_ok"] is False
    assert quiet["no_tool_expected_ok"] is True
    # The tool checks don't apply to a no-tool case and must not read as failures.
    assert quiet["called_expected"] is None and quiet["args_ok"] is None


def test_score_chat_checks_argument_values():
    """The weekday rule: the PHRASE goes to the tool, not a date the model
    worked out. A resolved date fails even when it is the right day."""
    case = {"expect_tool": "get_events_by_date",
            "arg_checks": {"date": lambda v: "tuesday" in str(v).lower()}}
    good = run_eval.score_chat(
        case, [{"name": "get_events_by_date", "arguments": {"date": "next Tuesday"}}], "")
    bad = run_eval.score_chat(
        case, [{"name": "get_events_by_date", "arguments": {"date": "2026-08-18"}}], "")
    assert good["args_ok"] is True
    assert bad["args_ok"] is False and bad["args_failed_keys"] == ["date"]


def test_score_chat_checks_args_against_the_best_accepted_call():
    """A case that accepts several tools must not be scored on whichever the
    model happened to call first — the other call may be the one carrying the
    arguments. This scored a real run wrong before it was fixed."""
    case = {"expect_tool": "log_calendar_event",
            "expect_any_of": ["fetch_strava", "log_calendar_event"],
            "arg_checks": {"summary": lambda v: bool(v)}}
    calls = [{"name": "fetch_strava", "arguments": {"days": 7}},
             {"name": "log_calendar_event", "arguments": {"summary": "Volleyball"}}]

    assert run_eval.score_chat(case, calls, "")["args_ok"] is True


def test_score_chat_fails_args_when_the_tool_was_never_called():
    case = {"expect_tool": "set_reminder", "arg_checks": {"when": lambda v: bool(v)}}
    assert run_eval.score_chat(case, [], "")["args_ok"] is False


def test_score_chat_detects_fabrication():
    case = {"expect_tool": "list_games", "final_must_not_contain": ["wordle", "chess"]}
    score = run_eval.score_chat(
        case, [{"name": "list_games", "arguments": {}}],
        "We could play Wordle, or a game of chess!")
    assert score["no_fabrication_ok"] is False
    assert score["fabricated"] == ["wordle", "chess"]


def test_score_chat_checks_the_final_answer_used_the_tool_result():
    case = {"expect_tool": "fetch_weather", "final_must_contain": ["78"]}
    used = run_eval.score_chat(case, [{"name": "fetch_weather", "arguments": {}}],
                               "It hits 78 today.")
    ignored = run_eval.score_chat(case, [{"name": "fetch_weather", "arguments": {}}],
                                  "It should be warm.")
    assert used["final_ok"] is True
    assert ignored["final_ok"] is False


# --------------------------------------------------------------------------- #
# Task scoring
# --------------------------------------------------------------------------- #

def test_score_task_flags_an_empty_answer():
    """The thinking-budget failure. It must be distinguishable from a parse
    failure, which is how it hid for three incidents."""
    score = run_eval.score_task({"expect_count": 10}, "", [], None)
    assert score["non_empty"] is False
    assert score["parsed_ok"] is True
    assert score["complete"] is False


def test_score_task_flags_a_partial_result():
    """8 of 10 is the silent-loss shape — a digest with 8 leads looks like one
    with 10."""
    score = run_eval.score_task({"expect_count": 10}, "x" * 50, list(range(8)), None)
    assert score["non_empty"] is True
    assert score["complete"] is False
    assert (score["result_count"], score["expect_count"]) == (8, 10)


def test_score_task_passes_a_complete_result():
    score = run_eval.score_task({"expect_count": 3}, "x", [1, 2, 3], None)
    assert score["complete"] is True


def test_score_task_records_a_parser_that_raised():
    score = run_eval.score_task({"expect_count": 5}, "not json", None,
                                "RuntimeError: bad JSON")
    assert score["parsed_ok"] is False
    assert "bad JSON" in score["parse_error"]


def test_score_task_leaves_complete_unset_when_silence_is_allowed():
    """daily_synthesis may legitimately return no nudges, so an empty LIST is
    not a failure there — an empty ANSWER still is."""
    score = run_eval.score_task({"expect_count": None}, "NONE", [], None)
    assert score["complete"] is None
    assert score["non_empty"] is True


# --------------------------------------------------------------------------- #
# Runners
# --------------------------------------------------------------------------- #

def test_run_chat_case_records_calls_timing_and_tokens(monkeypatch):
    def fake_advance(messages, tools, dispatch, **kwargs):
        messages.append({"role": "assistant", "tool_calls": [
            {"function": {"name": "fetch_weather", "arguments": {}}}]})
        kwargs["logger"].info(
            "ollama_chat model=x num_ctx=32768 prompt_tokens=1200 eval_tokens=45")
        return {"type": "final", "text": "It hits 78 today."}

    monkeypatch.setattr(run_eval, "advance", fake_advance)
    case = {"id": "t", "prompt": "What's the weather?", "expect_tool": "fetch_weather",
            "final_must_contain": ["78"]}

    record = run_eval.run_chat_case(case, "m", 1, "system", 300.0)

    assert record["outcome"] == "final"
    assert record["calls"] == [{"name": "fetch_weather", "arguments": {}}]
    assert record["score"]["called_expected"] is True
    assert record["score"]["final_ok"] is True
    assert (record["prompt_tokens"], record["eval_tokens"]) == (1200, 45)
    assert isinstance(record["elapsed_s"], float)


def test_run_chat_case_scores_a_gated_write_as_called(monkeypatch):
    """advance() pauses on a WRITE_TOOLS call and executes nothing. The pause is
    the pass — it means the model made the call instead of describing it."""
    def fake_advance(messages, tools, dispatch, **kwargs):
        call = {"function": {"name": "create_task", "arguments": {"title": "Renew rego"}}}
        messages.append({"role": "assistant", "tool_calls": [call]})
        return {"type": "confirm", "call": call}

    monkeypatch.setattr(run_eval, "advance", fake_advance)
    case = {"id": "t", "prompt": "Add a task", "expect_tool": "create_task",
            "arg_checks": {"title": lambda v: bool(v)}}

    record = run_eval.run_chat_case(case, "m", 1, "system", 300.0)

    assert record["outcome"] == "confirm"
    assert record["score"]["called_expected"] is True
    assert record["score"]["args_ok"] is True


def test_run_chat_case_records_a_model_error_instead_of_raising(monkeypatch):
    """A wedged Ollama partway through a two-hour run must cost one case, not
    the whole run."""
    from agent.loop import OllamaUnavailable

    def boom(*a, **kw):
        raise OllamaUnavailable("Ollama is up but stalled")

    monkeypatch.setattr(run_eval, "advance", boom)
    record = run_eval.run_chat_case({"id": "t", "prompt": "hi", "expect_tool": None},
                                    "m", 1, "system", 300.0)

    assert record["outcome"] == "unavailable"
    assert "stalled" in record["error"]


def test_run_task_case_captures_a_parser_that_raised(monkeypatch):
    def bad_parse(raw):
        raise RuntimeError("Could not parse model response as JSON")

    monkeypatch.setattr(run_eval, "complete_text", lambda **kw: "prose, not JSON")
    case = {"id": "t", "task": "calendar_colorizer", "system": "s", "user": "u",
            "parse": bad_parse, "expect_count": 12, "think": False}

    record = run_eval.run_task_case(case, "m", 1, 300.0)

    assert record["score"]["parsed_ok"] is False
    assert "Could not parse" in record["score"]["parse_error"]


def test_run_task_case_keeps_loop_warnings(monkeypatch):
    """agent/loop.py's num_predict warning is the direct evidence for a
    thinking model eating its own answer, so it has to survive into the record."""
    def fake_complete(**kwargs):
        kwargs["logger"].warning("ollama generation (3072 tokens) reached num_predict")
        return ""

    monkeypatch.setattr(run_eval, "complete_text", fake_complete)
    case = {"id": "t", "task": "x", "system": "s", "user": "u",
            "parse": lambda raw: [], "expect_count": 5, "think": None}

    record = run_eval.run_task_case(case, "m", 1, 300.0)

    assert record["score"]["non_empty"] is False
    assert any("num_predict" in w for w in record["loop_warnings"])


def test_case_loggers_never_reach_the_production_logs():
    """propagate=False, or every model call in a two-hour run would also land in
    logs/wren.log and the 8am log inspector would report on the eval."""
    log, _ = run_eval._logger_for("case")
    assert log.propagate is False
    assert all(not isinstance(h, logging.FileHandler) for h in log.handlers)


def test_unload_is_best_effort(monkeypatch):
    """A failed eviction costs accuracy on the next arm, not the run."""
    def boom(*a, **kw):
        raise ConnectionError("refused")

    monkeypatch.setattr(run_eval.requests, "post", boom)
    assert run_eval.unload("m") is False


def test_run_unloads_each_model_when_its_arm_finishes(monkeypatch, tmp_path):
    """Two 19GB models resident at once would page, and the second arm's
    latency — the number being measured — would be the artefact."""
    unloaded = []
    monkeypatch.setattr(run_eval, "warm_model", lambda **kw: True)
    monkeypatch.setattr(run_eval, "unload", lambda m: unloaded.append(m) or True)
    monkeypatch.setattr(run_eval, "advance",
                        lambda messages, tools, dispatch, **kw: {"type": "final", "text": "hi"})
    monkeypatch.setattr(run_eval, "CHAT_CASES",
                        [{"id": "only", "prompt": "hi", "expect_tool": None}])

    run_eval.run(["m1", "m2"], reps=1, path="chat", timeout=300.0,
                 out=tmp_path / "raw.json")

    assert unloaded == ["m1", "m2"]


def test_run_writes_records_and_never_touches_production(monkeypatch, tmp_path):
    monkeypatch.setattr(run_eval, "warm_model", lambda **kw: True)
    monkeypatch.setattr(run_eval, "unload", lambda m: True)
    monkeypatch.setattr(run_eval, "advance",
                        lambda messages, tools, dispatch, **kw: {"type": "final", "text": "hi"})
    monkeypatch.setattr(run_eval, "CHAT_CASES",
                        [{"id": "only", "prompt": "hi", "expect_tool": None}])
    out = tmp_path / "raw.json"

    written = run_eval.run(["m1", "m2"], reps=2, path="chat", timeout=300.0, out=out)

    import json
    records = json.loads(written.read_text())
    assert len(records) == 4
    assert {r["model"] for r in records} == {"m1", "m2"}


# --------------------------------------------------------------------------- #
# The case sets themselves
# --------------------------------------------------------------------------- #

def test_chat_cases_name_real_tools():
    """A typo in a case id would score every model as failing that case."""
    from agent.toolset import DISPATCH

    for case in CHAT_CASES:
        for name in (case.get("expect_any_of") or []) + [case.get("expect_tool")]:
            if name is not None:
                assert name in DISPATCH, f"{case['id']} expects unknown tool {name}"


def test_chat_case_tools_are_reachable_for_that_prompt():
    """Keyword pre-loading has to actually offer the tool the case expects,
    otherwise the case measures GROUP_KEYWORDS rather than the model. (The model
    could still get there via load_tools — this just keeps the common path honest.)"""
    from agent.toolset import groups_for_message, tools_for

    for case in CHAT_CASES:
        expected = case.get("expect_any_of") or ([case["expect_tool"]]
                                                 if case.get("expect_tool") else [])
        if not expected:
            continue
        offered = {t["function"]["name"]
                   for t in tools_for(groups_for_message(case["prompt"]))}
        assert offered & set(expected), (
            f"{case['id']}: none of {expected} is offered for {case['prompt']!r}")


def test_arg_checks_name_parameters_the_tool_actually_declares():
    """The guard that pays for itself. `arg_checks` keyed on a parameter the
    schema doesn't have can never pass, so every model scores 0 on that case and
    the harness's typo reads as a shared model weakness. Caught exactly that:
    get_events_by_date takes start/end, and a case checked a "date" argument.
    Cheap here; two hours of Ollama time to discover in a run."""
    from agent.toolset import TOOLS

    props = {t["function"]["name"]: set(t["function"]["parameters"]["properties"])
             for t in TOOLS}
    for case in CHAT_CASES:
        checks = case.get("arg_checks") or {}
        if not checks:
            continue
        accepted = case.get("expect_any_of") or [case["expect_tool"]]
        # Some tool the case accepts must declare every checked parameter.
        assert any(set(checks) <= props.get(name, set()) for name in accepted), (
            f"{case['id']}: no accepted tool of {accepted} declares all of "
            f"{sorted(checks)} — declared: "
            + "; ".join(f"{n}={sorted(props.get(n, []))}" for n in accepted))


def test_case_ids_are_unique():
    ids = [c["id"] for c in CHAT_CASES] + [c["id"] for c in TASK_CASES]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("case", TASK_CASES, ids=lambda c: c["id"])
def test_task_cases_carry_a_real_prompt_and_working_parser(case):
    assert case["system"].strip() and case["user"].strip()
    # The parser must survive an empty answer without raising, or record it as a
    # parse error — both are handled, but it must not be a surprise.
    try:
        case["parse"]("")
    except Exception as e:
        assert isinstance(e, (RuntimeError, ValueError)), e


@pytest.mark.parametrize("case", TASK_CASES, ids=lambda c: c["id"])
def test_a_perfect_answer_scores_as_a_pass(case):
    """Feed each case an ideal answer and require a clean pass.

    The guard for the other half of the harness. A parser wired to the wrong
    fixture scores a flawless model answer as a failure, and in the results
    table that is indistinguishable from the model being bad. It happened twice
    during the build — once because _parse_scores was handed the compacted leads
    instead of the raw ones, which drops the id it keys on."""
    parsed = case["parse"](case["golden"])
    score = run_eval.score_task(case, case["golden"], parsed, None)

    assert score["non_empty"] is True
    assert score["parsed_ok"] is True
    assert score["complete"] is not False, (
        f"{case['id']}: a perfect answer parsed to {score['result_count']} "
        f"result(s), not {score['expect_count']} — the case is miswired")


# --------------------------------------------------------------------------- #
# Fixture dates
# --------------------------------------------------------------------------- #

def test_chat_cases_hardcode_no_dates():
    """Fixture dates must be derived from today, never written out.

    The chat system prompt bakes in the real current date, so an absolute
    fixture date stops meaning what the case meant when it was written — and
    the scoring inverts rather than failing. `calendar_upcoming` pinned an
    event to 2026-08-17; by 2026-08-18 the model that correctly said "that was
    yesterday" scored 0/3 and the model that called it "tomorrow" scored 3/3,
    because the check only looked for the event's name."""
    import re
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent / "evals" / "cases_chat.py"
    # Only the case list. The helpers above it document their output format
    # ("'Tuesday, August 18, 2026'"), which is an example, not a fixture.
    text = src.read_text().split("CASES = [", 1)[1]

    iso = re.findall(r'"(\d{4}-\d{2}-\d{2}[^"]*)"', text)
    assert not iso, f"absolute date literal(s) in cases_chat.py: {iso}"

    months = re.findall(
        r'"[^"]*(January|February|March|April|May|June|July|August|September|'
        r'October|November|December) \d{1,2}[^"]*"', text)
    assert not months, f"written-out date(s) in cases_chat.py: {months}"


def test_upcoming_event_case_rejects_the_wrong_day():
    """The guard that the rot removed: naming the event is not enough, the
    reply has to put it on the right day."""
    case = next(c for c in CHAT_CASES if c["id"] == "calendar_upcoming")
    forbidden = case["final_must_not_contain"]

    assert "yesterday" in forbidden
    # Today's and tomorrow's weekday names are the two misdatings observed.
    from evals.cases_chat import _day
    assert _day(0).strftime("%A").lower() in forbidden
    assert _day(1).strftime("%A").lower() in forbidden
    # ...and never the correct day's own name.
    assert _day(2).strftime("%A").lower() not in forbidden


# --- the continuation turn, which nothing measured before 2026-08-18 ----------

def test_score_chat_flags_a_repeated_confirmation():
    case = {"expect_tool": "log_calendar_event"}
    calls = [{"name": "log_calendar_event", "arguments": {}}]

    clean = run_eval.score_chat(case, calls, "Done.", reconfirmed=False)
    repeat = run_eval.score_chat(case, calls, "Done.", reconfirmed=True)

    assert clean["no_repeat_confirm_ok"] is True
    assert repeat["no_repeat_confirm_ok"] is False


def test_score_chat_leaves_the_repeat_check_unasked_for_normal_cases():
    """None, not False — a case that stops at the first card never tested it."""
    case = {"expect_tool": "fetch_weather"}
    score = run_eval.score_chat(case, [{"name": "fetch_weather", "arguments": {}}], "78F")

    assert score["no_repeat_confirm_ok"] is None


def test_the_confirm_cases_cover_both_decisions():
    """One approve and one decline: the two halves of the loop had different
    causes (an unlabelled result, and a decline shaped like a failure)."""
    confirm_cases = [c for c in CHAT_CASES if c.get("confirm")]

    assert {c["confirm"] for c in confirm_cases} == {"approve", "decline"}
    # Pointless unless the tool actually pauses.
    assert all(c["expect_tool"] in WRITE_TOOLS for c in confirm_cases)
