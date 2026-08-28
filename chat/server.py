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
    complete_text,
    escalation_available,
    escalation_backend,
    load_persona,
    probe_local_model,
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
from agent.tools import background
from agent.tools.notify import notify
from agent.tools.skills import render_skills_index
from agent.tools.wiki import render_lenses_index
from chat.auth import _authenticated
from chat.login_throttle import LoginThrottle
from chat.routes_dashboard import dashboard_bp
from chat.routes_games import games_bp
from chat.routes_logs import logs_bp
from chat.routes_opportunities import opportunities_bp
from chat.routes_starred import starred_bp
from chat.routes_wiki import wiki_bp
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
# Cap on the running summary that replaces the turns MAX_HISTORY_CHARS evicts.
# The summary rides in the system message, so it is spent out of the very budget
# it exists to protect: left ungrown it would eventually crowd out the live
# conversation it is meant to make room for. Counted into the startup budget
# warning below.
SUMMARY_CHARS = int(os.getenv("WREN_CHAT_SUMMARY_CHARS", "1500"))
# How much of the evicted transcript the summarizer is shown, and how much of
# any single message counts toward that. The model is small: a bounded prompt it
# can actually read beats a complete one it truncates.
SUMMARY_INPUT_CHARS = 6000
SUMMARY_MESSAGE_CHARS = 600
# Read timeout for an interactive turn, deliberately tighter than the
# OLLAMA_TIMEOUT the scheduled tasks use. It is a between-chunks timeout, so it
# only has to cover the wait for the FIRST token: model load plus prefill of a
# full context, measured at ~50s cold on a 40k-token prompt. Past that, someone
# waiting on their phone is better served by a fast, accurate "Ollama is busy"
# than by a five-minute spinner — the value 300 gave us on 2026-08-03.
CHAT_MODEL_TIMEOUT = float(os.getenv("WREN_CHAT_MODEL_TIMEOUT", "120"))
# Before committing a turn to the local model, ask whether its one request slot
# is free, and offer the frontier model instead when it isn't (probe_local_model
# explains why the question has to come first). Costs ~0.3s per local turn
# against a free, warm Ollama; WREN_CHAT_BUSY_PROBE=0 switches it off entirely.
BUSY_PROBE_ENABLED = os.getenv("WREN_CHAT_BUSY_PROBE", "1") != "0"

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


def _unwrap(text: str) -> str:
    """Collapse single newlines to spaces, leaving blank lines (real paragraph
    breaks) alone. The persona files are soft-wrapped so they can be edited as
    prose; the model should see the paragraphs, not the editor's line breaks."""
    return re.sub(r"(?<!\n)\n(?!\n)", " ", text)


# Two files, deliberately: wren_chat.md is how she behaves in an interactive
# session, wren_chat_tools.md is what she can do with the tools. Both are prose
# the model reads, so both live as prose rather than as literals here.
CHAT_SYSTEM_PROMPT = (
    load_persona("wren_chat.md")
    + "\n\n---\n\n"
    + _unwrap(load_persona("wren_chat_tools.md")).format(name=_NAME)
)


def _system_message_content() -> str:
    """Build the chat system prompt with today's date baked in. The local
    model doesn't know the current date, so without this it resolves a bare
    "July 2nd" to a default year (e.g. 2024) and tools like get_events_by_date come
    back empty. Computed per turn (the /chat route rebuilds history[0] on
    every message) rather than at import time or per conversation: the server
    is long-running under launchd, so the date would otherwise go stale after
    midnight, and a fact pinned or skill saved mid-session would otherwise
    only take effect in the NEXT conversation."""
    today = datetime.now().strftime("%A, %B %-d, %Y")
    dated = (
        CHAT_SYSTEM_PROMPT
        + f"\n\nToday's date is {today}. When {_NAME} names a relative day "
        "('tomorrow', 'next Tuesday', 'last Friday'), pass that phrase through to "
        "the tool verbatim — the tool resolves it, and you report the date the "
        "tool hands back. Do not work out the date yourself: you get weekday "
        "arithmetic wrong."
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
    return with_identity(dated, logger)


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

# The read-only dashboard/scheduler API, the opportunities triage API, the games
# surface, the log viewer, the starred-repo API and the wiki lint/graph API live
# in their own blueprint modules (see chat/routes_dashboard.py,
# chat/routes_opportunities.py, chat/routes_games.py, chat/routes_logs.py,
# chat/routes_starred.py, chat/routes_wiki.py); the conversation engine and auth
# stay here.
app.register_blueprint(dashboard_bp)
app.register_blueprint(opportunities_bp)
app.register_blueprint(games_bp)
app.register_blueprint(logs_bp)
app.register_blueprint(starred_bp)
app.register_blueprint(wiki_bp)

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
# The running summary of each session's compacted-away turns, rebuilt into the
# system message on every turn. It lives outside `conversations` on purpose:
# anything inside the history is a candidate for the next eviction, and the one
# message that must survive an eviction is the record of the last one.
summaries: dict[str, str] = {}
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
# Sessions whose running turn was invalidated by /chat/new. Kept separate from
# the Event because /chat/cancel stops a turn without clearing its whole session.
_reset_sessions: set[str] = set()
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


def _clear_session_state(sid: str) -> None:
    """Clear everything owned by one session. Caller holds _turn_registry_lock."""
    conversations.pop(sid, None)
    summaries.pop(sid, None)
    pending_confirmations.pop(sid, None)
    pending_backends.pop(sid, None)
    loaded_groups.pop(sid, None)
    _session_last_active.pop(sid, None)


def _finish_turn(sid: str, cancel: threading.Event, *, history: list | None = None,
                 pending_call: dict | None = None,
                 pending_backend: str | None = None) -> bool:
    """Atomically commit a completed turn and release its slot.

    Returns False when /chat/new reset the session, cancellation arrived after
    advance() returned, or this request no longer owns the session's turn slot.
    In those cases no result state is committed; a reset also clears any
    session-scoped writes the old turn made after /chat/new first cleared them.
    """
    with _turn_registry_lock:
        owns_slot = cancel_events.get(sid) is cancel
        was_reset = owns_slot and sid in _reset_sessions
        cancelled = not owns_slot or cancel.is_set()

        if was_reset:
            _clear_session_state(sid)
        elif not cancelled:
            if history is not None:
                conversations[sid] = history
            if pending_call is not None:
                pending_confirmations[sid] = pending_call
                if pending_backend:
                    pending_backends[sid] = pending_backend
                else:
                    pending_backends.pop(sid, None)

        if owns_slot:
            cancel_events.pop(sid, None)
            _reset_sessions.discard(sid)
        return owns_slot and not was_reset and not cancelled


def _evict_idle_sessions() -> None:
    cutoff = time.time() - SESSION_IDLE_EVICT_S
    with _turn_registry_lock:
        for sid in [s for s, t in _session_last_active.items() if t < cutoff]:
            if sid in cancel_events:
                continue  # a turn is somehow still running; leave it alone
            _clear_session_state(sid)
            _reset_sessions.discard(sid)

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


def _trim_history(history: list) -> list:
    """Drop the oldest whole user-turns (a user message and everything up to
    the next user message) until the history fits MAX_HISTORY_CHARS. The
    system message (index 0) and the most recent turn always survive. Returns
    the messages it dropped, oldest first, for _summarize_dropped to fold into
    the system message.

    Without this the history grows without bound: every turn re-sends all of
    it, so prefill latency climbs with session length, and once the prompt
    exceeds num_ctx Ollama silently truncates the FRONT — the system prompt —
    after which the model typically degrades into repetition loops (loop.py
    logs a warning when that happens; this keeps it from happening). Dropping
    whole turns keeps every assistant tool_call adjacent to its tool results,
    so the model never sees an orphaned half of a pair."""
    total = sum(_message_chars(m) for m in history)
    if total <= MAX_HISTORY_CHARS:
        return []
    starts = [i for i, m in enumerate(history) if m.get("role") == "user"]
    dropped = []
    while total > MAX_HISTORY_CHARS and len(starts) > 1:
        start, end = starts[0], starts[1]
        total -= sum(_message_chars(m) for m in history[start:end])
        dropped.extend(history[start:end])
        del history[start:end]
        starts = [i - (end - start) for i in starts[1:]]
    return dropped


_SUMMARY_SYSTEM_PROMPT = (
    "You compress the earlier part of a conversation so it can be deleted "
    "without losing what the rest of the conversation depends on. You write "
    "notes, not prose."
)

_SUMMARY_PROMPT = """Summary of the conversation so far (may be empty):
{previous}

Newer messages to fold into it:
{transcript}

Write the replacement summary. It replaces BOTH blocks above and both are then
deleted, so carry forward anything the rest of the conversation still needs.

Rules:
- One fact per line. Start every line with "- ". No preamble, no heading.
- Keep: what the user asked for, what was decided, facts the user stated about
  themselves, what a tool wrote or changed, and anything left unfinished.
- Drop: greetings, restated questions, and tool output nobody acted on.
- Under {limit} characters total."""


def _summary_transcript(dropped: list) -> str:
    """Render evicted messages as a bounded transcript for the summarizer.

    Trimmed from the front, not the back: the oldest of these are the messages
    the previous summary most likely already covers, while the newest sit right
    up against the window that survived."""
    lines = []
    for msg in dropped:
        text = (msg.get("content") or "").strip()
        for call in msg.get("tool_calls") or []:
            text = f"{text} {describe_call(call)}".strip()
        if text:
            lines.append(f"{msg.get('role', '?')}: {text[:SUMMARY_MESSAGE_CHARS]}")
    return "\n".join(lines)[-SUMMARY_INPUT_CHARS:]


def _summarize_dropped(dropped: list, previous: str,
                      backend: str | None = None) -> str:
    """Fold the evicted messages into the session's running summary, returning
    `previous` unchanged if the model can't produce one.

    A failure here degrades to exactly the old behaviour — those turns are gone
    either way — so it must never raise into the chat request. It logs at
    WARNING because the symptom otherwise surfaces much later and looks like the
    model losing the thread rather than like a failed call.

    think=False: this fills a fixed line format from material already in the
    prompt, and thinking tokens come out of the same budget as the answer, so a
    reasoning-heavy run returns empty content rather than a shorter summary.

    SUMMARY_CHARS=0 turns the whole thing off, model call included — the escape
    hatch if compaction's latency on the shared Ollama slot ever costs more than
    the memory is worth.

    `backend` follows the turn. A turn already routed to the frontier model
    because the local one is busy would otherwise queue HERE instead, on the
    same taken slot, and hang the escape hatch it was reaching for. Sending
    the summary the same way exposes nothing new: it summarizes the very
    conversation that turn is shipping off-device anyway."""
    if SUMMARY_CHARS <= 0:
        return ""  # compaction off — back to plain dropping, no model call
    transcript = _summary_transcript(dropped)
    if not transcript:
        return previous
    prompt = _SUMMARY_PROMPT.format(previous=previous or "(none)",
                                    transcript=transcript, limit=SUMMARY_CHARS)
    try:
        summary = complete_text(_SUMMARY_SYSTEM_PROMPT, prompt, think=False,
                                timeout=CHAT_MODEL_TIMEOUT, logger=logger,
                                **({"backend": backend} if backend else {})).strip()
    except Exception as e:
        summary = ""
        logger.warning(f"compaction call failed ({e})")
    if not summary:
        logger.warning(
            f"compaction produced no summary: {len(dropped)} messages "
            f"({len(transcript)} chars) dropped, leaving only the previous "
            f"summary ({len(previous)} chars) standing"
        )
        return previous
    if len(summary) > SUMMARY_CHARS:
        # Back up to a whole line: half a fact reads as a wrong fact.
        summary = summary[:SUMMARY_CHARS].rsplit("\n", 1)[0]
    return summary


def _with_summary(system_content: str, summary: str) -> str:
    """Append the compaction summary to the system prompt. It rides there rather
    than in a message of its own because index 0 is the one slot _trim_history
    never evicts — a summary stored as a message would be dropped by the very
    mechanism that created it."""
    if not summary:
        return system_content
    return (
        system_content
        + "\n\n---\n\nEarlier in this conversation, summarized because the "
        "original messages were dropped to save room. Treat it as something you "
        "already said and heard, not as new information:\n"
        + summary
    )


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

    Per AGENTS.md, a turn that silently does *less* is worse than one that
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


def _warn_if_final_is_empty(stage: str, text: str, history: list, checkpoint: int) -> None:
    """Log when a turn ends with an empty (or whitespace-only) final reply.

    The model simply returns content of length 0 and stops. Measured 2026-08-15
    by the evals/ harness: qwen3.6:27b-mlx did this on 5 of 11 runs of the
    `tasks_due_soon` case — it called the tool correctly, got the result, then
    said nothing about it — and gemma4:26b-mlx on 2 of 3 daily_synthesis runs.
    done_reason was `stop` and eval_count only ~65-103 tokens, so agent/loop.py's
    num_predict cut-off warning never fires: nothing was truncated, the model
    just produced nothing. The user sees an empty bubble and the log records an
    ordinary turn, which is the silence AGENTS.md says is worse than a failure.

    Whether the turn ran a tool separates two different bugs, so say which:
    no tool means the model answered nothing at all; a tool ran means it fetched
    the answer and then failed to report it (the user's request was served, the
    reply wasn't). The eval token count isn't on the returned message, but the
    `ollama_chat ... eval_tokens=N` INFO line for this same turn sits directly
    above this one in the log.

    As with _warn_if_promised_without_acting we deliberately don't auto-retry:
    the honest signal is that the model produced nothing, not that the turn
    needs another lap."""
    if text and text.strip():
        return
    turn = history[checkpoint:]
    ran = [m for m in turn if m.get("role") == "tool"]
    if ran:
        # Tool result messages carry no name (agent/loop.py appends role+content
        # only), so read the names off the assistant tool_calls that produced them.
        names = [c["function"]["name"] for m in turn if m.get("role") == "assistant"
                 for c in (m.get("tool_calls") or [])]
        logger.warning(
            "chat %s returned an EMPTY final reply after running %d tool(s) (%s) — "
            "the model got the tool result and then said nothing about it; see the "
            "ollama_chat line above for this turn's eval_tokens",
            stage, len(ran), ", ".join(names) or "unknown",
        )
    else:
        logger.warning(
            "chat %s returned an EMPTY final reply and ran no tool — the model "
            "produced nothing at all; see the ollama_chat line above for this "
            "turn's eval_tokens",
            stage,
        )


def _record_if_escalated(escalation: dict | None, outcome: str) -> None:
    """Write the escalation log entry for a turn that went off-device, if this
    was one. Best-effort: the turn already happened and its answer is on its
    way to the user, so a store failure must not turn a good reply into a 500 —
    it logs instead, because an audit trail that silently stops recording is
    worse than one that says it broke."""
    if not escalation:
        return
    try:
        record_escalation(**escalation, outcome=outcome)
    except Exception as e:
        logger.warning("failed to record escalation (%s): %s", outcome, e)


def _run_turn(sid: str, history: list, checkpoint: int, cancel: threading.Event,
              stage: str = "turn", backend: str | None = None,
              compacted: bool = False, escalation: dict | None = None):
    """Advance the session's conversation and shape the HTTP response — the
    shared back half of /chat and /chat/confirm. On cancel or failure the
    history is rolled back to `checkpoint` so the next turn starts clean; for
    /chat/confirm the checkpoint sits after the resolved tool result, which
    therefore survives the rollback (see that route's comment). `cancel` is
    the Event _begin_turn registered for this sid; _finish_turn deregisters it
    on the way out and atomically rejects a result invalidated by /chat/new or
    a late cancellation.

    `backend` is None for a normal (local) turn and set only when a /chat/confirm
    continues an escalated turn — so the frontier turn's continuation stays on
    the frontier model.

    `compacted` rides out on whatever this turn answers with, cancels and errors
    included: the history was already summarized away before advance() ran, so
    the user is owed the notice regardless of how the turn itself ends.

    `escalation` is the half-filled record for a turn that went off-device
    because the local model was busy. It is written HERE rather than in the
    route so it carries the turn's real outcome — a record logged before
    advance() would claim every escalation succeeded, including the ones that
    failed to reach the provider at all. The audit trail is the reason the
    store exists, so it is written on every path out of the turn, cancel and
    error included."""
    # Chat sends only the always-loaded core plus this session's activated
    # groups, not the whole registry — keeps the small model's context lean. The
    # tools list is mutable so a mid-turn load_tools call can extend it (see
    # _make_load_tools); dispatch carries every real tool, gated only in schema.
    tools = tools_for(loaded_groups.get(sid, set()))
    dispatch = {**DISPATCH, "load_tools": _make_load_tools(sid, tools)}
    note = {"compacted": True} if compacted else {}
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
                         stateful_tools=WRITE_TOOLS, logger=logger,
                         should_cancel=cancel.is_set,
                         timeout=CHAT_MODEL_TIMEOUT, **backend_kwargs)
    except TurnCancelled:
        del history[checkpoint:]  # discard the stopped turn so the next one starts clean
        _finish_turn(sid, cancel)
        logger.info(f"chat {stage} cancelled by user")
        _record_if_escalated(escalation, "cancelled")
        return jsonify({**note, "type": "cancelled"})
    except Exception as e:
        del history[checkpoint:]  # roll back the failed turn so the next one starts clean
        if not _finish_turn(sid, cancel):
            logger.info(f"chat {stage} cancelled before its error was returned")
            _record_if_escalated(escalation, "cancelled")
            return jsonify({**note, "type": "cancelled"})
        logger.exception(f"chat {stage} failed: {e}")
        _record_if_escalated(escalation, f"error:{e}")
        return jsonify({**note, "error": str(e)}), 500

    pending_call = result["call"] if result["type"] == "confirm" else None
    if not _finish_turn(sid, cancel, pending_call=pending_call,
                        pending_backend=backend):
        del history[checkpoint:]
        logger.info(f"chat {stage} cancelled before its result was committed")
        _record_if_escalated(escalation, "cancelled")
        return jsonify({**note, "type": "cancelled"})

    _record_if_escalated(escalation, "ok")

    if result["type"] == "confirm":
        return jsonify({**note, **_call_response(result)})

    resp = {**note, **_call_response(result)}
    if result["type"] == "final":
        _warn_if_promised_without_acting(stage, result.get("text", ""), history, checkpoint)
        _warn_if_final_is_empty(stage, result.get("text", ""), history, checkpoint)
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

    payload = request.get_json() or {}
    user_message = payload.get("message", "").strip()
    if not user_message:
        return jsonify({"error": "empty message"}), 400
    if len(user_message) > MAX_MESSAGE_CHARS:
        return jsonify({"error": "message too long"}), 400
    # Both set by the two buttons on a "Wren is busy" answer: the client re-sends
    # the same message, saying which way the user chose to go.
    frontier = payload.get("backend") == "frontier"
    force_local = bool(payload.get("force_local"))
    if frontier and not escalation_available():
        return jsonify({"error": "no frontier backend is configured"}), 400

    sid = _session_id()
    cancel = _begin_turn(sid)
    if cancel is None:
        return jsonify({"error": "a turn is already running for this session"}), 409

    # Ask the local model whether its one slot is free before this turn is
    # committed to it, and offer the frontier model instead when it isn't. This
    # sits first because everything below can call the model too — the history
    # trim summarizes through it — so a probe placed any later would already be
    # queued behind the job it exists to detect.
    #
    # Nothing has been touched yet at this point: no history, no user message.
    # A "busy" answer therefore leaves the session exactly as it found it, and
    # the turn slot taken above is handed straight back.
    if BUSY_PROBE_ENABLED and not frontier and not force_local and escalation_available():
        free, reason = probe_local_model(logger=logger)
        if not free:
            if not _finish_turn(sid, cancel):
                return jsonify({"type": "cancelled"})
            logger.info("chat turn offered the frontier model: %s", reason)
            return jsonify({"type": "busy", "reason": reason,
                            "escalate_to": active_model_label(escalation_backend())})

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

    # Set only for a turn the user redirected off-device; None keeps advance()
    # and the compaction call on their own local default.
    turn_backend = escalation_backend() if frontier else None

    # (Re)build the system message every turn, not just on session start, so a
    # fact pinned or a skill saved mid-session takes effect on the very next
    # turn, and the baked-in date rolls over at midnight in a long-lived session.
    base_system = _system_message_content()
    system_message = {"role": "system",
                      "content": _with_summary(base_system, summaries.get(sid, ""))}
    if history:
        history[0] = system_message
    else:
        history.append(system_message)

    # Trim before taking the checkpoint — trimming shifts indices, and
    # _run_turn's rollback slices from the checkpoint. Whatever the trim evicts
    # is summarized back into the system message, so a long session forgets the
    # wording of its early turns rather than the substance. That rewrite happens
    # after the trim measured the budget, so the history can end the turn up to
    # SUMMARY_CHARS over it — priced into _context_budget_warning.
    dropped = _trim_history(history)
    if dropped:
        logger.info(
            f"compacting {len(dropped)} oldest history messages to fit the context "
            f"budget ({MAX_HISTORY_CHARS} chars)"
        )
        summaries[sid] = _summarize_dropped(dropped, summaries.get(sid, ""),
                                            backend=turn_backend)
        history[0] = {"role": "system",
                      "content": _with_summary(base_system, summaries[sid])}

    # Deterministic pre-load: attach any tool groups this message's keywords cue
    # so the model usually doesn't have to make the load_tools reasoning hop.
    preload = groups_for_message(user_message)
    if preload:
        loaded_groups.setdefault(sid, set()).update(preload)

    checkpoint = len(history)
    history.append({"role": "user", "content": user_message})
    escalation = None
    if turn_backend:
        # local_reply is empty on purpose and not a missing value: the whole
        # point of this path is that the local model never answered, so there is
        # no rejected reply to pair the request with the way a manual redo has.
        escalation = {
            "request": user_message,
            "local_reply": "",
            "prompt_tokens": sum(_message_chars(m) for m in history) // _CHARS_PER_TOKEN,
            "backend": turn_backend,
            "model": active_model_label(turn_backend),
            "trigger": "busy",
        }
    return _run_turn(sid, history, checkpoint, cancel, backend=turn_backend,
                     compacted=bool(dropped), escalation=escalation)


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
        if not _finish_turn(sid, cancel):
            return jsonify({"type": "cancelled"})
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
        if not _finish_turn(sid, cancel):
            return jsonify({"type": "cancelled"})
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
                         stateful_tools=WRITE_TOOLS, backend=backend,
                         logger=logger, should_cancel=cancel.is_set)
    except TurnCancelled:
        _finish_turn(sid, cancel)
        logger.info("chat escalate cancelled by user")
        return jsonify({"type": "cancelled"})  # working discarded; local answer intact
    except Exception as e:
        if not _finish_turn(sid, cancel):
            logger.info("chat escalate cancelled before its error was returned")
            return jsonify({"type": "cancelled"})
        logger.exception(f"chat escalate failed: {e}")
        record_escalation(request=request_text, local_reply=local_reply,
                          prompt_tokens=prompt_tokens, backend=backend, model=label,
                          outcome=f"error:{e}")
        # 502: the local answer is untouched on screen — surface the failure, no
        # silent fallback to the reply the user just judged too weak.
        return jsonify({"error": f"the frontier model couldn't be reached ({e}). "
                                 "Your local answer is unchanged."}), 502

    # Success (a final answer, or a write paused for confirmation): commit the
    # frontier history and log the escalation.
    pending_call = result["call"] if result["type"] == "confirm" else None
    if not _finish_turn(sid, cancel, history=working, pending_call=pending_call,
                        pending_backend=backend):
        logger.info("chat escalate cancelled before its result was committed")
        return jsonify({"type": "cancelled"})
    record_escalation(request=request_text, local_reply=local_reply,
                      prompt_tokens=prompt_tokens, backend=backend, model=label,
                      outcome="ok")
    if result["type"] == "confirm":
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
    with _turn_registry_lock:
        event = cancel_events.get(_session_id())
        if event is not None:
            event.set()
    return jsonify({"cancelling": event is not None})


@app.route("/chat/new", methods=["POST"])
def chat_new():
    if not _authenticated():
        return jsonify({"error": "not authenticated"}), 401
    sid = _session_id()
    # Mark, cancel, and clear under the same lock _finish_turn uses to commit.
    # The old request keeps the turn slot until it drains, so no new turn can
    # start between this clear and its final cleanup. _finish_turn clears once
    # more before releasing the slot, catching any late load_tools write too.
    with _turn_registry_lock:
        event = cancel_events.get(sid)
        if event is not None:
            _reset_sessions.add(sid)
            event.set()
        else:
            _reset_sessions.discard(sid)
        _clear_session_state(sid)
    return jsonify({"ok": True})


# The static view pages. Each one's JSON API lives in its own blueprint (see the
# register_blueprint block above); only the HTML shells are here, because their
# bodies are identical — authenticate, then hand back a file. Registered from a
# table rather than as nine copies of the same four lines: the duplication is
# what made this the one place the "new routes get a blueprint" rule kept
# getting bent, and a tenth page should be a dict entry, not another handler.
#
# A page is NOT a 401: an unauthenticated browser navigation gets the login form
# at 200, so the user sees a form instead of raw JSON. The APIs behind them do
# answer 401 — that's the XHR path, where the front end handles the status.
VIEW_PAGES = {
    "/dashboard": "dashboard.html",
    "/memories": "memories.html",
    "/map": "map.html",
    "/opportunities": "opportunities.html",
    "/starred": "starred.html",
    "/logs": "logs.html",
    "/wiki": "wiki.html",
    "/wiki/lint": "wiki-lint.html",
    "/games": "games.html",
}


def _view_page(filename: str):
    def view():
        if not _authenticated():
            return LOGIN_PAGE.format(error="")
        return send_from_directory(STATIC_DIR, filename)
    return view


for _rule, _filename in VIEW_PAGES.items():
    # Endpoint names are derived, not written: nothing uses url_for, and Flask
    # only needs them unique.
    app.add_url_rule(_rule, f"page{_rule.replace('/', '_')}",
                     _view_page(_filename), methods=["GET"])


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
# ratio would fire on today's settings. Measured over 465 logged chat turns:
# median 20% of num_ctx, p95 43%, peak 82% (one turn that ran 8 tool steps).
# The peak is what a tighter ratio would trip on, and a warning that cries wolf
# gets muted — so it wouldn't survive contact with real use.
_CHARS_PER_TOKEN = 4


def _prompt_head_chars() -> int:
    """Everything in a prompt that is NOT conversation history: the system
    message, the compaction summary that rides inside it, and the tool schemas.

    Measured from live values rather than a constant, because the schema total
    moves every time a tool is registered and a hard-coded number here would rot
    silently — which is the exact failure mode this whole check exists to catch.

    Schemas are priced with every group loaded, and that is the real worst case
    rather than a pessimistic one: loaded_groups only ever grows within a
    session (nothing removes a group short of /chat/new), and a live session was
    logged holding 35 of the 55 registered tools.
    """
    system = len(_system_message_content()) + SUMMARY_CHARS
    schemas = sum(len(json.dumps(t)) for t in tools_for(set(TOOL_GROUPS)))
    return system + schemas


def _context_budget_warning() -> str | None:
    """Return a warning if the configured worst-case prompt can overflow
    num_ctx, else None.

    WREN_CHAT_MAX_HISTORY_CHARS, OLLAMA_MAX_TOOL_RESULT_CHARS and OLLAMA_NUM_CTX
    have to be raised together, and nothing couples them. _trim_history bounds
    the history *between* turns, but a turn's tool results pile on top of that
    ceiling inside advance() — up to MAX_TOOL_ITERATIONS of them — so the real
    worst case is history + every tool result, plus the compaction summary that
    is written into the system message after the trim already measured the
    budget. Overflow doesn't raise: Ollama
    silently drops the FRONT of the prompt, which is the system prompt (identity,
    tool rules, pinned memories), and the model degrades into repetition loops.

    That failure is near-impossible to diagnose from the symptom, so price it out
    at startup instead. Pure (returns the text, no logging or push) so the
    arithmetic is testable without side effects.

    **The head is counted, and it is the bigger half.** This check used to price
    only the history terms, which are the ones with env vars attached — so it
    silently ignored the ~48,000 chars of system message and tool schemas that
    ride on EVERY turn. At the 2026-08-26 config that gap was 21% of num_ctx,
    and the check reported "fits" on a config whose worst case did not. Anything
    added to the prompt head must be inside _prompt_head_chars(), or this check
    starts lying again."""
    head = _prompt_head_chars()
    worst_case = MAX_HISTORY_CHARS + head + (MAX_TOOL_ITERATIONS * MAX_TOOL_RESULT_CHARS)
    num_ctx = int(os.getenv("OLLAMA_NUM_CTX", "8192"))
    capacity = num_ctx * _CHARS_PER_TOKEN
    if worst_case <= capacity:
        return None
    return (
        f"context budget over num_ctx: worst-case prompt ~{worst_case:,} chars "
        f"(history {MAX_HISTORY_CHARS:,} + prompt head {head:,} "
        f"[system message + summary {SUMMARY_CHARS:,} + tool schemas] + "
        f"{MAX_TOOL_ITERATIONS} tool results x {MAX_TOOL_RESULT_CHARS:,}) vs "
        f"num_ctx={num_ctx:,} (~{capacity:,} chars). "
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
