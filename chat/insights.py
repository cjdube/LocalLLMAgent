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
"""

import json
import plistlib
import re
import subprocess
import threading
from datetime import datetime, timedelta
from pathlib import Path

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


def discover_tasks() -> list[dict]:
    """One entry per launchd plist, sorted daemons-last then by schedule time."""
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
