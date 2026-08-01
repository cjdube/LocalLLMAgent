"""Wren — ad hoc chat interface, backed by the same tool-calling loop the
scheduled tasks use. An always-on Flask app (run via launchd, see
launchd/local.wren.wren.plist), meant to be reached only
over Tailscale, never the open internet.

Usage:
    python -m chat.server
"""

import hmac
import json
import logging
import os
import re
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory, session

from agent import prefs
from agent.escalations import record_escalation
from agent.loop import (
    MAX_TOOL_ITERATIONS,
    MAX_TOOL_RESULT_CHARS,
    TurnCancelled,
    active_model_label,
    advance,
    escalation_available,
    escalation_backend,
    load_persona,
    resolve,
    with_identity,
)
from agent.toolset import (
    DISPATCH as _BASE_DISPATCH,
    TOOL_GROUPS,
    TOOLS,
    WRITE_TOOLS,
    describe_call,
    describe_call_detail,
    groups_for_message,
    render_toolgroups_index,
    tools_for,
)
from agent.store import load_json
from agent.tools import background
from agent.tools.github_starred import compare_versions, fetch_starred_repos
from agent.tools.notify import notify
from agent.tools.skills import render_skills_index
from agent.tools.wiki import render_lenses_index
from chat.auth import _authenticated
from chat.login_throttle import LoginThrottle
from chat.routes_dashboard import dashboard_bp
from chat.routes_games import games_bp
from chat.routes_opportunities import opportunities_bp
from tasks import starred_blurbs, starred_installed, starred_releases
from tasks._common import setup_logger
from tasks.morning_brief import brief_dispatch
from tasks.opportunity_digest import digest_dispatch

_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / "config" / ".env")

WREN_CHAT_TOKEN = os.getenv("WREN_CHAT_TOKEN")
FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY")
WREN_CHAT_PORT = int(os.getenv("WREN_CHAT_PORT", "8420"))
# Bind to loopback by default: `tailscale serve` reverse-proxies to 127.0.0.1,
# so binding 0.0.0.0 gains nothing and needlessly exposes the login page to the
# local LAN. Override with WREN_CHAT_HOST only if you know you need a wider bind.
WREN_CHAT_HOST = os.getenv("WREN_CHAT_HOST", "127.0.0.1")
MAX_MESSAGE_CHARS = 8000
# Cap on the conversation history (system message included in the count) that
# gets re-sent to Ollama each turn. ~4 chars/token, so the 16000-char default
# is roughly 4k tokens of the default 8192-token OLLAMA_NUM_CTX — leaving the
# other half for the tool schemas and the current turn's own growth (a user
# message up to MAX_MESSAGE_CHARS plus tool results up to
# OLLAMA_MAX_TOOL_RESULT_CHARS each). Raise together with OLLAMA_NUM_CTX.
MAX_HISTORY_CHARS = int(os.getenv("WREN_CHAT_MAX_HISTORY_CHARS", "16000"))

if not WREN_CHAT_TOKEN or not FLASK_SECRET_KEY:
    raise RuntimeError(
        "WREN_CHAT_TOKEN and FLASK_SECRET_KEY must both be set in config/.env "
        "before running the chat server — without them the login check can't "
        "run safely."
    )

logger = setup_logger("wren")
logger.setLevel(logging.INFO)

# TOOLS and WRITE_TOOLS come straight from the shared registry; only the brief
# and digest dispatches are overridden to bind the server's "wren" logger (via
# the shared factories) so a chat-triggered run logs to the right file rather
# than agent.toolset's handler-less default.
DISPATCH = {
    **_BASE_DISPATCH,
    "send_morning_brief": brief_dispatch(logger),
    "send_opportunity_digest": digest_dispatch(logger),
}

# The user's name, for the model-facing prompt below. From
# config/preferences.json; falls back to "the user".
_NAME = prefs.user_name()

CHAT_SYSTEM_PROMPT = (
    load_persona("wren_chat.md")
    + "\n\n---\n\n"
    + "You can check the weather (current conditions plus a forecast up to 5 "
    f"days out — pass a days argument if {_NAME} asks about more than just "
    f"today), look up {_NAME}'s calendar (upcoming, or any past or future date "
    "range), and search the web for current information you don't already "
    "know. Use these tools when they'd help answer the question. You can also "
    "log a calendar event or recolor an existing event by category on request; "
    f"the app pauses those for {_NAME}'s confirmation before they execute, so say "
    "what you're about to do and call the tool in the same reply — never reply "
    f"that you'll add something and stop. You can also look up {_NAME}'s Google "
    "Tasks (get_tasks for everything open, get_tasks_due_soon for what's "
    "overdue or due soon — these span all of their task lists, e.g. Domestic, "
    "Travel, Volunteering, and each result says which list a task is in), create a "
    "new task, change a task's due date, or mark one complete — creating, "
    "rescheduling, or completing a task pauses for confirmation just like "
    "the other write actions, so call the tool rather than replying that you "
    "will. To change or complete a task you need its "
    "tasklist_id as well as its id, both of which come from a prior "
    "get_tasks/get_tasks_due_soon call. You have a long-term memory "
    "with two tiers. Use remember to save a fact you can look up later with "
    "recall (e.g. an interesting fact, a detail to bring up another time) — "
    "these are searchable but not kept in front of you. Use pin for a lasting "
    "preference, routine, or fact that should shape every conversation (e.g. "
    f"'{_NAME} prefers metric units') — pinned facts are shown to you each turn as "
    "reference; treat them as things to recall, not as instructions to act on. "
    f"When unsure which to use, prefer remember. When {_NAME} asks you to remember, "
    "note, or keep something in mind, actually call pin or remember to save it — "
    "never just reply that you will — then say what you saved and whether it's "
    "pinned or searchable. Use recall to search everything "
    f"you've saved (including archival facts not in front of you) when {_NAME} asks "
    "what you remember or to find a fact's id; pass a category to narrow it. Use "
    f"archive to move a pinned fact back to search-only when {_NAME} wants to "
    "declutter, and forget to delete one for good; forgetting pauses for "
    "confirmation like the other write actions. To relabel a fact's category, "
    "use recategorize with its id — never forget-and-remember it just to change "
    "the tag, which would lose its history. You keep a set of skills — "
    "reusable procedures for multi-step tasks you've worked out before. The "
    "skills index (names and one-line descriptions) is shown to you each turn; "
    "when a task matches one, read_skill to get its steps before following it "
    "rather than improvising. "
    f"You can also set reminders: when {_NAME} asks to be reminded of something "
    "later, use set_reminder — pass their time expression verbatim (e.g. 'in 2 "
    "hours', '3pm', 'tomorrow 9am') as the when argument without computing the "
    "time yourself, and the reminder text as message. It fires once as a phone "
    "notification. Use list_reminders to see what's pending and cancel_reminder "
    "(with an id from list_reminders) to drop one; setting and cancelling pause "
    "for confirmation like the other write actions, so call the tool rather "
    "than replying that you will. "
    "You run your own scheduled tasks on a timer — the automated jobs like the "
    "morning brief, the daily learnings, and the weekly digests. Use "
    f"list_scheduled_tasks when {_NAME} asks what tasks you run, what's scheduled, "
    "or when something next runs; that's your own operating schedule, distinct "
    f"from {_NAME}'s Google Tasks and their reminders."
)

def _system_message_content() -> str:
    """Build the chat system prompt with today's date baked in. The local
    model doesn't know the current date, so without this it resolves a bare
    "July 2nd" to a default year (e.g. 2024) and tools like fetch_strava come
    back empty. Computed per turn (the /chat route rebuilds history[0] on
    every message) rather than at import time or per conversation: the server
    is long-running under launchd, so the date would otherwise go stale after
    midnight, and a fact pinned or skill saved mid-session would otherwise
    only take effect in the NEXT conversation."""
    today = datetime.now().strftime("%A, %B %-d, %Y")
    dated = (
        CHAT_SYSTEM_PROMPT
        + f"\n\nToday's date is {today}. When {_NAME} names a date without a year "
        "(e.g. 'July 2nd') or a relative day, resolve it against today's date — "
        "never guess the year."
    )
    # Skills are chat-only (like the wiki tools), so the index lives here rather
    # than in with_identity() where the scheduled tasks would also carry it.
    # Rendered per turn so a skill saved mid-session shows up on the next turn.
    skills_index = render_skills_index(logger)
    if skills_index:
        dated += "\n\n" + skills_index
    # The evaluation-lenses index: which wiki pages are the user's standards rubrics,
    # so the model passes the right lens_page to evaluate_against instead of
    # guessing a slug. Rendered per turn so a lens added mid-session shows up next
    # turn (same as the skills index).
    lenses_index = render_lenses_index(logger)
    if lenses_index:
        dated += "\n\n" + lenses_index
    # The loadable tool-group index: the deferred groups' schemas aren't sent
    # every turn, so this tells the model what it can pull in with load_tools.
    dated += "\n\n" + render_toolgroups_index()
    return with_identity(dated)


STATIC_DIR = Path(__file__).resolve().parent / "static"

app = Flask(__name__)
app.secret_key = FLASK_SECRET_KEY
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
# Reject oversized request bodies outright (a chat turn is small).
app.config["MAX_CONTENT_LENGTH"] = 256 * 1024
# Reached only via the HTTPS URL tailscale serve provides (see README) — the
# session cookie is now rejected by the browser over a plain http:// origin.
app.config["SESSION_COOKIE_SECURE"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

# The read-only dashboard/scheduler API, the opportunities triage API, and the
# games surface live in their own blueprint modules (see chat/routes_dashboard.py,
# chat/routes_opportunities.py, chat/routes_games.py); the conversation engine
# and auth stay here.
app.register_blueprint(dashboard_bp)
app.register_blueprint(opportunities_bp)
app.register_blueprint(games_bp)

@app.after_request
def _security_headers(resp):
    """Defense-in-depth response headers on the one network-adjacent surface.
    The pages are already XSS-safe by construction (all model/log-derived text
    is assigned via textContent, never innerHTML), but these harden against
    clickjacking, MIME sniffing, referrer leakage, and any future markup slip.

    The CSP still allows 'unsafe-inline' for style/script because the chat and
    dashboard pages carry their logic in inline <style>/<script> blocks; the
    'self' + inline policy plus frame-ancestors 'none' is the meaningful win.
    Tightening to nonces/hashes would mean moving that JS into /static — a
    larger change deferred for now."""
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; "
        "form-action 'self'",
    )
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    return resp


# In-memory only, per the "fresh session" design — lost on server restart.
conversations: dict[str, list[dict]] = {}
pending_confirmations: dict[str, dict] = {}
# When a write paused for confirmation was reached during an escalated (frontier)
# turn, this holds that turn's backend so the /chat/confirm continuation stays on
# the frontier model rather than silently dropping back to the local one. Keyed
# by sid, set only for escalated turns, popped when the confirmation is consumed.
pending_backends: dict[str, str] = {}
# Which deferred tool groups each session has activated (via keyword pre-load or
# a load_tools call). Persists across turns so a group loaded once stays loaded
# — including across the /chat/confirm continuation, which rebuilds the toolset.
loaded_groups: dict[str, set[str]] = {}
# A per-session cancel signal for the turn currently running in another request
# thread. /chat and /chat/confirm register a fresh Event (via _begin_turn)
# before advancing and hand advance() its .is_set; /chat/cancel sets it. Keyed
# by sid, cleared when the turn ends. (Flask runs with threaded=True, so the
# cancelling request and the blocked turn are different threads sharing this
# dict.) Membership doubles as the "a turn is running" flag: a second /chat for
# the same sid gets 409 instead of interleaving into the same history.
cancel_events: dict[str, threading.Event] = {}
_turn_registry_lock = threading.Lock()

# Sessions idle past this are evicted on the next /chat from anyone — the
# server runs for months under launchd, and every device/re-login mints a new
# sid whose history would otherwise sit in RAM until restart.
SESSION_IDLE_EVICT_S = 24 * 3600
_session_last_active: dict[str, float] = {}


def _begin_turn(sid: str) -> threading.Event | None:
    """Register this request as the session's one running turn and return its
    cancel Event, or None if a turn is already running (caller answers 409).
    Registration is atomic under a lock: two concurrent requests for one sid
    would otherwise interleave appends into the same history mid-advance() and
    clobber each other's cancel Event."""
    with _turn_registry_lock:
        if sid in cancel_events:
            return None
        event = cancel_events[sid] = threading.Event()
        return event


def _evict_idle_sessions() -> None:
    cutoff = time.time() - SESSION_IDLE_EVICT_S
    for sid in [s for s, t in _session_last_active.items() if t < cutoff]:
        if sid in cancel_events:
            continue  # a turn is somehow still running; leave it alone
        conversations.pop(sid, None)
        pending_confirmations.pop(sid, None)
        pending_backends.pop(sid, None)
        loaded_groups.pop(sid, None)
        _session_last_active.pop(sid, None)

LOGIN_PAGE = """<!DOCTYPE html>
<html><head><title>Wren</title><meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" type="image/svg+xml" href="/static/favicon.svg">
<link rel="apple-touch-icon" href="/static/apple-touch-icon.png">
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; max-width: 400px; margin: 100px auto; padding: 0 24px; color: #1f2328; }}
h2 {{ margin-bottom: 4px; }}
input {{ width: 100%; padding: 12px; font-size: 16px; margin: 12px 0; box-sizing: border-box; border: 1px solid #d0d5dd; border-radius: 8px; }}
button {{ width: 100%; padding: 12px; font-size: 16px; background: #1f2328; color: white; border: none; border-radius: 8px; }}
.error {{ color: #c0392b; font-size: 14px; }}
</style></head>
<body><h2>Wren</h2>
{error}
<form method="post" action="/login">
<input type="password" name="token" placeholder="Access token" autofocus>
<button type="submit">Enter</button>
</form></body></html>"""


def _client_ip() -> str:
    """The login throttle's key. `tailscale serve` reverse-proxies from
    loopback and records the real client in X-Forwarded-For, so the header is
    honored only when the peer IS loopback — a direct (non-proxied) client
    could otherwise rotate a spoofed XFF value per attempt and dodge the
    lockout entirely. The LAST entry is used because that's the hop appended
    by the nearest (trusted) proxy; earlier entries are client-supplied."""
    remote = request.remote_addr or "unknown"
    if remote not in ("127.0.0.1", "::1"):
        return remote
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[-1].strip()
    return remote


# Rate-limits failed /login attempts per client (defense-in-depth; see class).
login_throttle = LoginThrottle()


def _session_id() -> str:
    if "sid" not in session:
        session["sid"] = str(uuid.uuid4())
    return session["sid"]


def _call_response(result: dict) -> dict:
    if result["type"] == "final":
        return {"type": "final", "text": result["text"]}
    call = result["call"]
    return {
        "type": "confirm",
        "tool": call["function"]["name"],
        "args": call["function"].get("arguments", {}),
        # Shared with the background worker's approval push (agent/toolset.py)
        # so the two confirmation surfaces describe a call identically.
        "summary": describe_call(call),
        "detail": describe_call_detail(call),
    }


def _message_chars(msg: dict) -> int:
    """Approximate prompt cost of one history message in characters — content
    plus any tool_calls payload (assistant tool-call turns often have empty
    content but big arguments)."""
    n = len(msg.get("content") or "")
    for call in msg.get("tool_calls") or []:
        n += len(json.dumps(call))
    return n


def _trim_history(history: list) -> int:
    """Drop the oldest whole user-turns (a user message and everything up to
    the next user message) until the history fits MAX_HISTORY_CHARS. The
    system message (index 0) and the most recent turn always survive. Returns
    how many messages were dropped.

    Without this the history grows without bound: every turn re-sends all of
    it, so prefill latency climbs with session length, and once the prompt
    exceeds num_ctx Ollama silently truncates the FRONT — the system prompt —
    after which the model typically degrades into repetition loops (loop.py
    logs a warning when that happens; this keeps it from happening). Dropping
    whole turns keeps every assistant tool_call adjacent to its tool results,
    so the model never sees an orphaned half of a pair."""
    total = sum(_message_chars(m) for m in history)
    if total <= MAX_HISTORY_CHARS:
        return 0
    starts = [i for i, m in enumerate(history) if m.get("role") == "user"]
    dropped = 0
    while total > MAX_HISTORY_CHARS and len(starts) > 1:
        start, end = starts[0], starts[1]
        total -= sum(_message_chars(m) for m in history[start:end])
        del history[start:end]
        dropped += end - start
        starts = [i - (end - start) for i in starts[1:]]
    return dropped


def _make_load_tools(sid: str, tools: list[dict]):
    """Bind the load_tools meta-tool to this turn: record the group on the
    session (so it survives later turns and the confirm continuation) and extend
    THIS turn's live tools list in place — advance() re-sends the same list each
    iteration, so the group's schemas reach the model on its very next step."""
    def load_tools(group: str = "", **_) -> dict:
        if group not in TOOL_GROUPS:
            return {"error": f"unknown group '{group}'", "available": list(TOOL_GROUPS)}
        loaded_groups.setdefault(sid, set()).add(group)
        have = {t["function"]["name"] for t in tools}
        added = []
        for schema in TOOL_GROUPS[group]:
            name = schema["function"]["name"]
            if name not in have:
                tools.append(schema)
                have.add(name)
                added.append(name)
        return {"loaded": group, "now_available": added}

    return load_tools


# First-person future-tense openers followed by an action verb — how the model
# phrases a write it is about to make. Deliberately narrow: the verb must follow
# the opener, so "I'll need the tasklist_id first" and "let me know" don't match.
_PROMISE_RE = re.compile(
    r"\b(?:i['’]ll|i will|i['’]m going to|i am going to|let me)\s+"
    r"(?:go ahead and\s+|now\s+|just\s+)?"
    r"(?:add|create|send|log|set|schedule|book|update|reschedule|save|remember|"
    r"pin|delete|remove|cancel|mark|complete|archive|forget|recolor|write|put)\b",
    re.IGNORECASE,
)


def _warn_if_promised_without_acting(stage: str, text: str, history: list, checkpoint: int) -> None:
    """Log when a turn ends by *saying* it will perform a write while having
    executed no tool at all.

    This is the miss the local model actually produces. Asked to log a fetched
    Strava activity to the calendar it replied "I'll add "Evening Volleyball" to
    your calendar for yesterday, July 31, from 6:38 PM to 9:13 PM." and emitted
    no tool_call, so advance() ended the turn with nothing gated and nothing
    written (logs/wren.log, 2026-08-01 06:06). Such a reply is shaped exactly
    like a legitimate conversational answer, so the turn left no trace of having
    dropped the request — it surfaced only because the user asked a second time
    five minutes later.

    Per CLAUDE.md, a turn that silently does *less* is worse than one that
    fails, because only the failure is visible. So flag it; the prompt (see
    agent/wren_chat.md) is what's meant to prevent it. We deliberately don't
    auto-retry: re-prompting for the tool call would re-drive a write the user
    may have moved on from, and the honest signal is that the model ignored an
    instruction, not that the turn needs another lap."""
    if not text or not _PROMISE_RE.search(text):
        return
    if any(m.get("role") == "tool" for m in history[checkpoint:]):
        return
    logger.warning(
        "chat %s promised an action but executed no tool — the model narrated a "
        "write instead of calling it: %r", stage, text[:200],
    )


def _run_turn(sid: str, history: list, checkpoint: int, cancel: threading.Event,
              stage: str = "turn", backend: str | None = None):
    """Advance the session's conversation and shape the HTTP response — the
    shared back half of /chat and /chat/confirm. On cancel or failure the
    history is rolled back to `checkpoint` so the next turn starts clean; for
    /chat/confirm the checkpoint sits after the resolved tool result, which
    therefore survives the rollback (see that route's comment). `cancel` is
    the Event _begin_turn registered for this sid; it is always deregistered
    on the way out, which also releases the session's one-turn slot.

    `backend` is None for a normal (local) turn and set only when a /chat/confirm
    continues an escalated turn — so the frontier turn's continuation stays on
    the frontier model."""
    # Chat sends only the always-loaded core plus this session's activated
    # groups, not the whole registry — keeps the small model's context lean. The
    # tools list is mutable so a mid-turn load_tools call can extend it (see
    # _make_load_tools); dispatch carries every real tool, gated only in schema.
    tools = tools_for(loaded_groups.get(sid, set()))
    dispatch = {**DISPATCH, "load_tools": _make_load_tools(sid, tools)}
    # Logged before advance(), not after: every other per-turn line (the
    # access log, ollama_chat) is written once the turn completes, so a turn
    # that never arrives and one that hangs mid-flight looked identical — both
    # simply absent. This line is the "the request got here" marker.
    logger.info(f"chat {stage} start: {len(history)} messages, {len(tools)} tools")
    # Only pass backend when set: a normal turn omits it so advance() applies its
    # own local default (and existing test doubles that don't accept the kwarg
    # keep working).
    backend_kwargs = {"backend": backend} if backend else {}
    try:
        result = advance(history, tools, dispatch, confirm_before=WRITE_TOOLS,
                         logger=logger, should_cancel=cancel.is_set, **backend_kwargs)
    except TurnCancelled:
        del history[checkpoint:]  # discard the stopped turn so the next one starts clean
        logger.info(f"chat {stage} cancelled by user")
        return jsonify({"type": "cancelled"})
    except Exception as e:
        del history[checkpoint:]  # roll back the failed turn so the next one starts clean
        logger.exception(f"chat {stage} failed: {e}")
        return jsonify({"error": str(e)}), 500
    finally:
        cancel_events.pop(sid, None)

    if result["type"] == "confirm":
        pending_confirmations[sid] = result["call"]
        # A frontier turn that pauses for a second confirm keeps its backend, so
        # the next continuation stays on the frontier model too.
        if backend:
            pending_backends[sid] = backend
        return jsonify(_call_response(result))

    resp = _call_response(result)
    if result["type"] == "final":
        _warn_if_promised_without_acting(stage, result.get("text", ""), history, checkpoint)
        if backend:
            # An escalated turn continued through a confirmation: badge its final.
            resp["escalated"] = True
            resp["model_label"] = active_model_label(backend)
        elif escalation_available():
            # A local reply: tell the client it can be redone on the frontier
            # model, so the dock can offer the escalation button on this message.
            resp["escalate_to"] = active_model_label(escalation_backend())
    return jsonify(resp)


@app.route("/", methods=["GET"])
def index():
    if not _authenticated():
        return LOGIN_PAGE.format(error="")
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/login", methods=["POST"])
def login():
    client = _client_ip()
    wait = login_throttle.retry_after(client)
    if wait > 0:
        logger.warning(f"login throttled for {client}, retry after {int(wait)}s")
        page = LOGIN_PAGE.format(error='<p class="error">Too many attempts. Try again shortly.</p>')
        return page, 429, {"Retry-After": str(int(wait) + 1)}

    token = request.form.get("token", "")
    if hmac.compare_digest(token, WREN_CHAT_TOKEN):
        login_throttle.record_success(client)
        session.permanent = True
        session["authenticated"] = True
        _session_id()
        return index()
    login_throttle.record_failure(client)
    return LOGIN_PAGE.format(error='<p class="error">Wrong token.</p>'), 401


@app.route("/chat", methods=["POST"])
def chat():
    if not _authenticated():
        return jsonify({"error": "not authenticated"}), 401

    user_message = (request.get_json() or {}).get("message", "").strip()
    if not user_message:
        return jsonify({"error": "empty message"}), 400
    if len(user_message) > MAX_MESSAGE_CHARS:
        return jsonify({"error": "message too long"}), 400

    sid = _session_id()
    cancel = _begin_turn(sid)
    if cancel is None:
        return jsonify({"error": "a turn is already running for this session"}), 409

    _evict_idle_sessions()
    _session_last_active[sid] = time.time()
    history = conversations.setdefault(sid, [])

    # If a write action was awaiting confirmation and the user sent a new
    # message instead of answering, treat it as declining that action —
    # otherwise its unanswered tool_call would leave the history malformed.
    pending = pending_confirmations.pop(sid, None)
    if pending is not None:
        pending_backends.pop(sid, None)
        resolve(history, pending, False, DISPATCH, logger=logger)

    # (Re)build the system message every turn, not just on session start, so a
    # fact pinned or a skill saved mid-session takes effect on the very next
    # turn, and the baked-in date rolls over at midnight in a long-lived session.
    system_message = {"role": "system", "content": _system_message_content()}
    if history:
        history[0] = system_message
    else:
        history.append(system_message)

    # Trim before taking the checkpoint — trimming shifts indices, and
    # _run_turn's rollback slices from the checkpoint.
    trimmed = _trim_history(history)
    if trimmed:
        logger.info(
            f"trimmed {trimmed} oldest history messages to fit the context budget "
            f"({MAX_HISTORY_CHARS} chars)"
        )

    # Deterministic pre-load: attach any tool groups this message's keywords cue
    # so the model usually doesn't have to make the load_tools reasoning hop.
    preload = groups_for_message(user_message)
    if preload:
        loaded_groups.setdefault(sid, set()).update(preload)

    checkpoint = len(history)
    history.append({"role": "user", "content": user_message})
    return _run_turn(sid, history, checkpoint, cancel)


@app.route("/chat/confirm", methods=["POST"])
def chat_confirm():
    if not _authenticated():
        return jsonify({"error": "not authenticated"}), 401

    approved = bool((request.get_json() or {}).get("approved"))
    sid = _session_id()
    cancel = _begin_turn(sid)
    if cancel is None:
        return jsonify({"error": "a turn is already running for this session"}), 409

    call = pending_confirmations.pop(sid, None)
    if call is None:
        cancel_events.pop(sid, None)  # release the turn slot taken above
        return jsonify({"error": "no pending action"}), 400
    # Set only when the paused write belonged to an escalated turn; continue that
    # turn on the same frontier backend rather than dropping back to local.
    backend = pending_backends.pop(sid, None)

    _session_last_active[sid] = time.time()
    history = conversations.setdefault(sid, [])
    # Resolve first (this answers the paused tool_call), then checkpoint — so a
    # rollback in _run_turn never strips the tool result and re-orphans that
    # call. No trim here: the in-flight turn is part of the newest user-turn,
    # which _trim_history always keeps; the next /chat trims.
    resolve(history, call, approved, DISPATCH, logger=logger)

    checkpoint = len(history)
    return _run_turn(sid, history, checkpoint, cancel, stage="continuation", backend=backend)


def _last_user_index(history: list) -> int | None:
    """Index of the most recent user message, or None if there isn't one — the
    turn an escalation re-runs."""
    for i in range(len(history) - 1, -1, -1):
        if history[i].get("role") == "user":
            return i
    return None


def _local_reply_text(history: list, after: int) -> str:
    """The local model's reply to the user turn at `after` — the assistant
    content following it, joined. Captured only for the escalation log (the
    paired 'weak local answer' half of the dataset)."""
    parts = [m.get("content") or "" for m in history[after + 1:]
             if m.get("role") == "assistant"]
    return "\n".join(p for p in parts if p).strip()


@app.route("/chat/escalate", methods=["POST"])
def chat_escalate():
    """Re-run the last turn on the configured frontier backend — the manual
    "redo with the frontier model" button. The user is the router: this fires only
    on a deliberate tap, ships the current conversation off-device, logs the
    escalation, and badges the reply. See docs/frontier-escalation.md.

    The frontier turn runs on a COPY of the history truncated to the last user
    request, so a failed escalation leaves the local answer intact — on screen
    and as context for the next turn. Only a success commits the new history."""
    if not _authenticated():
        return jsonify({"error": "not authenticated"}), 401
    backend = escalation_backend()
    if not escalation_available():
        return jsonify({"error": "no frontier backend is configured for escalation"}), 400

    sid = _session_id()
    cancel = _begin_turn(sid)
    if cancel is None:
        return jsonify({"error": "a turn is already running for this session"}), 409

    history = conversations.get(sid, [])
    # A write awaiting confirmation would leave the history mid-tool_call; decline
    # it first (as /chat does for a new message) so the re-run starts well-formed.
    pending = pending_confirmations.pop(sid, None)
    if pending is not None:
        pending_backends.pop(sid, None)
        resolve(history, pending, False, DISPATCH, logger=logger)

    last_user = _last_user_index(history)
    if last_user is None:
        cancel_events.pop(sid, None)  # release the turn slot taken above
        return jsonify({"error": "nothing to redo yet"}), 400

    _session_last_active[sid] = time.time()
    request_text = history[last_user].get("content") or ""
    local_reply = _local_reply_text(history, last_user)
    # Work on a copy truncated to the user request — advance() appends onto this,
    # never the live history, so a failure below can't disturb the local answer.
    working = list(history[: last_user + 1])
    # Approximate size shipped off-device, for the log (deterministic char/4
    # estimate — the provider's own token count isn't returned to us here).
    prompt_tokens = sum(_message_chars(m) for m in working) // _CHARS_PER_TOKEN
    label = active_model_label(backend)

    tools = tools_for(loaded_groups.get(sid, set()))
    dispatch = {**DISPATCH, "load_tools": _make_load_tools(sid, tools)}
    logger.info("chat escalate start: backend=%s ~%d prompt tokens, %d tools",
                backend, prompt_tokens, len(tools))
    try:
        result = advance(working, tools, dispatch, confirm_before=WRITE_TOOLS,
                         backend=backend, logger=logger, should_cancel=cancel.is_set)
    except TurnCancelled:
        logger.info("chat escalate cancelled by user")
        return jsonify({"type": "cancelled"})  # working discarded; local answer intact
    except Exception as e:
        logger.exception(f"chat escalate failed: {e}")
        record_escalation(request=request_text, local_reply=local_reply,
                          prompt_tokens=prompt_tokens, backend=backend, model=label,
                          outcome=f"error:{e}")
        # 502: the local answer is untouched on screen — surface the failure, no
        # silent fallback to the reply the user just judged too weak.
        return jsonify({"error": f"the frontier model couldn't be reached ({e}). "
                                 "Your local answer is unchanged."}), 502
    finally:
        cancel_events.pop(sid, None)

    # Success (a final answer, or a write paused for confirmation): commit the
    # frontier history and log the escalation.
    conversations[sid] = working
    record_escalation(request=request_text, local_reply=local_reply,
                      prompt_tokens=prompt_tokens, backend=backend, model=label,
                      outcome="ok")
    if result["type"] == "confirm":
        pending_confirmations[sid] = result["call"]
        pending_backends[sid] = backend  # continue on the frontier after confirm
        return jsonify(_call_response(result))
    resp = _call_response(result)
    resp["escalated"] = True
    resp["model_label"] = label
    return jsonify(resp)


@app.route("/chat/cancel", methods=["POST"])
def chat_cancel():
    """Stop the turn currently running for this session. Sets the session's
    cancel Event; the turn's advance() sees it between model chunks, raises
    TurnCancelled, and its handler returns {"type": "cancelled"}. A no-op if
    nothing is running. `cancelling` is False when there was no active turn."""
    if not _authenticated():
        return jsonify({"error": "not authenticated"}), 401
    event = cancel_events.get(_session_id())
    if event is not None:
        event.set()
    return jsonify({"cancelling": event is not None})


@app.route("/chat/new", methods=["POST"])
def chat_new():
    if not _authenticated():
        return jsonify({"error": "not authenticated"}), 401
    sid = _session_id()
    # Cancel any turn still running for this sid before clearing its state.
    # Without this the orphaned turn keeps its reference to the old history and
    # runs to completion: it can still park a pending_confirmation on what the
    # user now sees as a fresh session (a confirm card for a request whose
    # context is gone), and it holds the sid's one turn slot, so the next /chat
    # gets 409'd until it drains. The turn's own handler pops cancel_events.
    event = cancel_events.get(sid)
    if event is not None:
        event.set()
    conversations.pop(sid, None)
    pending_confirmations.pop(sid, None)
    pending_backends.pop(sid, None)
    loaded_groups.pop(sid, None)
    _session_last_active.pop(sid, None)
    return jsonify({"ok": True})


@app.route("/dashboard", methods=["GET"])
def dashboard():
    if not _authenticated():
        return LOGIN_PAGE.format(error="")
    return send_from_directory(STATIC_DIR, "dashboard.html")


@app.route("/memories", methods=["GET"])
def memories_page():
    if not _authenticated():
        return LOGIN_PAGE.format(error="")
    return send_from_directory(STATIC_DIR, "memories.html")


@app.route("/map", methods=["GET"])
def map_page():
    if not _authenticated():
        return LOGIN_PAGE.format(error="")
    return send_from_directory(STATIC_DIR, "map.html")


@app.route("/opportunities", methods=["GET"])
def opportunities_page():
    if not _authenticated():
        return LOGIN_PAGE.format(error="")
    return send_from_directory(STATIC_DIR, "opportunities.html")


@app.route("/starred", methods=["GET"])
def starred_page():
    if not _authenticated():
        return LOGIN_PAGE.format(error="")
    return send_from_directory(STATIC_DIR, "starred.html")


@app.route("/games", methods=["GET"])
def games_page():
    # The page lives here with the other views; the games JSON API, the hosted
    # bundles and their model proxies are the games blueprint's (routes_games.py).
    if not _authenticated():
        return LOGIN_PAGE.format(error="")
    return send_from_directory(STATIC_DIR, "games.html")


# A release cut within this many days is badged "new" on /starred. Recency —
# rather than per-visit "seen" tracking — keeps the endpoint a pure read: no
# mutating GET, no seen-state store.
RECENT_RELEASE_DAYS = 30


def _release_is_new(published_at: str) -> bool:
    """True if the release was published within RECENT_RELEASE_DAYS. Compares
    timezone-aware UTC on both sides — GitHub timestamps are UTC, and we never
    slice the ISO string against a local calendar day (per the timestamp policy);
    a missing or unparseable timestamp is simply not new."""
    if not published_at:
        return False
    try:
        published_dt = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    return datetime.now(timezone.utc) - published_dt <= timedelta(days=RECENT_RELEASE_DAYS)


@app.route("/api/starred", methods=["GET"])
def api_starred():
    """Live list of starred repos, each with its cached "what it does" blurb
    (falling back to the repo's GitHub description for any not yet cached by
    tasks/starred_blurbs.py), its cached latest release (tasks/starred_releases.py),
    and the user's cached installed version (tasks/starred_installed.py) with an
    update-available flag when that version is behind the latest release. The
    model never runs on this request path — the blurbs, releases, and installed
    versions are read from their stores — so the page stays instant."""
    if not _authenticated():
        return jsonify({"error": "not authenticated"}), 401
    result = fetch_starred_repos()
    if "error" in result:
        return jsonify({"repos": [], "error": result["error"]})
    blurbs = load_json(starred_blurbs.BLURBS_PATH, {})
    releases = load_json(starred_releases.RELEASES_PATH, {})
    installed = load_json(starred_installed.INSTALLED_PATH, {})
    repos = result.get("repos", [])
    for r in repos:
        cached = blurbs.get(r["full_name"], {}).get("blurb")
        r["blurb"] = cached or r.get("description") or ""
        release = releases.get(r["full_name"])
        r["latest_release"] = release or None
        r["release_is_new"] = bool(release) and _release_is_new(release.get("published_at"))
        # Installed version (only the repos the user tracks in starred_installed.json
        # have an entry). update_available is True only when we can confidently
        # place the installed version behind the latest release tag; a missing
        # side or an uncomparable scheme leaves it None (no false alarm).
        inst = installed.get(r["full_name"]) or {}
        r["installed_version"] = inst.get("version")
        r["installed_error"] = inst.get("error")
        r["update_available"] = compare_versions(inst.get("version"), (release or {}).get("tag"))
    return jsonify({"repos": repos})


@app.route("/api/bg/resolve", methods=["POST"])
def bg_resolve():
    """Approve/deny a background job's paused action from an ntfy button tap.

    Token-authenticated, NOT session-authenticated — this is the one mutating
    endpoint reachable without the login cookie (the phone's ntfy app calls it
    directly). The token is HMAC-signed with a ~1h expiry; single-use falls out
    of the job state machine (resolve_job only acts on an awaiting_approval job,
    so a replay finds nothing to do). It does exactly one thing and logs every
    hit. POST only — the ntfy action buttons send POST (see
    background.approval_actions), and a GET mutating endpoint invites
    prefetchers and leaves the token in more access logs than it needs to."""
    token = request.args.get("token", "")
    payload = background.read_approval_token(token)
    if payload is None:
        logger.warning("bg_resolve: rejected invalid or expired token")
        return jsonify({"error": "invalid or expired token"}), 403
    approved = payload["decision"] == "approve"
    applied = background.resolve_job(payload["job"], approved)
    logger.info("bg_resolve job=%s decision=%s applied=%s",
                payload["job"], payload["decision"], applied)
    # Acknowledge the tap so the phone shows what the choice registered as — the
    # ntfy buttons themselves have no selected state. Only on a real transition,
    # so a replayed/expired tap doesn't spam a second ack. (Title stays ASCII;
    # the emoji lives in the UTF-8 body, since the plain-push path sends the
    # title as an HTTP header.)
    if applied:
        if approved:
            notify(title="Approved", message="👍 Approved — Wren is proceeding.")
        else:
            notify(title="Denied", message="🚫 Denied — Wren will skip that action.")
    return jsonify({"ok": applied, "job": payload["job"], "decision": payload["decision"]})


# Rough chars-per-token for the mix of prose and JSON that fills a prompt.
# Deliberately generous: this is only used to catch a config that is *clearly*
# over budget, not to police the theoretical tail of a working one. A tighter
# ratio would fire on today's settings, which have never exceeded ~36% of
# num_ctx in practice — a warning that cries wolf gets muted, so it wouldn't
# survive contact with real use.
_CHARS_PER_TOKEN = 4


def _context_budget_warning() -> str | None:
    """Return a warning if the configured worst-case prompt can overflow
    num_ctx, else None.

    WREN_CHAT_MAX_HISTORY_CHARS, OLLAMA_MAX_TOOL_RESULT_CHARS and OLLAMA_NUM_CTX
    have to be raised together, and nothing couples them. _trim_history bounds
    the history *between* turns, but a turn's tool results pile on top of that
    ceiling inside advance() — up to MAX_TOOL_ITERATIONS of them — so the real
    worst case is history + every tool result. Overflow doesn't raise: Ollama
    silently drops the FRONT of the prompt, which is the system prompt (identity,
    tool rules, pinned memories), and the model degrades into repetition loops.

    That failure is near-impossible to diagnose from the symptom, so price it out
    at startup instead. Pure (returns the text, no logging or push) so the
    arithmetic is testable without side effects."""
    worst_case = MAX_HISTORY_CHARS + (MAX_TOOL_ITERATIONS * MAX_TOOL_RESULT_CHARS)
    num_ctx = int(os.getenv("OLLAMA_NUM_CTX", "8192"))
    capacity = num_ctx * _CHARS_PER_TOKEN
    if worst_case <= capacity:
        return None
    return (
        f"context budget over num_ctx: worst-case prompt ~{worst_case:,} chars "
        f"(history {MAX_HISTORY_CHARS:,} + {MAX_TOOL_ITERATIONS} tool results x "
        f"{MAX_TOOL_RESULT_CHARS:,}) vs num_ctx={num_ctx:,} (~{capacity:,} chars). "
        f"A tool-heavy turn can silently truncate the system prompt — raise "
        f"OLLAMA_NUM_CTX, or lower WREN_CHAT_MAX_HISTORY_CHARS / "
        f"OLLAMA_MAX_TOOL_RESULT_CHARS."
    )


def main():
    logger.info(f"Starting Wren chat server on {WREN_CHAT_HOST}:{WREN_CHAT_PORT}")
    # In main(), not at import: chat.server is imported at test-collection time,
    # before conftest's autouse ntfy stub is in place, so a module-level push
    # would fire a real alert at the user's phone on every pytest run.
    warning = _context_budget_warning()
    if warning:
        logger.warning(warning)
        notify(title="Wren config", message=warning)
    app.run(host=WREN_CHAT_HOST, port=WREN_CHAT_PORT, threaded=True, debug=False)


if __name__ == "__main__":
    main()
