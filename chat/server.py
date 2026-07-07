"""Wren — ad hoc chat interface, backed by the same tool-calling loop the
scheduled tasks use. An always-on Flask app (run via launchd, see
launchd/com.craigdube.localllmagent.wren.plist), meant to be reached only
over Tailscale, never the open internet.

Usage:
    python -m chat.server
"""

import hmac
import json
import logging
import os
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory, session

from agent.loop import advance, load_persona, resolve, with_identity
from chat.insights import (
    RunManager,
    describe_tools,
    discover_tasks,
    next_run,
    parse_run_detail,
    parse_runs,
    task_by_key,
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
from agent.tools.email import TOOL_SCHEMA as EMAIL_SCHEMA, send_email
from agent.tools.github_starred import TOOL_SCHEMA as GITHUB_STARRED_SCHEMA, fetch_starred_repos
from agent.tools.strava import TOOL_SCHEMA as STRAVA_SCHEMA, fetch_strava
from agent.tools.weather import TOOL_SCHEMA as WEATHER_SCHEMA, fetch_weather
from agent.tools.web_search import TOOL_SCHEMA as WEB_SEARCH_SCHEMA, search_web
from tasks._common import setup_logger
from tasks.morning_brief import SEND_BRIEF_TOOL_SCHEMA, build_and_send_brief

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

if not WREN_CHAT_TOKEN or not FLASK_SECRET_KEY:
    raise RuntimeError(
        "WREN_CHAT_TOKEN and FLASK_SECRET_KEY must both be set in config/.env "
        "before running the chat server — without them the login check can't "
        "run safely."
    )

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
]


def _send_morning_brief(**_) -> dict:
    # Bound here instead of imported directly so it logs to the "wren"
    # logger below rather than build_and_send_brief()'s default of none.
    # Accepts/ignores stray kwargs in case the model hallucinates an argument
    # for this no-parameter tool.
    return build_and_send_brief(logger=logger)


DISPATCH = {
    "get_upcoming_events": get_upcoming_events,
    "log_calendar_event": log_calendar_event,
    "get_events_by_date": get_events_by_date,
    "recolor_event": recolor_event,
    "fetch_chrome_history": fetch_chrome_history,
    "send_email": send_email,
    "fetch_strava": fetch_strava,
    "fetch_weather": fetch_weather,
    "search_web": search_web,
    "fetch_starred_repos": fetch_starred_repos,
    "send_morning_brief": _send_morning_brief,
}
WRITE_TOOLS = frozenset({"log_calendar_event", "send_email", "recolor_event", "send_morning_brief"})

CHAT_SYSTEM_PROMPT = (
    load_persona("wren_chat.md")
    + "\n\n---\n\n"
    + "You can check the weather (current conditions plus a forecast up to 5 "
    "days out — pass a days argument if Craig asks about more than just "
    "today), look up Craig's calendar (upcoming, or any "
    "past or future date range), fetch his recent Strava activities, look up "
    "his recent Chrome browsing history, search the web for current "
    "information you don't already know, and look up his starred GitHub "
    "repos (pass days_ago rather than computing a date yourself if he asks "
    "what's new in the last N days). Each repo's recent_changes field is "
    "already condensed to what matters — if it ends with a '(+N more "
    "commits)' or '(+N more releases)' note, that count is important, not a "
    "footnote to drop: always carry it into your summary (e.g. 'plus 8 more "
    "commits') so Craig knows the repo had more activity than what's shown. "
    "Use these tools when they'd help answer the question. You can also send an email, log a calendar "
    "event, or recolor an existing event by category on request; the app "
    "pauses those for Craig's confirmation before they execute, so just "
    "explain what you're about to do. If Craig asks you to send or resend "
    "his morning brief, use send_morning_brief rather than composing that "
    "email yourself with send_email — it builds the same formatted HTML "
    "brief the scheduled task sends, which you can't reliably reproduce by "
    "writing the email body freehand."
)

def _system_message_content() -> str:
    """Build the chat system prompt with today's date baked in. The local
    model doesn't know the current date, so without this it resolves a bare
    "July 2nd" to a default year (e.g. 2024) and tools like fetch_strava come
    back empty. Computed per conversation rather than at import time because
    the server is long-running under launchd and would otherwise go stale
    after midnight."""
    today = datetime.now().strftime("%A, %B %-d, %Y")
    dated = (
        CHAT_SYSTEM_PROMPT
        + f"\n\nToday's date is {today}. When Craig names a date without a year "
        "(e.g. 'July 2nd') or a relative day, resolve it against today's date — "
        "never guess the year."
    )
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

logger = setup_logger("wren")
logger.setLevel(logging.INFO)

# In-memory only, per the "fresh session" design — lost on server restart.
conversations: dict[str, list[dict]] = {}
pending_confirmations: dict[str, dict] = {}

# Triggers scheduled tasks on demand for the dashboard's "Run now" button.
run_manager = RunManager()

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


class LoginThrottle:
    """Per-client failed-login limiter — defense-in-depth on the one
    internet-adjacent surface. The 256-bit token is the real defense (brute
    force is infeasible), so this only aims to blunt automated guessing, not to
    lock the box down. After MAX_FAILURES failures inside WINDOW_S a client is
    locked out for a backoff that doubles on repeat offenses (capped at
    MAX_LOCKOUT_S), then the counter resets — short enough that the single
    legitimate user fat-fingering the token isn't durably self-DoSed.

    Keyed by caller identity (see _client_ip): behind `tailscale serve` most
    requests arrive from loopback, so this is coarse, but it still slows a
    proxied guessing loop. `clock` is injectable for tests."""

    MAX_FAILURES = 5
    WINDOW_S = 300
    BASE_LOCKOUT_S = 30
    MAX_LOCKOUT_S = 900

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._state: dict[str, dict] = {}

    def retry_after(self, key: str) -> float:
        """Seconds the caller must still wait, or 0 if an attempt is allowed now."""
        now = self._clock()
        with self._lock:
            entry = self._state.get(key)
            if not entry:
                return 0.0
            return max(0.0, entry["locked_until"] - now)

    def record_failure(self, key: str) -> None:
        now = self._clock()
        with self._lock:
            entry = self._state.get(key)
            if not entry or now - entry["window_start"] > self.WINDOW_S:
                entry = {"failures": 0, "window_start": now, "lockouts": 0, "locked_until": 0.0}
            entry["failures"] += 1
            if entry["failures"] >= self.MAX_FAILURES:
                entry["lockouts"] += 1
                backoff = min(self.BASE_LOCKOUT_S * 2 ** (entry["lockouts"] - 1), self.MAX_LOCKOUT_S)
                entry["locked_until"] = now + backoff
                entry["failures"] = 0
                entry["window_start"] = now
            self._state[key] = entry

    def record_success(self, key: str) -> None:
        with self._lock:
            self._state.pop(key, None)


def _client_ip() -> str:
    # `tailscale serve` reverse-proxies from loopback, so remote_addr is
    # 127.0.0.1 for real users; prefer the first hop it records in
    # X-Forwarded-For when that header is present.
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "unknown"


# Rate-limits failed /login attempts per client (defense-in-depth; see class).
login_throttle = LoginThrottle()


def _authenticated() -> bool:
    return bool(session.get("authenticated"))


def _session_id() -> str:
    if "sid" not in session:
        session["sid"] = str(uuid.uuid4())
    return session["sid"]


def _describe_call(call: dict) -> str:
    name = call["function"]["name"]
    args = call["function"].get("arguments", {})
    if name == "send_email":
        return f'Send an email — subject: "{args.get("subject", "")}"'
    if name == "send_morning_brief":
        return "Send the morning brief (weather, calendar, AI news)"
    if name == "log_calendar_event":
        return f'Create calendar event "{args.get("summary", "")}" from {args.get("start", "?")} to {args.get("end", "?")}'
    if name == "recolor_event":
        return f'Recolor calendar event to "{args.get("category", "")}"'
    return f"{name}({json.dumps(args)})"


def _call_response(result: dict) -> dict:
    if result["type"] == "final":
        return {"type": "final", "text": result["text"]}
    call = result["call"]
    return {
        "type": "confirm",
        "tool": call["function"]["name"],
        "args": call["function"].get("arguments", {}),
        "summary": _describe_call(call),
    }


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
    history = conversations.setdefault(sid, [])

    # If a write action was awaiting confirmation and the user sent a new
    # message instead of answering, treat it as declining that action —
    # otherwise its unanswered tool_call would leave the history malformed.
    pending = pending_confirmations.pop(sid, None)
    if pending is not None:
        resolve(history, pending, False, DISPATCH, logger=logger)

    if not history:
        history.append({"role": "system", "content": _system_message_content()})

    checkpoint = len(history)
    history.append({"role": "user", "content": user_message})
    try:
        result = advance(history, TOOLS, DISPATCH, confirm_before=WRITE_TOOLS, logger=logger)
    except Exception as e:
        del history[checkpoint:]  # roll back this failed turn so the next one starts clean
        logger.exception(f"chat turn failed: {e}")
        return jsonify({"error": str(e)}), 500

    if result["type"] == "confirm":
        pending_confirmations[sid] = result["call"]
    return jsonify(_call_response(result))


@app.route("/chat/confirm", methods=["POST"])
def chat_confirm():
    if not _authenticated():
        return jsonify({"error": "not authenticated"}), 401

    approved = bool((request.get_json() or {}).get("approved"))
    sid = _session_id()
    call = pending_confirmations.pop(sid, None)
    if call is None:
        return jsonify({"error": "no pending action"}), 400

    history = conversations.setdefault(sid, [])
    # Resolve first (this answers the paused tool_call), then checkpoint — so a
    # rollback below never strips the tool result and re-orphans that call.
    resolve(history, call, approved, DISPATCH, logger=logger)

    checkpoint = len(history)
    try:
        result = advance(history, TOOLS, DISPATCH, confirm_before=WRITE_TOOLS, logger=logger)
    except Exception as e:
        del history[checkpoint:]  # roll back the failed continuation, keep the resolved call
        logger.exception(f"chat turn failed after confirmation: {e}")
        return jsonify({"error": str(e)}), 500

    if result["type"] == "confirm":
        pending_confirmations[sid] = result["call"]
    return jsonify(_call_response(result))


@app.route("/chat/new", methods=["POST"])
def chat_new():
    if not _authenticated():
        return jsonify({"error": "not authenticated"}), 401
    sid = _session_id()
    conversations.pop(sid, None)
    pending_confirmations.pop(sid, None)
    return jsonify({"ok": True})


@app.route("/dashboard", methods=["GET"])
def dashboard():
    if not _authenticated():
        return LOGIN_PAGE.format(error="")
    return send_from_directory(STATIC_DIR, "dashboard.html")


def _run_summary(run: dict | None) -> dict | None:
    """The slice of a run the Overview needs — omits the heavy tool_calls/error."""
    if run is None:
        return None
    return {k: run[k] for k in ("id", "start", "end", "duration_s", "status", "summary")}


@app.route("/api/schedules", methods=["GET"])
def api_schedules():
    if not _authenticated():
        return jsonify({"error": "not authenticated"}), 401
    out = []
    for task in discover_tasks():
        runs = [] if task["is_daemon"] else parse_runs(task["log_path"], limit=10)
        out.append({
            "key": task["key"],
            "display_name": task["display_name"],
            "human_schedule": task["human_schedule"],
            "is_daemon": task["is_daemon"],
            "next_run": next_run(task["schedule"]),
            "last_run": _run_summary(runs[0] if runs else None),
            "recent_statuses": [r["status"] for r in runs],
        })
    return jsonify({"tasks": out})


@app.route("/api/runs/<task_key>", methods=["GET"])
def api_runs(task_key: str):
    if not _authenticated():
        return jsonify({"error": "not authenticated"}), 401
    task = task_by_key(task_key)
    if task is None:
        return jsonify({"error": "unknown task"}), 404
    runs = parse_runs(task["log_path"], limit=50)
    return jsonify({
        "task": {"key": task["key"], "display_name": task["display_name"],
                 "human_schedule": task["human_schedule"], "is_daemon": task["is_daemon"]},
        "runs": [_run_summary(r) for r in runs],
    })


@app.route("/api/runs/<task_key>/<run_id>", methods=["GET"])
def api_run_detail(task_key: str, run_id: str):
    if not _authenticated():
        return jsonify({"error": "not authenticated"}), 401
    task = task_by_key(task_key)
    if task is None:
        return jsonify({"error": "unknown task"}), 404
    run = parse_run_detail(task["log_path"], run_id)
    if run is None:
        return jsonify({"error": "unknown run"}), 404
    return jsonify(run)


@app.route("/api/capabilities", methods=["GET"])
def api_capabilities():
    if not _authenticated():
        return jsonify({"error": "not authenticated"}), 401
    return jsonify({"tools": describe_tools(TOOLS, WRITE_TOOLS)})


@app.route("/api/run/<task_key>", methods=["POST"])
def api_run(task_key: str):
    if not _authenticated():
        return jsonify({"error": "not authenticated"}), 401
    result = run_manager.start(task_key)
    logger.info(f"dashboard run-now {task_key} -> {result}")
    return jsonify(result), (200 if result.get("ok") else 409)


@app.route("/api/run/<task_key>/status", methods=["GET"])
def api_run_status(task_key: str):
    if not _authenticated():
        return jsonify({"error": "not authenticated"}), 401
    if task_by_key(task_key) is None:
        return jsonify({"error": "unknown task"}), 404
    return jsonify(run_manager.status(task_key))


def main():
    logger.info(f"Starting Wren chat server on {WREN_CHAT_HOST}:{WREN_CHAT_PORT}")
    app.run(host=WREN_CHAT_HOST, port=WREN_CHAT_PORT, threaded=True, debug=False)


if __name__ == "__main__":
    main()
