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

Crash caveat: a job whose *process* is killed mid-run (not a caught error — those
become 'failed') stays in its pre-run status and is retried from its last
persisted point, so an already-executed auto side effect could repeat. Caught
exceptions mark the job failed and never retry.

Usage:
    python -m tasks.bg_worker
"""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import toolset
from agent.loop import advance, resolve, with_identity
from agent.tools import background
from agent.tools.notify import notify
from tasks._common import notify_failure, setup_logger
from tasks.morning_brief import build_and_send_brief

# Tools an unattended run must not carry — bg-management tools plus everything
# that writes prompt-visible state. Policy and rationale live in toolset.py.
BG_EXCLUDE = toolset.UNATTENDED_EXCLUDED_TOOLS

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
    tools = [t for t in toolset.TOOLS if t["function"]["name"] not in BG_EXCLUDE]
    dispatch = {k: v for k, v in toolset.DISPATCH.items() if k not in BG_EXCLUDE}
    dispatch["send_morning_brief"] = lambda **_: build_and_send_brief(logger=logger)
    return tools, dispatch


def _describe_call(call: dict) -> str:
    """A short human line for the approval push describing the pending action."""
    name = call["function"]["name"]
    args = call["function"].get("arguments", {}) or {}
    if name == "send_email":
        return f"Send email to {args.get('to') or 'Craig'}: {args.get('subject', '(no subject)')}"
    if name == "send_morning_brief":
        return "Send the morning brief email"
    if name == "forget":
        return f"Delete memory {args.get('memory_id', '?')}"
    if name == "delete_skill":
        return f"Delete skill {args.get('name', '?')}"
    return f"{name}({', '.join(f'{k}={v}' for k, v in args.items())})"


def _seed_messages(task_text: str) -> list:
    today = datetime.now().strftime("%A, %B %-d, %Y")
    system = with_identity(BG_SYSTEM_PROMPT + f"\n\nToday's date is {today}.")
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": task_text},
    ]


def _run_job(job: dict, tools, dispatch, logger) -> None:
    if job["status"] == "pending":
        logger.info(f"starting job {job['id']}: {job['task_text'][:100]!r}")
        messages = _seed_messages(job["task_text"])
    else:  # approved / denied — resume the paused run
        approved = job["status"] == "approved"
        logger.info(f"resuming job {job['id']} ({'approved' if approved else 'denied'})")
        messages = job["messages"]
        resolve(messages, job["pending_call"], approved, dispatch, logger=logger)

    result = advance(messages, tools, dispatch, confirm_before=toolset.CONSEQUENTIAL_TOOLS, logger=logger)

    if result["type"] == "confirm":
        call = result["call"]
        background.save_awaiting(job["id"], messages, call)
        logger.info(f"job {job['id']} awaiting approval for {call['function']['name']}")
        notify(
            message=f"{_describe_call(call)} — approve?",
            title="Wren needs approval",
            priority="high",
            actions=background.approval_actions(job["id"]),
        )
    else:
        background.mark_done(job["id"], result["text"])
        logger.info(f"job {job['id']} done")
        notify(message=result["text"] or "(no summary)", title="Task done")


def main() -> int:
    logger = setup_logger("bg_worker")
    job = None
    try:
        job = background.next_actionable()
        if job is None:
            return 0
        tools, dispatch = _bg_tools_and_dispatch(logger)
        _run_job(job, tools, dispatch, logger)
        return 0
    except Exception as e:
        logger.exception(f"Background worker failed: {e}")
        if job is not None:
            background.mark_failed(job["id"], str(e))
        notify_failure("bg_worker", e, logger)
        return 1


if __name__ == "__main__":
    sys.exit(main())
