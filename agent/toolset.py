"""Single source of truth for Wren's agent tool registry.

Both the chat server (chat/server.py) and the background worker
(tasks/bg_worker.py) drive agent.loop.advance() over the same tools, so the
schemas (TOOLS), the name->callable map (DISPATCH), and the confirmation sets
(WRITE_TOOLS, CONSEQUENTIAL_TOOLS) live here rather than inside the Flask app —
that keeps a launchd task from having to import the web server.

- WRITE_TOOLS: state-changing tools the *chat* pauses on for the user's tap.
- CONSEQUENTIAL_TOOLS: the subset that a *background* run must get phone approval
  for — external/irreversible actions only. Reversible internal writes (to
  the user's own calendar/tasks/reminders) auto-execute unattended; see the Phase 2
  plan for the rationale.
"""

import json
import os
import re

from agent import prefs
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
from agent.tools.email import (
    REPLY_TO_THREAD_TOOL_SCHEMA,
    TOOL_SCHEMA as EMAIL_SCHEMA,
    reply_plan,
    reply_to_thread_tool,
    send_email_tool,
)
from agent.tools.evaluate_app import TOOL_SCHEMA as EVALUATE_APP_SCHEMA, evaluate_app
from agent.tools.evaluate_against import TOOL_SCHEMA as EVALUATE_AGAINST_SCHEMA, evaluate_against
from agent.tools.games import TOOL_SCHEMA as GAMES_SCHEMA, list_games
from agent.tools.gmail_read import MAIL_TOOL_SCHEMAS, read_email, search_mail
from agent.tools.github_starred import TOOL_SCHEMA as GITHUB_STARRED_SCHEMA, fetch_starred_repos
from agent.tools.projects import PROJECT_TOOL_SCHEMAS, list_projects, read_project
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
    RECATEGORIZE_TOOL_SCHEMA,
    REMEMBER_TOOL_SCHEMA,
    archive,
    forget,
    pin,
    recall,
    recategorize,
    remember,
)
from agent.tools.nudges import TOOL_SCHEMA as NUDGES_SCHEMA, list_nudges
from agent.tools.opportunities import (
    OPPORTUNITY_TOOL_SCHEMAS,
    list_opportunities,
    unwatch_company,
    update_opportunity,
    watch_company,
)
from agent.tools.push_log import TOOL_SCHEMA as PUSH_LOG_SCHEMA, list_notifications
from agent.tools.research import RESEARCH_TOOL_SCHEMAS, research_company, research_opportunity
from agent.tools.schedule import LIST_SCHEDULED_TASKS_TOOL_SCHEMA, list_scheduled_tasks
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
from agent.tools.sports import TOOL_SCHEMA as SPORTS_SCHEMA, fetch_scores
from agent.tools.strava import TOOL_SCHEMA as STRAVA_SCHEMA, fetch_strava
from agent.tools.weather import TOOL_SCHEMA as WEATHER_SCHEMA, fetch_weather
from agent.tools.web_fetch import TOOL_SCHEMA as WEB_FETCH_SCHEMA, fetch_webpage
from agent.tools.web_search import TOOL_SCHEMA as WEB_SEARCH_SCHEMA, search_web
from agent.tools.wiki import (
    WIKI_TOOL_SCHEMAS,
    read_wiki_page,
    search_wiki,
)
from agent.tools.youtube import TOOL_SCHEMA as YOUTUBE_SCHEMA, fetch_liked_videos
from tasks.morning_brief import SEND_BRIEF_TOOL_SCHEMA, brief_dispatch
from tasks.opportunity_digest import SEND_DIGEST_TOOL_SCHEMA, digest_dispatch

# The user's name, for the model-facing group blurbs and load_tools schema
# below. From config/preferences.json; falls back to "the user".
_NAME = prefs.user_name()


TOOLS = [
    CALENDAR_LIST_SCHEMA,
    CALENDAR_LOG_SCHEMA,
    CALENDAR_BY_DATE_SCHEMA,
    CALENDAR_RECOLOR_SCHEMA,
    CHROME_SCHEMA,
    YOUTUBE_SCHEMA,
    EMAIL_SCHEMA,
    REPLY_TO_THREAD_TOOL_SCHEMA,
    STRAVA_SCHEMA,
    SPORTS_SCHEMA,
    WEATHER_SCHEMA,
    WEB_SEARCH_SCHEMA,
    WEB_FETCH_SCHEMA,
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
    RECATEGORIZE_TOOL_SCHEMA,
    ARCHIVE_TOOL_SCHEMA,
    FORGET_TOOL_SCHEMA,
    SET_REMINDER_TOOL_SCHEMA,
    LIST_REMINDERS_TOOL_SCHEMA,
    CANCEL_REMINDER_TOOL_SCHEMA,
    PUSH_LOG_SCHEMA,
    LIST_SCHEDULED_TASKS_TOOL_SCHEMA,
    RUN_IN_BACKGROUND_TOOL_SCHEMA,
    LIST_BG_JOBS_TOOL_SCHEMA,
    GET_JOB_RESULT_TOOL_SCHEMA,
    *WIKI_TOOL_SCHEMAS,
    *SKILL_TOOL_SCHEMAS,
    *OPPORTUNITY_TOOL_SCHEMAS,
    SEND_DIGEST_TOOL_SCHEMA,
    *RESEARCH_TOOL_SCHEMAS,
    EVALUATE_APP_SCHEMA,
    EVALUATE_AGAINST_SCHEMA,
    GAMES_SCHEMA,
    *PROJECT_TOOL_SCHEMAS,
    NUDGES_SCHEMA,
    *MAIL_TOOL_SCHEMAS,
]

DISPATCH = {
    "get_upcoming_events": get_upcoming_events,
    "log_calendar_event": log_calendar_event,
    "get_events_by_date": get_events_by_date,
    "recolor_event": recolor_event,
    "fetch_chrome_history": fetch_chrome_history,
    "fetch_liked_videos": fetch_liked_videos,
    # The wrapper, not send_email itself: it drops model-supplied arguments the
    # schema doesn't declare (to, html), pinning the recipient to BRIEF_TO_EMAIL.
    "send_email": send_email_tool,
    # Same wrapper reasoning, and one more: this tool has no recipient argument
    # at all. Its recipients come from the thread's own headers in Python
    # (email.reply_plan), so an injected address has nothing to land in.
    "reply_to_thread": reply_to_thread_tool,
    "fetch_strava": fetch_strava,
    # Read-only: one public scoreboard GET per league the user follows.
    "fetch_scores": fetch_scores,
    "fetch_weather": fetch_weather,
    "search_web": search_web,
    "fetch_webpage": fetch_webpage,
    "fetch_starred_repos": fetch_starred_repos,
    "send_morning_brief": brief_dispatch(),
    "get_tasks": get_tasks,
    "get_tasks_due_soon": get_tasks_due_soon,
    "create_task": create_task,
    "update_task_due_date": update_task_due_date,
    "complete_task": complete_task,
    "remember": remember,
    "pin": pin,
    "recall": recall,
    "recategorize": recategorize,
    "archive": archive,
    "forget": forget,
    "search_wiki": search_wiki,
    "read_wiki_page": read_wiki_page,
    "list_skills": list_skills,
    "read_skill": read_skill,
    "write_skill": write_skill,
    "delete_skill": delete_skill,
    "set_reminder": set_reminder,
    "list_reminders": list_reminders,
    "cancel_reminder": cancel_reminder,
    # Read-only: reads the log notify() writes on every delivered push. The
    # counterpart to list_reminders, which only ever sees PENDING reminders —
    # this is the only way to see one that already fired.
    "list_notifications": list_notifications,
    "list_scheduled_tasks": list_scheduled_tasks,
    "run_in_background": run_in_background,
    "list_background_jobs": list_background_jobs,
    "get_job_result": get_job_result,
    "list_opportunities": list_opportunities,
    "update_opportunity": update_opportunity,
    "watch_company": watch_company,
    "unwatch_company": unwatch_company,
    "send_opportunity_digest": digest_dispatch(),
    # Read-only against the outside world (web searches + an internal cached
    # brief), so ungated like search_web.
    "research_opportunity": research_opportunity,
    "research_company": research_company,
    # Read-only like the research tools: fetches + analyzes a page, writes nothing.
    "evaluate_app": evaluate_app,
    # Read-only: loads a wiki lens page + the target, analyzes, writes nothing.
    "evaluate_against": evaluate_against,
    # Read-only: reads the registry and probes each game's service. Writes nothing.
    "list_games": list_games,
    # Read-only: scans the local checkouts and reads the cached registry.
    "list_projects": list_projects,
    "read_project": read_project,
    # Read-only: reads the dated nudge archive daily_synthesis wrote. Writes nothing.
    "list_nudges": list_nudges,
    # Read-only against the mailbox (gmail.readonly). Ungated for the same
    # reason search_web is — but note what they return is untrusted text a
    # stranger wrote, so nothing downstream may treat it as instruction.
    "search_mail": search_mail,
    "read_email": read_email,
}

WRITE_TOOLS = frozenset({
    "log_calendar_event", "send_email", "reply_to_thread", "recolor_event",
    "send_morning_brief",
    "create_task", "update_task_due_date", "complete_task", "forget",
    # remember/pin/recategorize are gated alongside forget: chat turns ingest
    # untrusted web/search content inline, and a pinned fact is injected into
    # every future system prompt (memory.render_memory_block), so an injected
    # "pin that ..." must not write memory without the user's tap. This mirrors the
    # UNATTENDED_EXCLUDED_TOOLS ban on the same tools in background runs.
    # Tradeoff to revisit if the per-save tap gets bothersome: gate these only
    # after a turn has actually pulled untrusted web content, rather than always
    # — more complex, deferred until the friction is felt (README documents this
    # for the user too, under the memory section).
    "remember", "pin", "recategorize",
    "write_skill", "delete_skill", "set_reminder", "cancel_reminder",
    "run_in_background", "update_opportunity", "watch_company",
    "unwatch_company", "send_opportunity_digest",
})

# The subset a background run must get phone approval for: external/irreversible
# only. Reversible internal writes (calendar/tasks/reminders on the user's own
# account) auto-execute unattended. This is the one line to move to tighten the
# policy (e.g. add calendar writes). forget/delete_skill stay listed even though
# UNATTENDED_EXCLUDED_TOOLS below keeps them out of background runs entirely —
# belt-and-braces if that exclusion ever loosens.
CONSEQUENTIAL_TOOLS = frozenset({
    "send_email", "send_morning_brief", "send_opportunity_digest", "forget",
    "delete_skill",
    # The only tool here that mails someone who is not the user. Its recipients
    # cannot be steered (see DISPATCH above), but the words can be, and a reply
    # in his name to a real contact is not something to retract.
    "reply_to_thread",
})

# Tools an unattended (background) run must not have AT ALL — removed from its
# toolset rather than approval-gated. Background jobs ingest untrusted content
# (web pages, search results, calendar text); tools that write prompt-visible
# state — pinned memories and skills are rendered into every future system
# prompt / steer future procedures — would let injected text plant a durable
# instruction that outlives the job. Approval-gating them would be the wrong
# shape too: a push asking to approve "save this fact" is noise, and background
# tasks have no legitimate need to write memories or skills (the job's summary
# reaches the user, who can ask chat-Wren to remember things deliberately). The
# read side (recall, list_skills, read_skill) stays available. Also excludes
# the bg-management tools themselves: a job spawning or polling jobs is never
# useful, and run_in_background would let a job replicate.
UNATTENDED_EXCLUDED_TOOLS = frozenset({
    "run_in_background", "list_background_jobs", "get_job_result",
    "remember", "pin", "recategorize", "archive", "forget",
    "write_skill", "delete_skill",
})

# What a MAIL-originated background job may call without a tap. Everything else
# in TOOLS is gated for those jobs — including tools that do not exist yet.
#
# **Read the direction, it is the whole point.** The obvious spelling is a deny
# list ("these extra tools need a tap on a mail job"), and it rots: every tool
# added later has to be remembered here, and forgetting is silent — the new tool
# just runs unattended on text a stranger emailed in. Spelled as a safe list,
# forgetting costs one extra tap and nothing else, and
# test_every_background_tool_is_classified makes even that hard to forget. This
# is a maintenance property before it is a security one: Wren's tools keep
# growing, and nobody should have to come back to this file per tool to decide
# whether a stranger may drive it.
#
# What earns a place here: a read whose destination is fixed and first-party.
# What does not, and why it is not merely "a read": any tool that puts
# model-chosen text into an outbound request — fetch_webpage, evaluate_app and
# evaluate_against take a url, search_web/research_company take a query — is an
# exfiltration channel, because the mailbox content an injected email wants out
# fits in a URL. Those are gated for mail jobs even though they write nothing.
MAIL_JOB_SAFE_TOOLS = frozenset({
    # The mailbox itself, which is what these jobs are about.
    "search_mail", "read_email",
    # Calendar and tasks, read side.
    "get_events_by_date", "get_upcoming_events", "get_tasks", "get_tasks_due_soon",
    "list_reminders", "list_scheduled_tasks",
    # Wren's own notes and state.
    "recall", "list_skills", "read_skill", "search_wiki", "read_wiki_page",
    "list_notifications", "list_nudges", "list_opportunities",
    "list_projects", "read_project", "list_games",
    # Fixed first-party feeds: the account is the user's and the destination is
    # not a parameter, so an injected email cannot aim them anywhere.
    "fetch_weather", "fetch_scores", "fetch_chrome_history",
    "fetch_liked_videos", "fetch_starred_repos", "fetch_strava",
})


def confirm_set_for(origin: str) -> frozenset:
    """Which tools pause for the user's tap, given where the job came from.

    A chat job was typed by the user, so only CONSEQUENTIAL_TOOLS pause. A mail
    job is driven by text a stranger wrote, so it gates everything not named in
    MAIL_JOB_SAFE_TOOLS — tools registered after this line included. The daemon
    passes an origin, never a tool list: policy that lives in two files drifts."""
    if origin != "mail":
        return CONSEQUENTIAL_TOOLS
    return frozenset(t["function"]["name"] for t in TOOLS) - MAIL_JOB_SAFE_TOOLS


# --------------------------------------------------------------------------- #
# Lazy tool loading (chat only). The full TOOLS list above stays the source of
# truth — the dashboard and the background worker use it whole. Chat sessions,
# though, only send the model a small always-loaded CORE plus whichever GROUPS
# the turn has activated, so the per-turn schema overhead (and the prompt
# narrative describing every tool) doesn't crowd the small model's context on
# the common asks. A group is pulled in two ways: deterministic keyword
# pre-loading in the server (GROUP_KEYWORDS) and, as a fallback, the model
# calling the load_tools meta-tool. See docs/tool-loading.md.
#
# Groups are defined by tool NAME and resolved against TOOLS, so the schema
# objects aren't duplicated and the *_TOOL_SCHEMAS bundles can reorder freely.
# --------------------------------------------------------------------------- #

_BY_NAME = {t["function"]["name"]: t for t in TOOLS}

# The lean always-loaded set. Skills READ (list_skills/read_skill) is core
# because the skills index is rendered into the chat prompt every turn and tells
# the model to read_skill; skill authoring (write/delete) is deferred below.
CORE_TOOL_NAMES = [
    "fetch_weather",
    # Core for the same reason as list_notifications below: "how'd Boston do?"
    # arrives in wording no keyword pre-loader catches, and the cost of the miss
    # is a score invented from pretraining, which reads exactly like a real one.
    "fetch_scores",
    "get_upcoming_events", "get_events_by_date", "log_calendar_event", "recolor_event",
    "get_tasks", "get_tasks_due_soon", "create_task", "update_task_due_date", "complete_task",
    "search_web",
    "remember", "pin", "recall", "recategorize", "archive", "forget",
    "set_reminder", "list_reminders", "cancel_reminder",
    # Core rather than a deferred group, unlike list_nudges: "did you ping me
    # about X?" arrives in wording no keyword list reliably catches, a missed
    # pre-load is silent, and the failure mode of the miss is a fabricated
    # notification rather than a shrug.
    "list_notifications",
    "list_scheduled_tasks",
    "list_skills", "read_skill",
]

# Deferred groups, loadable on demand. Every tool in TOOLS is in exactly one of
# CORE_TOOL_NAMES or a group here (enforced by tests/test_toolset.py) so nothing
# becomes unreachable when a new tool is added.
TOOL_GROUP_NAMES = {
    "opportunities": [
        "list_opportunities", "update_opportunity", "watch_company",
        "unwatch_company", "send_opportunity_digest",
        "research_opportunity", "research_company",
    ],
    "wiki": ["search_wiki", "read_wiki_page"],
    "background": ["run_in_background", "list_background_jobs", "get_job_result"],
    "web": ["fetch_webpage", "evaluate_app", "evaluate_against", "fetch_starred_repos"],
    "activity": ["fetch_strava", "fetch_chrome_history", "fetch_liked_videos"],
    "authoring": ["write_skill", "delete_skill"],
    "brief": ["send_morning_brief", "send_email"],
    "games": ["list_games"],
    "projects": ["list_projects", "read_project"],
    "nudges": ["list_nudges"],
    "mail": ["search_mail", "read_email", "reply_to_thread"],
}

# One-line "when to load it" blurb per group, rendered into the chat prompt so
# the model can reach for load_tools on a keyword the pre-loader missed.
_GROUP_BLURBS = {
    "opportunities": f"{_NAME}'s fractional-work opportunities, watchlist, and company research.",
    "wiki": f"{_NAME}'s learnings wiki — weekly reviews and concept pages.",
    "background": "Hand a long-running task off to run detached and report back.",
    "web": "Fetch a specific web page, evaluate a web app, or list starred GitHub repos.",
    "activity": f"{_NAME}'s Strava activities, recent Chrome browsing history, "
                "and the videos he Liked on YouTube.",
    "authoring": "Save or delete a skill (a reusable multi-step procedure).",
    "brief": "Send the morning brief, or send an email.",
    "games": f"The games {_NAME} can play with you, and the link to open one. "
             "Load this for any ask about playing something — the games that exist "
             "are only the ones the tool lists, never ones you know of.",
    "projects": f"{_NAME}'s software projects — what he has built, what each one is, "
                "and how recently he touched it. Load this for any ask about his "
                "projects, repos, or what he is working on; the projects that exist "
                "are only the ones the tool lists, never ones you know of.",
    "nudges": "The suggestions the daily synthesis has pushed to him — 'you looked "
              "at X, it fits your Y note'. Load this for any ask about what you have "
              "suggested, recommended or noticed; the ones that were sent are only "
              "the ones the tool returns, never ones you recall.",
    "mail": f"{_NAME}'s email — search his mailbox, read a conversation, and "
            "reply on one. His mail is not something you know: only the messages "
            "the tools return exist, and if a search finds nothing, say so.",
}

# Case-insensitive word-boundary cues that pre-load a group before the model
# runs, so it usually never has to make the load_tools reasoning hop. Tunable.
GROUP_KEYWORDS = {
    "opportunities": ["job", "jobs", "role", "opportunit", "hiring", "watchlist",
                      "watch", "fractional", "research", "compan"],
    # Cues match as prefixes after a word boundary, so "learn" is deliberate:
    # "learning" missed "what have I learned about X", the most natural way to
    # ask. The note-taking cues matter more than they look — without them the
    # only way in was to say "wiki" out loud, and asked "what do my notes say
    # about X" Wren searched her memory store, found nothing, and answered that
    # she had nothing written down about a topic with a wiki page on it.
    "wiki": ["wiki", "learn", "weekly review", "working on",
             "notes", "wrote", "written", "write down", "read about"],
    "background": ["background", "kick off", "hand off", "handoff"],
    "web": ["webpage", "web page", "fetch", "url", "evaluate", "starred", "github"],
    # "liked"/"video" rather than "like"/"watch": \blike matches "likely", and
    # "watch" is already an opportunities cue (watchlist).
    "activity": ["strava", "run", "ran", "ride", "cycling", "workout",
                 "browsing", "chrome", "history",
                 "youtube", "liked", "video"],
    "authoring": ["skill"],
    "brief": ["brief", "email"],
    # "play" without "game" catches "let's play something" and "play weigh anchor";
    # the game's own name is a cue too, since naming it is the likeliest way in.
    "games": ["game", "play", "weigh anchor"],
    # "repo" and "built" are the other ways in; "project" covers the direct ask.
    "projects": ["project", "repo", "repos", "codebase", "built", "building",
                 "working on", "stale"],
    # Prefix cues, so "suggest" covers suggested/suggestion and "notice" covers
    # noticed. "suggest"/"recommend" will occasionally load this group for an
    # unrelated ask ("suggest a restaurant") — one extra schema, and the tool's
    # own description keeps it from being called for anything but the archive.
    "nudges": ["nudge", "synthesis", "suggest", "recommend", "connection", "notice"],
    # "email" and "mail" also cue the `brief` group (send_email lives there),
    # so both load — two extra schemas, and each tool's description keeps the
    # read and the send apart. "inbox"/"wrote"/"reply" are the ways in that
    # brief's cues miss.
    "mail": ["email", "mail", "inbox", "wrote to me", "reply", "replied",
             "message from", "hear back", "heard from"],
}

# The meta-tool. Not in TOOLS/DISPATCH — its callable is bound per session in
# chat/server.py (it needs the session id and that turn's live tools list).
LOAD_TOOLS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "load_tools",
        "description": (
            "Load an additional group of tools when the current tools can't do "
            f"what {_NAME} asked. After loading, the group's tools become available "
            "to call on your next step."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "group": {
                    "type": "string",
                    "enum": list(TOOL_GROUP_NAMES),
                    "description": "Which tool group to load.",
                },
            },
            "required": ["group"],
        },
    },
}

CORE_TOOLS = [LOAD_TOOLS_SCHEMA] + [_BY_NAME[n] for n in CORE_TOOL_NAMES]
TOOL_GROUPS = {g: [_BY_NAME[n] for n in names] for g, names in TOOL_GROUP_NAMES.items()}


def tools_for(group_names) -> list[dict]:
    """CORE_TOOLS plus the schemas for each loaded group, de-duped by tool name
    and order-stable (core first, then groups in definition order)."""
    out, seen = [], set()
    for schema in [*CORE_TOOLS, *(s for g in TOOL_GROUPS if g in group_names for s in TOOL_GROUPS[g])]:
        name = schema["function"]["name"]
        if name not in seen:
            seen.add(name)
            out.append(schema)
    return out


def groups_for_message(text: str) -> set[str]:
    """Groups whose keyword cues appear in the user message (word-boundary,
    case-insensitive) — the deterministic pre-load path."""
    low = (text or "").lower()
    hits = set()
    for group, words in GROUP_KEYWORDS.items():
        if any(re.search(rf"\b{re.escape(w)}", low) for w in words):
            hits.add(group)
    return hits


def render_toolgroups_index() -> str:
    """The compact loadable-groups block for the chat system prompt. Replaces
    the per-tool narrative for deferred groups and tells the model it can pull a
    group in with load_tools when the core tools fall short."""
    lines = [
        "Beyond the tools already available to you, these tool GROUPS can be "
        f"loaded on demand with load_tools(group) when {_NAME} asks for something "
        "the current tools can't do:",
    ]
    lines += [f"- {g}: {_GROUP_BLURBS[g]}" for g in TOOL_GROUPS]
    return "\n".join(lines)


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


def _reply_recipients(thread_id: str) -> str:
    """Who a pending reply_to_thread call will actually go to.

    Costs one Gmail read, on the confirmation path only. It has to: this call's
    arguments carry no address at all — the recipients live in the thread's
    headers — and a card that cannot say who the mail goes to is not worth
    tapping. Never raises: a thread that yields no recipients degrades to a line
    saying why, which is still an honest card, rather than losing the
    confirmation. reply_plan's refusals are already written for a person, so
    they are shown as-is — "could not be read" would be a lie on a thread that
    read fine and was refused for being a mailing list."""
    if not (thread_id or "").strip():
        return "(no thread id — this call will fail)"
    try:
        plan = reply_plan(thread_id)
    except Exception:
        return "(the thread could not be read)"
    if "error" in plan:
        return f"(no one — {plan['error']})"
    return ", ".join(plan["to"])


def describe_call(call: dict) -> str:
    """One human line summarizing a tool call awaiting confirmation."""
    name = call["function"]["name"]
    args = call["function"].get("arguments", {}) or {}
    if name == "send_email":
        return f'Send an email to {_email_recipient()} — subject: "{args.get("subject", "")}"'
    if name == "reply_to_thread":
        return f"Reply on the email thread to {_reply_recipients(args.get('thread_id'))}"
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
    if name == "remember":
        return f'Remember "{args.get("text", "")}"'
    if name == "pin":
        return f'Pin memory "{args.get("text", "")}" (kept in mind every conversation)'
    if name == "recategorize":
        return f'Re-file memory {args.get("memory_id", "?")} under "{args.get("category", "")}"'
    if name == "write_skill":
        return f'Save skill "{args.get("name", "")}"'
    if name == "delete_skill":
        return f'Delete skill "{args.get("name", "")}"'
    if name == "send_opportunity_digest":
        return f"Run the opportunity scout and email the digest to {_email_recipient()}"
    if name == "update_opportunity":
        return f'Mark opportunity {args.get("opportunity_id", "?")} as {args.get("status", "?")}'
    if name == "watch_company":
        return (f'Watch "{args.get("company", "")}" for leadership openings '
                f'({args.get("ats", "?")}/{args.get("slug", "?")})')
    if name == "unwatch_company":
        return f'Stop watching "{args.get("watch_id", "")}"'
    if name == "set_reminder":
        return f'Set reminder "{args.get("message", "")}" for {args.get("when", "?")}'
    if name == "cancel_reminder":
        return f'Cancel reminder {args.get("reminder_id", "?")}'
    if name == "run_in_background":
        task = (args.get("task") or "").strip()
        if len(task) > 120:
            task = task[:120].rstrip() + "…"
        return f'Run in the background: "{task}"'
    return f"{name}({json.dumps(args)})"


def _body_preview(body: str) -> str | None:
    """A readable, truncated preview of an outgoing message body."""
    body = (body or "").strip()
    if not body:
        return None
    # Chat-composed bodies are plain text; strip any stray tags defensively
    # (in case the model emitted HTML) and collapse whitespace so the preview
    # stays readable, then truncate.
    text = re.sub(r"<[^>]+>", "", body)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > BODY_PREVIEW_CHARS:
        text = text[:BODY_PREVIEW_CHARS].rsplit(" ", 1)[0] + "…"
    return text


def describe_call_detail(call: dict) -> str | None:
    """A secondary preview line for a confirmation. For the two mail sends this
    is the message body, so the human approving actually sees what will go out —
    not just who it goes to. Returns None when there's nothing extra to show
    (the summary alone suffices)."""
    name = call["function"]["name"]
    args = call["function"].get("arguments", {}) or {}
    if name in ("send_email", "reply_to_thread"):
        return _body_preview(args.get("body"))
    return None
