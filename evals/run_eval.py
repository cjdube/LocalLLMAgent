"""Run the model bake-off and write raw results to evals/results/.

    .venv/bin/python -m evals.run_eval --models gemma4:26b-mlx qwen3.6:27b-mlx

Drives the SAME functions production uses — agent.loop.advance() for chat and
agent.loop.complete_text() for the scheduled tasks — with the SAME system
prompt (chat/server.py:_system_message_content) and the SAME lazily-loaded tool
subset (agent.toolset.tools_for). A harness that built its own Ollama client
would be measuring settings Wren doesn't run under.

Nothing here touches production state:

  * tool dispatch is stubbed, so no Google/Strava/mail/web call is ever made
  * confirmation-gated tools stop inside advance() and execute nothing
  * results go to evals/results/, never to config/ or logs/
  * the per-case logger has propagate=False, so nothing reaches logs/wren.log

Models are looped OUTERMOST on purpose: these are 17-19GB and Ollama serves one
at a time, so interleaving them would pay a full model load per case.
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

from agent.loop import OllamaUnavailable, advance, complete_text, warm_model
from agent.toolset import DISPATCH, TOOL_GROUPS, WRITE_TOOLS, groups_for_message, tools_for
from evals.cases_chat import CASES as CHAT_CASES, DEFAULT_TOOL_RESULT
from evals.cases_tasks import CASES as TASK_CASES

RESULTS_DIR = Path(__file__).resolve().parent / "results"

# Matches the accounting line agent/loop.py logs on every call. Reading it back
# out of the log is how token counts are captured without changing loop.py —
# and it means the harness also records loop.py's own WARNINGs (prompt
# truncation, num_predict cut-off), which are themselves findings.
_TOKENS_RE = re.compile(r"prompt_tokens=(\S+) eval_tokens=(\S+)")


class _Capture(logging.Handler):
    """Collects the records agent/loop.py emits during one case."""

    def __init__(self):
        super().__init__(level=logging.INFO)
        self.records: list[tuple[str, str]] = []

    def emit(self, record):
        self.records.append((record.levelname, record.getMessage()))

    def tokens(self) -> tuple[int, int]:
        """(prompt tokens, generated tokens), summed over every model call this
        case made — a chat case makes at least two."""
        prompt = generated = 0
        for _, msg in self.records:
            m = _TOKENS_RE.search(msg)
            if m:
                for raw, add in ((m.group(1), "p"), (m.group(2), "g")):
                    if raw.isdigit():
                        if add == "p":
                            prompt += int(raw)
                        else:
                            generated += int(raw)
        return prompt, generated

    def warnings(self) -> list[str]:
        return [msg for level, msg in self.records if level in ("WARNING", "ERROR")]


def unload(model: str) -> bool:
    """Evict a model from Ollama's memory — the API equivalent of `ollama stop`.

    Called between arms. These are 17-19GB against 48GB of RAM, so leaving the
    finished arm resident (keep_alive is 30m) means the next one loads alongside
    it and the machine pages. That would show up as the second model being
    slower, which is exactly the number the run is trying to measure. Also the
    only way a num_ctx change takes effect on an already-resident model.

    Best-effort: a failure here costs accuracy, not the run."""
    host = os.getenv("OLLAMA_HOST", "http://localhost:11434")
    try:
        requests.post(f"{host}/api/chat",
                      json={"model": model, "messages": [], "keep_alive": 0},
                      timeout=60).raise_for_status()
        return True
    except Exception as e:
        print(f"  could not unload {model} ({e}); the next arm may share memory "
              f"with it", flush=True)
        return False


def _logger_for(case_id: str) -> tuple[logging.Logger, _Capture]:
    """A throwaway logger that captures and goes nowhere else. propagate=False
    matters: without it every model call would also land in logs/wren.log and
    the eval would pollute the run logs the dashboard reads."""
    log = logging.getLogger(f"evals.{case_id}.{time.monotonic_ns()}")
    log.setLevel(logging.INFO)
    log.handlers = []
    log.propagate = False
    cap = _Capture()
    log.addHandler(cap)
    return log, cap


# --------------------------------------------------------------------------- #
# Stub dispatch
# --------------------------------------------------------------------------- #

def make_dispatch(case: dict, tools: list[dict]) -> dict:
    """A dispatch table matching production's keys but executing nothing.

    Every real tool returns the case's canned result for that tool (or
    DEFAULT_TOOL_RESULT), so the second half of a chat turn — the model reading
    a tool result and answering from it — is exercised with known input.

    load_tools is the exception: it's the real behaviour, appending the group's
    schemas to THIS turn's live tools list, exactly as chat/server.py does. A
    stub would unfairly penalise a model that correctly reaches for a deferred
    group instead of guessing.
    """
    canned = case.get("tool_results") or {}

    def _load_tools(group=None, **_):
        present = {t["function"]["name"] for t in tools}
        added = []
        for schema in TOOL_GROUPS.get(group, []):
            if schema["function"]["name"] not in present:
                tools.append(schema)
                added.append(schema["function"]["name"])
        if not added and group not in TOOL_GROUPS:
            return {"error": f"unknown tool group '{group}'"}
        return {"loaded": group, "tools_now_available": added}

    dispatch = {name: (lambda _n=name, **kw: canned.get(_n, DEFAULT_TOOL_RESULT))
                for name in DISPATCH}
    dispatch["load_tools"] = _load_tools
    return dispatch


def tool_calls_made(messages: list[dict]) -> list[dict]:
    """Every tool call in the turn, in order, as {"name", "arguments"}.
    load_tools is dropped — it's plumbing, not an answer to the user."""
    out = []
    for msg in messages:
        for call in msg.get("tool_calls") or []:
            fn = call.get("function") or {}
            if fn.get("name") != "load_tools":
                out.append({"name": fn.get("name"), "arguments": fn.get("arguments") or {}})
    return out


# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #

def score_chat(case: dict, calls: list[dict], final: str) -> dict:
    """Score one chat run. Every check is None when the case doesn't ask for it,
    so the aggregate can tell "failed" from "not applicable"."""
    expect = case.get("expect_tool", "MISSING")
    accepted = case.get("expect_any_of") or ([expect] if expect else [])
    names = [c["name"] for c in calls]
    score: dict = {"tools_called": names}

    if expect is None:
        # A model that reaches for a tool on small talk burns a round trip and
        # usually answers worse; that's a fail, not a quirk.
        score["no_tool_expected_ok"] = not names
        score["called_expected"] = None
        score["args_ok"] = None
    else:
        score["no_tool_expected_ok"] = None
        score["called_expected"] = any(n in accepted for n in names)
        checks = case.get("arg_checks") or {}
        matches = [c for c in calls if c["name"] in accepted]
        if not checks:
            score["args_ok"] = None
        elif not matches:
            score["args_ok"] = False
        else:
            # Best of the accepted calls, not the first: a case that accepts
            # several tools would otherwise fail on whichever the model happened
            # to call first, and a model that retries a call with better
            # arguments would be scored on its first attempt.
            failures = [[k for k, pred in checks.items()
                         if k not in (c["arguments"] or {})
                         or not pred((c["arguments"] or {}).get(k))]
                        for c in matches]
            bad = min(failures, key=len)
            score["args_ok"] = not bad
            score["args_failed_keys"] = bad

    low = (final or "").lower()
    must = case.get("final_must_contain")
    forbid = case.get("final_must_not_contain")
    score["final_ok"] = all(s.lower() in low for s in must) if must else None
    if forbid:
        hits = [s for s in forbid if s.lower() in low]
        score["no_fabrication_ok"] = not hits
        score["fabricated"] = hits
    else:
        score["no_fabrication_ok"] = None
    return score


def score_task(case: dict, raw: str, parsed, parse_error: str | None) -> dict:
    """Score one scheduled-task run. `non_empty` is the headline: an empty
    answer is the thinking-budget failure, and it's indistinguishable from a
    parse failure downstream unless it's named here."""
    non_empty = bool((raw or "").strip())
    count = None if parsed is None else len(parsed)
    expect = case.get("expect_count")
    return {
        "non_empty": non_empty,
        "parsed_ok": parse_error is None and parsed is not None,
        "parse_error": parse_error,
        "result_count": count,
        "expect_count": expect,
        # None when the case has no fixed count (daily_synthesis, where silence
        # is a legitimate answer) — scored on non_empty alone there.
        "complete": None if expect is None else (count == expect),
        "raw_chars": len(raw or ""),
    }


# --------------------------------------------------------------------------- #
# Runners
# --------------------------------------------------------------------------- #

def run_chat_case(case: dict, model: str, rep: int, system_prompt: str,
                  timeout: float) -> dict:
    """One chat case: real system prompt, real keyword-preloaded tool subset,
    stubbed dispatch."""
    prompt = case["prompt"]
    tools = tools_for(groups_for_message(prompt))
    dispatch = make_dispatch(case, tools)
    messages = [{"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt}]
    log, cap = _logger_for(case["id"])

    record = {"case": case["id"], "model": model, "rep": rep, "path": "chat",
              "preloaded_groups": sorted(groups_for_message(prompt)),
              "tools_offered": len(tools)}
    t0 = time.monotonic()
    try:
        result = advance(messages, tools, dispatch, model=model, logger=log,
                         confirm_before=WRITE_TOOLS, timeout=timeout)
        record["outcome"] = result["type"]
        final = result.get("text", "") if result["type"] == "final" else ""
    except OllamaUnavailable as e:
        record.update(outcome="unavailable", error=str(e))
        final = ""
    except Exception as e:
        # RuntimeError from the MAX_TOOL_ITERATIONS cap lands here, and so does
        # anything a stub raised. Both are results, not crashes.
        record.update(outcome="error", error=f"{type(e).__name__}: {e}")
        final = ""

    record["elapsed_s"] = round(time.monotonic() - t0, 2)
    calls = tool_calls_made(messages)
    record["calls"] = calls
    record["final"] = final
    record["score"] = score_chat(case, calls, final)
    record["prompt_tokens"], record["eval_tokens"] = cap.tokens()
    record["loop_warnings"] = cap.warnings()
    return record


def run_task_case(case: dict, model: str, rep: int, timeout: float) -> dict:
    """One scheduled-task template case through complete_text()."""
    log, cap = _logger_for(case["id"])
    record = {"case": case["id"], "model": model, "rep": rep, "path": "tasks",
              "task": case["task"], "think": case["think"]}
    raw = ""
    t0 = time.monotonic()
    try:
        raw = complete_text(system_prompt=case["system"], user_prompt=case["user"],
                            model=model, logger=log, think=case["think"],
                            timeout=timeout)
        record["outcome"] = "final"
    except OllamaUnavailable as e:
        record.update(outcome="unavailable", error=str(e))
    except Exception as e:
        record.update(outcome="error", error=f"{type(e).__name__}: {e}")

    record["elapsed_s"] = round(time.monotonic() - t0, 2)
    parsed, parse_error = None, None
    try:
        parsed = case["parse"](raw)
    except Exception as e:
        # _parse_classification raises on empty/non-JSON by design; that IS the
        # production behaviour, so record it rather than letting it end the run.
        parse_error = f"{type(e).__name__}: {e}"

    record["raw"] = raw
    record["score"] = score_task(case, raw, parsed, parse_error)
    record["prompt_tokens"], record["eval_tokens"] = cap.tokens()
    record["loop_warnings"] = cap.warnings()
    return record


def run(models: list[str], reps: int, path: str, timeout: float,
        only: list[str] | None = None, out: Path | None = None) -> Path:
    """Run every (model, case, rep) and write the raw records to JSON.

    Writes after each MODEL finishes, not only at the end: a full run is hours
    long, and a wedged runner partway through shouldn't cost the arm that
    already completed."""
    from chat.server import _system_message_content

    chat_cases = [c for c in CHAT_CASES if not only or c["id"] in only]
    task_cases = [c for c in TASK_CASES if not only or c["id"] in only]
    if path == "chat":
        task_cases = []
    elif path == "tasks":
        chat_cases = []

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = out or RESULTS_DIR / f"raw_{datetime.now():%Y%m%d_%H%M%S}.json"
    records: list[dict] = []
    total = len(models) * reps * (len(chat_cases) + len(task_cases))
    done = 0

    for model in models:
        print(f"\n=== {model} — loading ===", flush=True)
        if not warm_model(model=model):
            print(f"  warm_model failed for {model}; running cold", flush=True)
        for rep in range(1, reps + 1):
            # Rebuilt per rep, as chat/server.py rebuilds it per turn.
            system_prompt = _system_message_content()
            for case in chat_cases:
                records.append(run_chat_case(case, model, rep, system_prompt, timeout))
                done += 1
                _progress(records[-1], done, total)
            for case in task_cases:
                records.append(run_task_case(case, model, rep, timeout))
                done += 1
                _progress(records[-1], done, total)
        out.write_text(json.dumps(records, indent=2, default=str))
        print(f"  wrote {len(records)} record(s) -> {out}", flush=True)
        unload(model)

    return out


def _progress(record: dict, done: int, total: int) -> None:
    score = record["score"]
    if record["path"] == "chat":
        flags = [k for k in ("called_expected", "no_tool_expected_ok", "args_ok",
                             "final_ok", "no_fabrication_ok")
                 if score.get(k) is False]
    else:
        flags = [k for k in ("non_empty", "parsed_ok", "complete")
                 if score.get(k) is False]
    mark = "ok " if not flags else "FAIL"
    detail = f" [{', '.join(flags)}]" if flags else ""
    print(f"  [{done}/{total}] {mark} {record['case']} rep{record['rep']} "
          f"{record['elapsed_s']}s{detail}", flush=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", nargs="+", required=True,
                        help="Ollama model tags to compare, e.g. gemma4:26b-mlx")
    parser.add_argument("--reps", type=int, default=3,
                        help="Runs per case. 1 measures nothing about variance.")
    parser.add_argument("--path", choices=["chat", "tasks", "both"], default="both")
    parser.add_argument("--only", nargs="*", help="Run only these case ids.")
    parser.add_argument("--timeout", type=float, default=300.0,
                        help="Per-model-call read timeout, seconds.")
    parser.add_argument("--out", type=Path, help="Write raw JSON here.")
    args = parser.parse_args(argv)

    out = run(args.models, args.reps, args.path, args.timeout, args.only, args.out)
    print(f"\nDone. Raw results: {out}")
    print(f"Score them: .venv/bin/python -m evals.score {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
