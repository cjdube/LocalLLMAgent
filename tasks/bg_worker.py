"""Run one background job per invocation, then exit. Non-interactive — launchd
runs it on a short StartInterval (launchd never runs two copies of the same job
at once, so a long job just delays the next poll rather than overlapping).

Execution posture "A + push-to-approve": the job runs the agent tool loop, but
any tool in toolset.CONSEQUENTIAL_TOOLS pauses the run — the job is saved as
awaiting_approval and Craig gets a tap-to-approve push. The next poll resumes
it once he's decided. Tools in toolset.UNATTENDED_EXCLUDED_TOOLS (memory/skill
writers — prompt-visible state — and the bg-management tools) are stripped from
the toolset entirely, so injected text in content fetched mid-job can't plant a
durable instruction. Everything else (reads, reversible internal writes) runs
unattended.

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
build_and_send_brief = None

# Errors worth retrying: the model runner or network being momentarily away.
# Tool-level errors never surface here (the loop funnels them into error dicts
# the model sees); what does is Ollama itself being unreachable mid-call.
TRANSIENT_EXCEPTIONS = (requests.exceptions.ConnectionError, requests.exceptions.Timeout)
MAX_TRANSIENT_ATTEMPTS = 3

# Re-push a stuck approval once its buttons' tokens have expired (see
# background._TOKEN_MAX_AGE_S) — without this, a missed push strands the job in
# awaiting_approval forever, invisible to next_actionable().
REPUSH_AFTER_S = background._TOKEN_MAX_AGE_S


def _load_agent_stack() -> None:
    """Populate the lazy module globals above. Called only when a job is
    actually going to run; the idle poll never pays the googleapiclient
    import chain."""
    global toolset, advance, resolve, with_identity, build_and_send_brief
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
    if build_and_send_brief is None:
        build_and_send_brief = tasks.morning_brief.build_and_send_brief

BG_SYSTEM_PROMPT = (
    "You are completing a task Craig handed off to run in the background, "
    "unattended — there is no one to ask follow-up questions, so infer what you "
    "need from the task description and your tools and do your best. Use your "
    "tools to actually carry out the task, then end with a short plain-text "
    "summary of what you did or found (this becomes the notification Craig "
    "gets). Consequential actions like sending an email are automatically routed "
    "to Craig for his approval, so go ahead and take them when the task calls "
    "for it — don't refuse or ask first."
)


def _bg_tools_and_dispatch(logger):
    # Tools an unattended run must not carry — bg-management tools plus
    # everything that writes prompt-visible state. Policy and rationale live
    # in toolset.UNATTENDED_EXCLUDED_TOOLS.
    _load_agent_stack()
    excluded = toolset.UNATTENDED_EXCLUDED_TOOLS
    tools = [t for t in toolset.TOOLS if t["function"]["name"] not in excluded]
    dispatch = {k: v for k, v in toolset.DISPATCH.items() if k not in excluded}
    dispatch["send_morning_brief"] = lambda **_: build_and_send_brief(logger=logger)
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
    system = with_identity(BG_SYSTEM_PROMPT + f"\n\nToday's date is {today}.")
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

    result = advance(messages, tools, dispatch, confirm_before=toolset.CONSEQUENTIAL_TOOLS, logger=logger)

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
        tools, dispatch = _bg_tools_and_dispatch(logger)
        _run_job(job, tools, dispatch, logger)
        return 0
    except TRANSIENT_EXCEPTIONS as e:
        # Ollama restarting or a network blip: leave the job actionable so the
        # next poll retries from its last persisted point, up to a bound.
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
