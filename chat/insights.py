"""Dashboard data layer for Wren.

Everything the dashboard shows is already on disk — this module reads and parses
it, with no Flask (or other web) imports so it stays unit-testable and runnable
standalone:

    python -m chat.insights                 # list discovered tasks
    python -m chat.insights morning_brief   # dump parsed runs for one task

Three sources:
  - launchd/*.plist  -> the schedule for each task (discover_tasks)
  - logs/<name>.log  -> run history, parsed from the task loggers' own markers
  - tool schemas     -> Wren's capabilities (describe_tools, given the lists
                        server.py already assembles)

system_map() additionally pulls the memory store, wiki page names, and saved
skills (via their agent.tools modules) to feed the /map visualization.
"""

import json
import os
import plistlib
import re
import subprocess
import threading
from datetime import datetime, timedelta
from pathlib import Path

from agent.tools.memory import recall
from agent.tools.skills import list_skills, read_skill
from agent.tools.wiki import list_wiki_pages

_ROOT = Path(__file__).resolve().parent.parent
LAUNCHD_DIR = _ROOT / "launchd"
LOGS_DIR = _ROOT / "logs"
VENV_PYTHON = _ROOT / ".venv" / "bin" / "python"

# A log line the task loggers emit, e.g.
#   2026-07-06 06:11:41,889 [INFO] Starting morning brief run
_LINE_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d+) \[(\w+)\] (.*)$")
_TS_FMT = "%Y-%m-%d %H:%M:%S,%f"

# launchd Weekday: 0 and 7 are Sunday, 1=Mon … 6=Sat.
_WEEKDAYS = {0: "Sunday", 1: "Monday", 2: "Tuesday", 3: "Wednesday",
             4: "Thursday", 5: "Friday", 6: "Saturday", 7: "Sunday"}


# --------------------------------------------------------------------------- #
# Task discovery (schedules)
# --------------------------------------------------------------------------- #

def _prettify(key: str) -> str:
    special = {"wren": "Wren Chat Server"}
    if key in special:
        return special[key]
    return key.replace("_", " ").title()


def _log_key_from_stdout(std_out_path: str) -> str:
    """The structured log basename, e.g. .../morning_brief.launchd.log -> morning_brief."""
    name = Path(std_out_path).name
    return name[: -len(".launchd.log")] if name.endswith(".launchd.log") else Path(name).stem


# discover_tasks() re-globs and re-parses every launchd plist on each call, and
# task_by_key() calls it in full to find a single task — so one dashboard poll
# can parse the plists several times. Cache the parsed list keyed by the plist
# directory's (name, mtime_ns) signature; it invalidates for free the moment a
# plist is added, removed, or edited. Guarded by a lock (Flask runs threaded).
_TASKS_CACHE: dict[str, tuple] = {}
_TASKS_CACHE_LOCK = threading.Lock()


def _launchd_signature() -> tuple:
    sig = []
    for path in sorted(LAUNCHD_DIR.glob("*.plist")):
        try:
            st = path.stat()
        except OSError:
            continue
        sig.append((path.name, st.st_mtime_ns))
    return tuple(sig)


def discover_tasks() -> list[dict]:
    """One entry per launchd plist, sorted daemons-last then by schedule time.

    Backed by a signature-keyed cache (see _TASKS_CACHE) so repeated calls in a
    single dashboard poll don't re-parse unchanged plists. Returns a fresh list
    each call so callers can't mutate the cached copy."""
    signature = _launchd_signature()
    with _TASKS_CACHE_LOCK:
        cached = _TASKS_CACHE.get("entry")
        if cached is not None and cached[0] == signature:
            tasks = cached[1]
        else:
            tasks = _discover_tasks_uncached()
            _TASKS_CACHE["entry"] = (signature, tasks)
    return list(tasks)


def _discover_tasks_uncached() -> list[dict]:
    tasks = []
    for plist_path in sorted(LAUNCHD_DIR.glob("*.plist")):
        with plist_path.open("rb") as fh:
            data = plistlib.load(fh)

        program_args = data.get("ProgramArguments", [])
        module = program_args[-1] if program_args else ""
        std_out = data.get("StandardOutPath", "")
        key = _log_key_from_stdout(std_out) if std_out else module.split(".")[-1]
        sci = data.get("StartCalendarInterval")
        is_daemon = sci is None or bool(data.get("KeepAlive"))

        tasks.append({
            "key": key,
            "display_name": _prettify(key),
            "label": data.get("Label", ""),
            "module": module,
            "schedule": sci,
            "human_schedule": "Always on" if is_daemon else human_schedule(sci),
            "log_path": str(LOGS_DIR / f"{key}.log"),
            "is_daemon": is_daemon,
        })

    tasks.sort(key=lambda t: (t["is_daemon"], _sort_time(t["schedule"])))
    return tasks


def _sort_time(sci: dict | None) -> tuple:
    if not sci:
        return (99, 99, 99)
    return (sci.get("Weekday", -1), sci.get("Hour", 0), sci.get("Minute", 0))


def task_by_key(key: str) -> dict | None:
    for task in discover_tasks():
        if task["key"] == key:
            return task
    return None


# --------------------------------------------------------------------------- #
# Schedule rendering
# --------------------------------------------------------------------------- #

def _fmt_time(hour: int, minute: int) -> str:
    suffix = "AM" if hour < 12 else "PM"
    h12 = hour % 12 or 12
    return f"{h12}:{minute:02d} {suffix}"


def human_schedule(sci: dict | None) -> str:
    if not sci:
        return "—"
    hour = sci.get("Hour", 0)
    minute = sci.get("Minute", 0)
    when = _fmt_time(hour, minute)
    if "Weekday" in sci:
        return f"{_WEEKDAYS.get(sci['Weekday'], '?')}s {when}"
    return f"Daily {when}"


def next_run(sci: dict | None, now: datetime | None = None) -> str | None:
    """Next fire time as an ISO string, or None for daemons / unschedulable."""
    if not sci:
        return None
    now = now or datetime.now()
    hour = sci.get("Hour", 0)
    minute = sci.get("Minute", 0)
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    if "Weekday" in sci:
        # launchd Sun=0/7 -> python Sun=6; launchd Mon=1..Sat=6 -> python 0..5.
        launchd_wd = sci["Weekday"]
        target_py = 6 if launchd_wd in (0, 7) else launchd_wd - 1
        for add in range(0, 8):
            day = candidate + timedelta(days=add)
            if day.weekday() == target_py and day > now:
                return day.isoformat(timespec="minutes")
        return None

    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate.isoformat(timespec="minutes")


# --------------------------------------------------------------------------- #
# Run-history parsing
# --------------------------------------------------------------------------- #

def _rotated_log_paths(log_path: Path) -> list[Path]:
    """Oldest first: <name>.log.3, .2, .1, <name>.log — chronological order."""
    paths = []
    for i in range(3, 0, -1):
        rotated = log_path.with_name(f"{log_path.name}.{i}")
        if rotated.exists():
            paths.append(rotated)
    if log_path.exists():
        paths.append(log_path)
    return paths


def _read_lines(log_path: Path) -> list[str]:
    lines: list[str] = []
    for path in _rotated_log_paths(log_path):
        lines.extend(path.read_text(errors="replace").splitlines())
    return lines


# parse_runs re-reads and re-parses every rotated log file on each call, and the
# dashboard hits it once per task on every /api/schedules poll. Cache the parsed
# runs keyed by the log files and their (mtime, size); the entry invalidates for
# free the moment a task appends a new line or a file rotates. Guarded by a lock
# because the chat server runs Flask with threaded=True.
_RUNS_CACHE: dict[str, tuple] = {}
_RUNS_CACHE_LOCK = threading.Lock()


def _log_signature(paths: list[Path]) -> tuple:
    sig = []
    for path in paths:
        try:
            st = path.stat()
        except OSError:
            continue
        sig.append((str(path), st.st_mtime_ns, st.st_size))
    return tuple(sig)


def _parse_ts(ts: str) -> datetime | None:
    try:
        return datetime.strptime(ts, _TS_FMT)
    except ValueError:
        return None


def _is_run_start(msg: str) -> bool:
    # "Starting morning brief run", "Starting daily log RERUN for 2026-07-02".
    # Deliberately excludes "Starting Wren chat server on port 8420".
    low = msg.lower()
    return low.startswith("starting ") and ("run" in low or "rerun" in low)


def _is_run_success(msg: str) -> bool:
    # Boundary messages never contain "->" (that marks tool call/result lines),
    # which keeps a tool result that happens to say "complete" from matching.
    if "->" in msg:
        return False
    low = msg.lower()
    return "complete" in low and ("run" in low or "rerun" in low)


def _parse_tool_call(msg: str) -> dict | None:
    """Recognise the loggers' three tool-activity shapes, all containing ' -> ':
        tool_call NAME(args) -> result
        NAME(args) -> result
        NAME -> result
    """
    if " -> " not in msg:
        return None
    left, _, result = msg.partition(" -> ")
    left = left.strip()
    if left.startswith("tool_call "):
        left = left[len("tool_call "):]
    m = re.match(r"^([A-Za-z_]\w*)\s*(?:\((.*)\))?$", left)
    if not m:
        return None
    return {"name": m.group(1), "args": (m.group(2) or "").strip(), "result": result.strip()}


def parse_runs(log_path, limit: int | None = None) -> list[dict]:
    """Group log lines into runs, most-recent first.

    A run = {id, start, end, duration_s, status, label, summary,
             tool_calls: [...], final_text, error}. status is one of
    "success" | "failure" | "running".

    Backed by a signature-keyed cache (see _RUNS_CACHE) so repeated dashboard
    polls of an unchanged log don't re-read and re-parse it. Always returns a
    fresh list so callers can't mutate the cached copy.
    """
    log_path = Path(log_path)
    key = str(log_path)
    signature = _log_signature(_rotated_log_paths(log_path))
    with _RUNS_CACHE_LOCK:
        cached = _RUNS_CACHE.get(key)
        if cached is not None and cached[0] == signature:
            runs = cached[1]
        else:
            runs = _parse_runs_uncached(log_path)
            _RUNS_CACHE[key] = (signature, runs)
    return list(runs[:limit]) if limit else list(runs)


def _parse_runs_uncached(log_path: Path) -> list[dict]:
    runs: list[dict] = []
    current: dict | None = None

    def close():
        nonlocal current
        if current is not None:
            runs.append(current)
            current = None

    for line in _read_lines(log_path):
        m = _LINE_RE.match(line)
        if not m:
            # Continuation (e.g. an exception traceback) — attach to the run.
            if current is not None:
                current["error"] = (current.get("error") or "") + line + "\n"
            continue

        ts, level, msg = m.group(1), m.group(2), m.group(3)

        if _is_run_start(msg):
            close()
            current = {
                "id": ts.replace(" ", "T").replace(":", "").replace(",", ""),
                "start": ts,
                "end": None,
                "duration_s": None,
                "status": "running",
                "label": msg.strip(),
                "summary": "",
                "tool_calls": [],
                "final_text": "",
                "error": "",
            }
            continue

        if current is None:
            continue

        if level in ("ERROR", "CRITICAL") or "failed" in msg.lower():
            current["status"] = "failure"
            current["end"] = ts
            current["error"] = (current.get("error") or "") + msg + "\n"
            _set_duration(current)
            continue

        if _is_run_success(msg):
            current["status"] = "success"
            current["end"] = ts
            current["summary"] = msg.strip()
            _set_duration(current)
            close()
            continue

        call = _parse_tool_call(msg)
        if call:
            current["tool_calls"].append(call)
            continue

        if msg.startswith("Agent final response:"):
            current["final_text"] = msg[len("Agent final response:"):].strip()

    close()

    runs.reverse()  # most-recent first
    for run in runs:
        run["error"] = (run.get("error") or "").strip()
    return runs  # caller (parse_runs) applies any limit against the cached copy


def _set_duration(run: dict) -> None:
    start = _parse_ts(run["start"])
    end = _parse_ts(run["end"]) if run["end"] else None
    if start and end:
        run["duration_s"] = round((end - start).total_seconds(), 1)


def parse_run_detail(log_path, run_id: str) -> dict | None:
    for run in parse_runs(log_path):
        if run["id"] == run_id:
            return run
    return None


# --------------------------------------------------------------------------- #
# Capabilities
# --------------------------------------------------------------------------- #

def describe_tools(tools: list[dict], write_tools) -> list[dict]:
    """Flatten OpenAI-format tool schemas into dashboard-friendly records."""
    out = []
    write_tools = set(write_tools)
    for schema in tools:
        fn = schema.get("function", schema)
        name = fn.get("name", "")
        params_schema = fn.get("parameters", {}) or {}
        required = set(params_schema.get("required", []))
        params = [
            {
                "name": pname,
                "type": pinfo.get("type", ""),
                "description": pinfo.get("description", ""),
                "required": pname in required,
            }
            for pname, pinfo in (params_schema.get("properties", {}) or {}).items()
        ]
        out.append({
            "name": name,
            "description": fn.get("description", ""),
            "parameters": params,
            "mutates": name in write_tools,
        })
    out.sort(key=lambda t: (t["mutates"], t["name"]))
    return out


# --------------------------------------------------------------------------- #
# System map (/map)
# --------------------------------------------------------------------------- #

# The /map page's grouping of chat tools by the external service each one talks
# to. Tool registration in chat/server.py is already a manual list, so this
# parallel map follows the same maintenance model: update it when registering a
# new tool. A drift-guard test asserts every registered tool name appears here;
# any unmapped tool still shows up on the map under "other".
TOOL_SERVICES = {
    "google_calendar": ("Google Calendar", ["get_upcoming_events", "log_calendar_event",
                                            "get_events_by_date", "recolor_event"]),
    "gmail": ("Gmail", ["send_email"]),
    "google_tasks": ("Google Tasks", ["get_tasks", "get_tasks_due_soon", "create_task",
                                      "update_task_due_date", "complete_task"]),
    "chrome": ("Chrome History", ["fetch_chrome_history"]),
    "strava": ("Strava", ["fetch_strava"]),
    "weather": ("OpenWeatherMap", ["fetch_weather"]),
    "web_search": ("Tavily Search", ["search_web"]),
    "github": ("GitHub", ["fetch_starred_repos"]),
    "youtube": ("YouTube", []),  # weekly_learnings-only; no chat tool
    "brief": ("Morning Brief", ["send_morning_brief"]),
    "memory": ("Memory", ["remember", "pin", "recall", "archive", "forget"]),
    "wiki": ("Obsidian Wiki", ["read_wiki_index", "list_wiki_pages", "read_wiki_page",
                               "list_weekly_reviews", "read_weekly_review"]),
    "skills": ("Skills", ["list_skills", "read_skill", "write_skill", "delete_skill"]),
}

# Which services each scheduled routine touches (mirrors the tasks' agent.tools
# imports) — drawn as edges on the map. Update alongside TOOL_SERVICES when a
# task gains or loses an integration.
ROUTINE_USES = {
    "morning_brief": ["google_calendar", "gmail", "github", "google_tasks", "weather"],
    "daily_log": ["google_calendar", "strava"],
    "calendar_colorizer": ["google_calendar", "gmail"],
    "weekly_learnings": ["google_calendar", "chrome", "gmail", "youtube", "wiki"],
}

# Keep the payload bounded: memory texts are truncated for the map (the detail
# panel links to /memories for the full store) and the wiki band is capped.
_MEMORY_TEXT_MAX = 300
_WIKI_PAGES_MAX = 150


def _tool_summary(tool: dict) -> dict:
    return {k: tool[k] for k in ("name", "description", "mutates")}


def system_map(tools: list[dict], write_tools) -> dict:
    """Everything the /map page draws, in one payload: chat tools grouped by
    external service, scheduled routines with last-run status and the services
    they touch, the memory store plus wiki page names, and the saved skills
    (bodies included — they're small, and it saves a second endpoint)."""
    by_name = {t["name"]: t for t in describe_tools(tools, write_tools)}

    services, placed = [], set()
    for key, (label, names) in TOOL_SERVICES.items():
        members = [_tool_summary(by_name[n]) for n in names if n in by_name]
        placed.update(n for n in names if n in by_name)
        services.append({"key": key, "label": label, "tools": members})
    leftover = sorted(set(by_name) - placed)
    if leftover:
        services.append({"key": "other", "label": "Other",
                         "tools": [_tool_summary(by_name[n]) for n in leftover]})

    routines = []
    for task in discover_tasks():
        if task["is_daemon"]:
            continue
        runs = parse_runs(task["log_path"], limit=1)
        last = runs[0] if runs else None
        routines.append({
            "key": task["key"],
            "display_name": task["display_name"],
            "human_schedule": task["human_schedule"],
            "next_run": next_run(task["schedule"]),
            "last_run": None if last is None else {
                "status": last["status"], "start": last["start"],
                "duration_s": last["duration_s"]},
            "uses": ROUTINE_USES.get(task["key"], []),
        })

    entries = []
    for m in recall()["memories"]:
        text = m.get("text", "")
        if len(text) > _MEMORY_TEXT_MAX:
            text = text[:_MEMORY_TEXT_MAX] + "…"
        entries.append({
            "id": m.get("id"),
            "text": text,
            "category": m.get("category"),
            "scope": m.get("scope", "active"),
            "created": m.get("created"),
        })
    # The vault lives on an external drive; list_wiki_pages() returns an error
    # dict when it isn't mounted — the map just shows an empty wiki band.
    wiki = list_wiki_pages()
    pages = [] if "error" in wiki else wiki.get("pages", [])
    wiki_pages = [p[:-3] if p.endswith(".md") else p for p in pages[:_WIKI_PAGES_MAX]]

    skills = []
    for s in list_skills()["skills"]:
        detail = read_skill(s["name"])
        skills.append({"name": s["name"], "description": s["description"],
                       "body": detail.get("body", "")})

    return {
        "identity": {"name": "Wren", "model": os.getenv("OLLAMA_MODEL", "")},
        "services": services,
        "routines": routines,
        "memory": {"entries": entries, "wiki_pages": wiki_pages},
        "skills": skills,
    }


# --------------------------------------------------------------------------- #
# Run-now
# --------------------------------------------------------------------------- #

class RunManager:
    """Triggers a scheduled task on demand as its own subprocess — exactly the
    command launchd runs — so its output lands in the same log and shows up in
    run history automatically. Only keys from discover_tasks() are accepted."""

    def __init__(self):
        self._procs: dict[str, subprocess.Popen] = {}
        self._lock = threading.Lock()

    def start(self, task_key: str) -> dict:
        task = task_by_key(task_key)
        if task is None:
            return {"ok": False, "error": "unknown task"}
        if task["is_daemon"]:
            return {"ok": False, "error": "the chat server is always-on and can't be run on demand"}

        with self._lock:
            existing = self._procs.get(task_key)
            if existing is not None and existing.poll() is None:
                return {"ok": False, "error": "already running"}

            launchd_log = LOGS_DIR / f"{task_key}.launchd.log"
            # The child gets its own dup of this fd at fork; close the parent's
            # copy right after Popen so it isn't left dangling in this process.
            with open(launchd_log, "a") as out:
                proc = subprocess.Popen(
                    [str(VENV_PYTHON), "-m", task["module"]],
                    cwd=str(_ROOT),
                    stdout=out,
                    stderr=subprocess.STDOUT,
                )
            self._procs[task_key] = proc
        return {"ok": True, "running": True}

    def status(self, task_key: str) -> dict:
        with self._lock:
            proc = self._procs.get(task_key)
        if proc is None:
            return {"running": False, "returncode": None}
        code = proc.poll()
        return {"running": code is None, "returncode": code}


# --------------------------------------------------------------------------- #
# Standalone use
# --------------------------------------------------------------------------- #

def _main(argv: list[str]) -> int:
    if len(argv) < 2:
        for task in discover_tasks():
            flag = " (daemon)" if task["is_daemon"] else ""
            nxt = next_run(task["schedule"])
            print(f"{task['key']:20s} {task['human_schedule']:20s} "
                  f"next={nxt or '—'}{flag}")
        return 0

    task = task_by_key(argv[1])
    if task is None:
        print(f"unknown task: {argv[1]}")
        return 1
    runs = parse_runs(task["log_path"], limit=10)
    print(f"{task['display_name']} — {len(runs)} recent run(s):")
    for run in runs:
        print(json.dumps({k: v for k, v in run.items()
                          if k not in ("tool_calls", "error")}, default=str))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_main(sys.argv))
