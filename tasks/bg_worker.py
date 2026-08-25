"""Run one background job per invocation, then exit. Non-interactive — launchd
runs it on a short StartInterval (launchd never runs two copies of the same job
at once, so a long job just delays the next poll rather than overlapping).

Execution posture "A + push-to-approve": the job runs the agent tool loop, but
a gated tool pauses the run — the job is saved as awaiting_approval and the user
gets a tap-to-approve push. The next poll resumes it once he's decided. Tools in
toolset.UNATTENDED_EXCLUDED_TOOLS (memory/skill writers — prompt-visible state —
and the bg-management tools) are stripped from the toolset entirely, so injected
text in content fetched mid-job can't plant a durable instruction.

*Which* tools are gated depends on the job's origin, via
toolset.confirm_set_for(). A chat job was typed by the user, so only
CONSEQUENTIAL_TOOLS pause and everything else (reads, reversible internal
writes) runs unattended. A job whose text came out of an email — origin "mail",
see tasks/mail_watcher.py — is driven by a stranger's words, so it gates
everything outside toolset.MAIL_JOB_SAFE_TOOLS instead.

Reuses agent.loop.advance()/resolve() exactly as chat/server.py does; the only
difference is the decision arrives via a persisted approval, not a live web tap.

Transient errors (Ollama restarting, a network blip) don't terminally fail a
job: the worker leaves it actionable with a bumped attempts counter and the
next poll retries, marking it failed only after MAX_TRANSIENT_ATTEMPTS. A
resolved approval is persisted (mark_resumed) BEFORE the run continues, so a
retry resumes from after the approved call — an approved consequential action
never executes twice.

Crash caveat: a job whose *process* is killed mid-run (not a caught error)
stays in its pre-run status and is retried from its last persisted point, so an
already-executed *auto* side effect (a reversible internal write) could repeat.
Consequential actions are covered by the mark_resumed boundary above.

This module imports the heavy agent stack (toolset -> googleapiclient etc.)
lazily, only when there's a job to run: launchd invokes a fresh interpreter
every StartInterval=30s, and with the imports at module top the idle poll—the
overwhelmingly common case—burned ~1s of CPU each time just importing code it
never used. The idle path needs only the job store and ntfy.

Usage:
    python -m tasks.bg_worker
"""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from agent import prefs
from agent.tools import background
from agent.tools.notify import notify
from tasks._common import notify_failure, setup_logger

# The heavy agent stack, loaded by _load_agent_stack() on first use. Tests
# monkeypatch these module attributes directly; the per-name None checks mean a
# patched name is left alone and only the missing ones get the real import.
toolset = None
advance = None
resolve = None
with_identity = None
brief_dispatch = None

MAX_TRANSIENT_ATTEMPTS = 3


def _transient_exceptions() -> tuple:
    """Errors worth retrying: the model runner or network being momentarily away.
    Tool-level errors never surface here (the loop funnels them into error dicts
    the model sees); what does is the backend itself being unreachable mid-call.

    Ollama raises those through `requests`. With WREN_LLM_BACKEND=gemini — which
    per docs/llm-backend.md routes this worker too — the same class of blip
    arrives instead as a google.genai ServerError (5xx) or an httpx transport
    error, and a tuple naming only the requests classes sends it to the
    mark_failed branch: a job the Ollama path would have retried is terminally
    failed by a hiccup, side effects already executed. ClientError (4xx — bad
    key, malformed request) stays out on purpose; it won't fix itself in 30s.

    Resolved in a function, not at import: an `except` expression is evaluated
    when an exception actually propagates, so the idle poll — the overwhelmingly
    common case this module keeps import-light for — never pays for the cloud
    SDK, and a machine without it installed still works.
    """
    transient = [requests.exceptions.ConnectionError, requests.exceptions.Timeout]
    try:
        import httpx
        from google.genai import errors as genai_errors
    except ImportError:
        return tuple(transient)
    return tuple(transient + [genai_errors.ServerError, httpx.TransportError])


# Re-push a stuck approval once its buttons' tokens have expired (see
# background._TOKEN_MAX_AGE_S) — without this, a missed push strands the job in
# awaiting_approval forever, invisible to next_actionable().
REPUSH_AFTER_S = background._TOKEN_MAX_AGE_S


def _load_agent_stack() -> None:
    """Populate the lazy module globals above. Called only when a job is
    actually going to run; the idle poll never pays the googleapiclient
    import chain."""
    global toolset, advance, resolve, with_identity, brief_dispatch
    import agent.loop
    import agent.toolset
    import tasks.morning_brief
    if toolset is None:
        toolset = agent.toolset
    if advance is None:
        advance = agent.loop.advance
    if resolve is None:
        resolve = agent.loop.resolve
    if with_identity is None:
        with_identity = agent.loop.with_identity
    if brief_dispatch is None:
        brief_dispatch = tasks.morning_brief.brief_dispatch

# The user's name, for the model-facing prompt below. From
# config/preferences.json; falls back to "the user".
_NAME = prefs.user_name()

BG_SYSTEM_PROMPT = (
    f"You are completing a task {_NAME} handed off to run in the background, "
    "unattended — there is no one to ask follow-up questions, so infer what you "
    "need from the task description and your tools and do your best. Use your "
    "tools to actually carry out the task, then end with a short plain-text "
    f"summary of what you did or found (this becomes the notification {_NAME} "
    "gets). Consequential actions like sending an email are automatically routed "
    f"to {_NAME} for their approval, so go ahead and take them when the task calls "
    "for it — don't refuse or ask first.\n\n"
    "You have a limited number of steps. Do not gather background you were not "
    "asked for, and never repeat a tool call you have already made — read what "
    "the task needs, act, and stop."
)


def _make_load_tools(tools: list[dict], excluded: frozenset):
    """load_tools for an unattended run. Same in-place extension the chat server
    does, minus the session: advance() re-sends this same list object on every
    iteration, so an appended schema reaches the model on its very next step."""
    def load_tools(group: str = "", **_) -> dict:
        if group not in toolset.TOOL_GROUPS:
            return {"error": f"unknown group '{group}'",
                    "available": list(toolset.TOOL_GROUPS)}
        have = {t["function"]["name"] for t in tools}
        added = []
        for schema in toolset.TOOL_GROUPS[group]:
            name = schema["function"]["name"]
            if name not in have and name not in excluded:
                tools.append(schema)
                have.add(name)
                added.append(name)
        return {"loaded": group, "now_available": added}

    return load_tools


def _bg_tools_and_dispatch(task_text: str, logger):
    """The tools one job is offered, and how to run them.

    **Selected by keyword, not handed over whole.** A job used to get every
    registered tool — 45 of them — and a small model reads that as a menu. A
    two-line email asking whether he ordered takeout produced ten steps of
    browsing (`fetch_chrome_history`, two `search_wiki` calls) and not one
    action. Chat has never done this: it keyword-loads groups and offers 25 core
    tools (docs/tool-loading.md). This is the same call, so a tool added later
    reaches background jobs by joining a group, with nobody curating a list.

    Keyword selection can miss, so `load_tools` stays available and the prompt
    names the groups: a job that needs the web group asks for it, one step and
    no code change. What is *excluded* is unchanged — that is policy, and it
    still comes from toolset.UNATTENDED_EXCLUDED_TOOLS.
    """
    _load_agent_stack()
    excluded = toolset.UNATTENDED_EXCLUDED_TOOLS
    groups = toolset.groups_for_message(task_text)
    tools = [t for t in toolset.tools_for(groups)
             if t["function"]["name"] not in excluded]
    dispatch = {k: v for k, v in toolset.DISPATCH.items() if k not in excluded}
    dispatch["send_morning_brief"] = brief_dispatch(logger)
    dispatch["load_tools"] = _make_load_tools(tools, excluded)
    if logger:
        logger.info(f"{len(tools)} tools offered"
                    + (f" (groups: {', '.join(sorted(groups))})" if groups else ""))
    return tools, dispatch


def _approval_message(call: dict) -> str:
    """The approval push's text: the same summary line the chat confirmation
    card shows (agent/toolset.py — the recipient included for send_email), plus
    the body preview when there is one, so a phone-only approval sees what will
    actually go out."""
    lines = [f"{toolset.describe_call(call)} — approve?"]
    detail = toolset.describe_call_detail(call)
    if detail:
        lines.append(f'"{detail}"')
    return "\n\n".join(lines)


def _seed_messages(task_text: str) -> list:
    today = datetime.now().strftime("%A, %B %-d, %Y")
    # The groups index, for the same reason chat carries it: the tools offered
    # are now keyword-selected, so the model has to be told what it can pull in
    # when the selection missed. Without this the narrower menu is a dead end.
    system = with_identity(
        BG_SYSTEM_PROMPT
        + f"\n\nToday's date is {today}."
        + f"\n\n{toolset.render_toolgroups_index()}"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": task_text},
    ]


def _run_job(job: dict, tools, dispatch, logger) -> None:
    if job["status"] == "pending":
        if job.get("messages"):
            # A resolved approval (or a transient failure after one) parked the
            # conversation here — resume it rather than re-seeding, so the
            # already-resolved call isn't replayed.
            logger.info(f"continuing job {job['id']} from its persisted conversation")
            messages = job["messages"]
        else:
            logger.info(f"starting job {job['id']}: {job['task_text'][:100]!r}")
            messages = _seed_messages(job["task_text"])
    else:  # approved / denied — resume the paused run
        approved = job["status"] == "approved"
        logger.info(f"resuming job {job['id']} ({'approved' if approved else 'denied'})")
        messages = job["messages"]
        resolve(messages, job["pending_call"], approved, dispatch, logger=logger)
        # Persist the resolved state BEFORE continuing: if the continuation
        # dies transiently and retries, the job must not re-enter the
        # approved/denied branch and execute the consequential call again.
        background.mark_resumed(job["id"], messages)

    # Which tools pause for a tap depends on where the job came from — a job
    # built out of an email gates far more than one the user typed. The policy
    # itself lives in agent/toolset.py; this passes the provenance and no more.
    confirm_before = toolset.confirm_set_for(job.get("origin"))
    result = advance(messages, tools, dispatch, confirm_before=confirm_before,
                     stateful_tools=toolset.WRITE_TOOLS, logger=logger)

    if result["type"] == "confirm":
        call = result["call"]
        approval_message = _approval_message(call)
        background.save_awaiting(job["id"], messages, call, approval_message=approval_message)
        logger.info(f"job {job['id']} awaiting approval for {call['function']['name']}")
        notify(
            message=approval_message,
            title="Wren needs approval",
            priority="high",
            actions=background.approval_actions(job["id"]),
        )
    else:
        background.mark_done(job["id"], result["text"])
        logger.info(f"job {job['id']} done")
        notify(message=result["text"] or "(no summary)", title="Task done")


def _repush_stale_approvals(logger) -> None:
    """Re-send the approval push for jobs stuck awaiting_approval longer than a
    token lifetime — their buttons have expired (or never rendered, if
    WREN_PUBLIC_URL was unset when they paused). Fresh tokens each time;
    touch() resets the staleness clock so each job re-pushes at most once per
    lifetime, not on every 30-second poll. Runs on the idle path, so it uses
    the approval_message persisted at pause time instead of the describer
    stack."""
    for job in background.stale_awaiting(REPUSH_AFTER_S):
        logger.info(f"re-pushing approval for stale job {job['id']}")
        notify(
            message=job.get("approval_message")
            or f"Background job {job['id']} still needs a decision — approve?",
            title="Wren still needs approval",
            priority="high",
            actions=background.approval_actions(job["id"]),
        )
        background.touch(job["id"])


def main() -> int:
    logger = setup_logger("bg_worker")
    job = None
    try:
        job = background.next_actionable()
        if job is None:
            _repush_stale_approvals(logger)
            return 0
        tools, dispatch = _bg_tools_and_dispatch(job["task_text"], logger)
        _run_job(job, tools, dispatch, logger)
        return 0
    except _transient_exceptions() as e:
        # The backend restarting or a network blip: leave the job actionable so
        # the next poll retries from its last persisted point, up to a bound.
        attempts = background.bump_attempts(job["id"]) if job is not None else 0
        if job is not None and attempts < MAX_TRANSIENT_ATTEMPTS:
            logger.warning(
                f"transient error on job {job['id']} "
                f"(attempt {attempts}/{MAX_TRANSIENT_ATTEMPTS}): {e} — will retry next poll"
            )
            return 0
        logger.exception(f"Background worker failed: {e}")
        if job is not None:
            background.mark_failed(job["id"], f"{e} (after {attempts} transient failures)")
        notify_failure("bg_worker", e, logger)
        return 1
    except Exception as e:
        logger.exception(f"Background worker failed: {e}")
        if job is not None:
            background.mark_failed(job["id"], str(e))
        notify_failure("bg_worker", e, logger)
        return 1


if __name__ == "__main__":
    sys.exit(main())
