"""Single source of truth for Wren's agent tool registry.

Both the chat server (chat/server.py) and the background worker
(tasks/bg_worker.py) drive agent.loop.advance() over the same tools, so the
schemas (TOOLS), the name->callable map (DISPATCH), and the confirmation sets
(WRITE_TOOLS, CONSEQUENTIAL_TOOLS) live here rather than inside the Flask app —
that keeps a launchd task from having to import the web server.

- WRITE_TOOLS: state-changing tools the *chat* pauses on for Craig's tap.
- CONSEQUENTIAL_TOOLS: the subset that a *background* run must get phone approval
  for — external/irreversible actions only. Reversible internal writes (to
  Craig's own calendar/tasks/reminders) auto-execute unattended; see the Phase 2
  plan for the rationale.
"""

import json
import os
import re

from agent.tools.background import (
    GET_JOB_RESULT_TOOL_SCHEMA,
    LIST_BG_JOBS_TOOL_SCHEMA,
    RUN_IN_BACKGROUND_TOOL_SCHEMA,
    get_job_result,
    list_background_jobs,
    run_in_background,
)
from agent.tools.calendar import (
    GET_BY_DATE_TOOL_SCHEMA as CALENDAR_BY_DATE_SCHEMA,
    LIST_TOOL_SCHEMA as CALENDAR_LIST_SCHEMA,
    LOG_TOOL_SCHEMA as CALENDAR_LOG_SCHEMA,
    RECOLOR_TOOL_SCHEMA as CALENDAR_RECOLOR_SCHEMA,
    get_events_by_date,
    get_upcoming_events,
    log_calendar_event,
    recolor_event,
)
from agent.tools.chrome_history import TOOL_SCHEMA as CHROME_SCHEMA, fetch_chrome_history
from agent.tools.email import TOOL_SCHEMA as EMAIL_SCHEMA, send_email_tool
from agent.tools.github_starred import TOOL_SCHEMA as GITHUB_STARRED_SCHEMA, fetch_starred_repos
from agent.tools.google_tasks import (
    COMPLETE_TASK_TOOL_SCHEMA,
    CREATE_TASK_TOOL_SCHEMA,
    GET_TASKS_DUE_SOON_TOOL_SCHEMA,
    GET_TASKS_TOOL_SCHEMA,
    UPDATE_TASK_DUE_DATE_TOOL_SCHEMA,
    complete_task,
    create_task,
    get_tasks,
    get_tasks_due_soon,
    update_task_due_date,
)
from agent.tools.memory import (
    ARCHIVE_TOOL_SCHEMA,
    FORGET_TOOL_SCHEMA,
    PIN_TOOL_SCHEMA,
    RECALL_TOOL_SCHEMA,
    REMEMBER_TOOL_SCHEMA,
    archive,
    forget,
    pin,
    recall,
    remember,
)
from agent.tools.reminders import (
    CANCEL_REMINDER_TOOL_SCHEMA,
    LIST_REMINDERS_TOOL_SCHEMA,
    SET_REMINDER_TOOL_SCHEMA,
    cancel_reminder,
    list_reminders,
    set_reminder,
)
from agent.tools.skills import (
    SKILL_TOOL_SCHEMAS,
    delete_skill,
    list_skills,
    read_skill,
    write_skill,
)
from agent.tools.strava import TOOL_SCHEMA as STRAVA_SCHEMA, fetch_strava
from agent.tools.weather import TOOL_SCHEMA as WEATHER_SCHEMA, fetch_weather
from agent.tools.web_search import TOOL_SCHEMA as WEB_SEARCH_SCHEMA, search_web
from agent.tools.wiki import (
    WIKI_TOOL_SCHEMAS,
    list_weekly_reviews,
    list_wiki_pages,
    read_weekly_review,
    read_wiki_index,
    read_wiki_page,
)
from tasks.morning_brief import SEND_BRIEF_TOOL_SCHEMA, build_and_send_brief


def _send_morning_brief(**_) -> dict:
    # Default dispatch entry (no logger). The chat server and the background
    # worker each override this with their own logger-bound version so the brief
    # run logs to the right file. Accepts/ignores stray kwargs for the model.
    return build_and_send_brief()


TOOLS = [
    CALENDAR_LIST_SCHEMA,
    CALENDAR_LOG_SCHEMA,
    CALENDAR_BY_DATE_SCHEMA,
    CALENDAR_RECOLOR_SCHEMA,
    CHROME_SCHEMA,
    EMAIL_SCHEMA,
    STRAVA_SCHEMA,
    WEATHER_SCHEMA,
    WEB_SEARCH_SCHEMA,
    GITHUB_STARRED_SCHEMA,
    SEND_BRIEF_TOOL_SCHEMA,
    GET_TASKS_TOOL_SCHEMA,
    GET_TASKS_DUE_SOON_TOOL_SCHEMA,
    CREATE_TASK_TOOL_SCHEMA,
    UPDATE_TASK_DUE_DATE_TOOL_SCHEMA,
    COMPLETE_TASK_TOOL_SCHEMA,
    REMEMBER_TOOL_SCHEMA,
    PIN_TOOL_SCHEMA,
    RECALL_TOOL_SCHEMA,
    ARCHIVE_TOOL_SCHEMA,
    FORGET_TOOL_SCHEMA,
    SET_REMINDER_TOOL_SCHEMA,
    LIST_REMINDERS_TOOL_SCHEMA,
    CANCEL_REMINDER_TOOL_SCHEMA,
    RUN_IN_BACKGROUND_TOOL_SCHEMA,
    LIST_BG_JOBS_TOOL_SCHEMA,
    GET_JOB_RESULT_TOOL_SCHEMA,
    *WIKI_TOOL_SCHEMAS,
    *SKILL_TOOL_SCHEMAS,
]

DISPATCH = {
    "get_upcoming_events": get_upcoming_events,
    "log_calendar_event": log_calendar_event,
    "get_events_by_date": get_events_by_date,
    "recolor_event": recolor_event,
    "fetch_chrome_history": fetch_chrome_history,
    # The wrapper, not send_email itself: it drops model-supplied arguments the
    # schema doesn't declare (to, html), pinning the recipient to BRIEF_TO_EMAIL.
    "send_email": send_email_tool,
    "fetch_strava": fetch_strava,
    "fetch_weather": fetch_weather,
    "search_web": search_web,
    "fetch_starred_repos": fetch_starred_repos,
    "send_morning_brief": _send_morning_brief,
    "get_tasks": get_tasks,
    "get_tasks_due_soon": get_tasks_due_soon,
    "create_task": create_task,
    "update_task_due_date": update_task_due_date,
    "complete_task": complete_task,
    "remember": remember,
    "pin": pin,
    "recall": recall,
    "archive": archive,
    "forget": forget,
    "read_wiki_index": read_wiki_index,
    "list_wiki_pages": list_wiki_pages,
    "read_wiki_page": read_wiki_page,
    "list_weekly_reviews": list_weekly_reviews,
    "read_weekly_review": read_weekly_review,
    "list_skills": list_skills,
    "read_skill": read_skill,
    "write_skill": write_skill,
    "delete_skill": delete_skill,
    "set_reminder": set_reminder,
    "list_reminders": list_reminders,
    "cancel_reminder": cancel_reminder,
    "run_in_background": run_in_background,
    "list_background_jobs": list_background_jobs,
    "get_job_result": get_job_result,
}

WRITE_TOOLS = frozenset({
    "log_calendar_event", "send_email", "recolor_event", "send_morning_brief",
    "create_task", "update_task_due_date", "complete_task", "forget",
    "write_skill", "delete_skill", "set_reminder", "cancel_reminder",
    "run_in_background",
})

# The subset a background run must get phone approval for: external/irreversible
# only. Reversible internal writes (calendar/tasks/reminders on Craig's own
# account) auto-execute unattended. This is the one line to move to tighten the
# policy (e.g. add calendar writes). forget/delete_skill stay listed even though
# UNATTENDED_EXCLUDED_TOOLS below keeps them out of background runs entirely —
# belt-and-braces if that exclusion ever loosens.
CONSEQUENTIAL_TOOLS = frozenset({
    "send_email", "send_morning_brief", "forget", "delete_skill",
})

# Tools an unattended (background) run must not have AT ALL — removed from its
# toolset rather than approval-gated. Background jobs ingest untrusted content
# (web pages, search results, calendar text); tools that write prompt-visible
# state — pinned memories and skills are rendered into every future system
# prompt / steer future procedures — would let injected text plant a durable
# instruction that outlives the job. Approval-gating them would be the wrong
# shape too: a push asking to approve "save this fact" is noise, and background
# tasks have no legitimate need to write memories or skills (the job's summary
# reaches Craig, who can ask chat-Wren to remember things deliberately). The
# read side (recall, list_skills, read_skill) stays available. Also excludes
# the bg-management tools themselves: a job spawning or polling jobs is never
# useful, and run_in_background would let a job replicate.
UNATTENDED_EXCLUDED_TOOLS = frozenset({
    "run_in_background", "list_background_jobs", "get_job_result",
    "remember", "pin", "archive", "forget",
    "write_skill", "delete_skill",
})


# --------------------------------------------------------------------------- #
# Human-readable descriptions of a pending confirm-gated call. Shared by the
# chat confirmation card (chat/server.py) and the background worker's approval
# push (tasks/bg_worker.py) so the two surfaces can't drift apart — they once
# did, on whether the email recipient was shown.
# --------------------------------------------------------------------------- #

# How much of an email body to surface in a confirmation. Long enough to see
# what's being sent, short enough to keep the card/push compact.
BODY_PREVIEW_CHARS = 240


def _email_recipient() -> str:
    # The model-facing send_email pins the recipient (see send_email_tool), so
    # the effective recipient is always BRIEF_TO_EMAIL — show it anyway: the
    # human approving a send should see where it goes, not infer it.
    return os.getenv("BRIEF_TO_EMAIL") or "(BRIEF_TO_EMAIL unset)"


def describe_call(call: dict) -> str:
    """One human line summarizing a tool call awaiting confirmation."""
    name = call["function"]["name"]
    args = call["function"].get("arguments", {}) or {}
    if name == "send_email":
        return f'Send an email to {_email_recipient()} — subject: "{args.get("subject", "")}"'
    if name == "send_morning_brief":
        return "Send the morning brief (weather, calendar, tasks due soon, starred repos)"
    if name == "log_calendar_event":
        return f'Create calendar event "{args.get("summary", "")}" from {args.get("start", "?")} to {args.get("end", "?")}'
    if name == "recolor_event":
        return f'Recolor calendar event to "{args.get("category", "")}"'
    if name == "create_task":
        due = args.get("due")
        return f'Create task "{args.get("title", "")}"' + (f" (due {due})" if due else "")
    if name == "update_task_due_date":
        label = args.get("task_title") or args.get("task_id", "")
        return f'Change due date of "{label}" to {args.get("due", "?")}'
    if name == "complete_task":
        label = args.get("task_title") or args.get("task_id", "")
        return f'Mark "{label}" complete'
    if name == "forget":
        label = args.get("memory_text") or args.get("memory_id", "?")
        return f'Delete memory "{label}"'
    if name == "write_skill":
        return f'Save skill "{args.get("name", "")}"'
    if name == "delete_skill":
        return f'Delete skill "{args.get("name", "")}"'
    return f"{name}({json.dumps(args)})"


def describe_call_detail(call: dict) -> str | None:
    """A secondary preview line for a confirmation. For send_email this is the
    message body, so the human approving the send actually sees what will go
    out — not just the subject. Returns None when there's nothing extra to show
    (the summary alone suffices)."""
    name = call["function"]["name"]
    args = call["function"].get("arguments", {}) or {}
    if name == "send_email":
        body = (args.get("body") or "").strip()
        if not body:
            return None
        # Chat-composed bodies are plain text; strip any stray tags defensively
        # (in case the model emitted HTML) and collapse whitespace so the
        # preview stays readable, then truncate.
        text = re.sub(r"<[^>]+>", "", body)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) > BODY_PREVIEW_CHARS:
            text = text[:BODY_PREVIEW_CHARS].rsplit(" ", 1)[0] + "…"
        return text
    return None
