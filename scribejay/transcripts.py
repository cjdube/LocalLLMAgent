"""Read past AI-agent chat transcripts for the daily tasks that review them —
ai_chat_learnings (what was accomplished) and claude_time_blocks (when it happened).

Two sources, both local and ToS-clean (there is no API to fetch past chats from
either consumer product, so we use what lands on disk):

- Claude Code writes every session to ~/.claude/projects/<slug>/<uuid>.jsonl as
  an append-only log of JSON events. We extract the human/assistant *text* for a
  given calendar day — dropping tool-call noise, sidechains, and injected
  system-reminders — so a session spanning several days is summarized once per
  day it was active ("new or revisited that day").
- A Gemini "drop folder" (WREN_GEMINI_CHATS_DIR): Gemini has no local footprint,
  so the user drops an exported .md/.txt/.json file per conversation and we pick up
  anything not yet processed. Files are never modified or deleted.

Everything here is deterministic Python — the model only turns the compacted text
into a summary (the small-local-model rule). Transcript text is untrusted input
(it contains web/tool output); callers treat it as data, not instructions.
"""

import json
import os
import re
from datetime import datetime
from pathlib import Path


def claude_projects_dir() -> Path:
    """Claude Code's session root, including its supported config override."""
    root = Path(os.getenv("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))
    return root.expanduser() / "projects"


# Module-level so tests can redirect it away from the real session store.
CLAUDE_PROJECTS_DIR = claude_projects_dir()

DEFAULT_GEMINI_DIR = str(Path.home() / "Vaults" / "llm-wiki-learnings" / "gemini_inbox")

# Bound the per-session text handed to the small local model. ~12k chars keeps a
# long session well inside the context window while preserving the goal (head)
# and the outcome (tail) — the two parts a "what did we accomplish" summary needs.
DEFAULT_MAX_CHARS = 12000

_SYSTEM_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)


def _parse_ts(raw):
    """Parse a JSONL event's ISO-8601 timestamp (UTC, 'Z'-suffixed) to a tz-aware
    datetime, or None if it's missing/unparseable."""
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _record_text(record) -> str | None:
    """Human/assistant text from one JSONL event, or None for tool/meta noise.

    Keeps only role=user/assistant *text* blocks: tool calls, tool results
    (echoed back as role=user with a `toolUseResult`), sidechains (subagents),
    meta events, and thinking blocks (type != "text") are all dropped, and
    injected <system-reminder> spans are stripped from the remaining text."""
    if record.get("isSidechain") or record.get("isMeta"):
        return None
    if record.get("toolUseResult") is not None:
        return None
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    role = message.get("role")
    if role not in ("user", "assistant"):
        return None

    content = message.get("content")
    parts = []
    if isinstance(content, str):
        parts.append(content)
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))

    text = _SYSTEM_REMINDER_RE.sub("", "\n".join(p for p in parts if p)).strip()
    if not text:
        return None
    return f"{'User' if role == 'user' else 'Assistant'}: {text}"


def _compact(turns: list[str], max_chars: int) -> str:
    """Join turns into one blob, trimming the middle if it exceeds max_chars. The
    head (what we set out to do) and tail (what came of it) are what a brief
    accomplishments/learnings summary depends on, so preserve both ends."""
    text = "\n\n".join(turns)
    if len(text) <= max_chars:
        return text
    head = int(max_chars * 0.6)
    tail = max_chars - head
    return f"{text[:head]}\n\n...[middle of conversation trimmed]...\n\n{text[-tail:]}"


def _read_session_day(path: Path, start: datetime, end: datetime, max_chars: int) -> dict | None:
    """One session's text for the day in [start, end], or None if it had no
    (non-noise) activity that day. `start`/`end` are tz-aware local bounds."""
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return None

    turns, first_ts, project, slug = [], None, None, None
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue

        ts = _parse_ts(record.get("timestamp"))
        if ts is None:
            continue
        ts_local = ts.astimezone(start.tzinfo)
        if not (start <= ts_local <= end):
            continue

        # cwd/slug ride along on the day's events — use them for the section header.
        if project is None and record.get("cwd"):
            project = Path(record["cwd"]).name
        if not slug and record.get("slug"):
            slug = record["slug"]

        text = _record_text(record)
        if text is None:
            continue
        if first_ts is None:
            first_ts = ts_local
        turns.append(text)

    if not turns:
        return None
    return {
        "project": project or "unknown",
        "slug": slug or "",
        "started_at": first_ts,
        "text": _compact(turns, max_chars),
    }


def fetch_claude_sessions(start: datetime, end: datetime,
                          max_chars: int = DEFAULT_MAX_CHARS) -> list[dict]:
    """Every Claude Code session with activity between `start` and `end` (tz-aware
    local bounds), as [{"project", "slug", "started_at", "text"}], oldest first.
    A session active across several days appears once per day, carrying only that
    day's turns. Returns [] if the session store is absent (nothing to do)."""
    if not CLAUDE_PROJECTS_DIR.exists():
        return []

    start_ts = start.timestamp()
    sessions = []
    for path in sorted(CLAUDE_PROJECTS_DIR.glob("*/*.jsonl")):
        # Append-only logs: a file last written before the day began can't hold
        # any of that day's events, so skip it without parsing — this keeps a
        # 14-day backfill from re-reading every historical transcript 14 times.
        try:
            if path.stat().st_mtime < start_ts:
                continue
        except OSError:
            continue
        session = _read_session_day(path, start, end, max_chars)
        if session:
            sessions.append(session)

    sessions.sort(key=lambda s: s["started_at"])
    return sessions


def _session_files(start: datetime) -> list[Path]:
    """Every session log that could hold an event at or after `start`. The
    mtime prefilter is the same one fetch_claude_sessions relies on: these are
    append-only logs, so a file last written before the window began can't hold
    any of its events and is skipped without being parsed."""
    if not CLAUDE_PROJECTS_DIR.exists():
        return []
    start_ts = start.timestamp()
    keep = []
    for path in sorted(CLAUDE_PROJECTS_DIR.glob("*/*.jsonl")):
        try:
            if path.stat().st_mtime >= start_ts:
                keep.append(path)
        except OSError:
            continue
    return keep


def fetch_session_activity(start: datetime, end: datetime) -> list[dict]:
    """Every timestamped Claude Code event between `start` and `end` (tz-aware
    local bounds), oldest first, as
    [{"ts", "project", "slug", "session", "text"}].

    Where fetch_claude_sessions returns one compacted blob per session, this
    returns the raw beat of the day — one entry per event, timestamps converted
    to the caller's local zone — which is what reconstructing working hours
    needs. It deliberately keeps records fetch_claude_sessions drops (tool
    results, subagent sidechains, meta): an agent grinding through tools for
    twenty minutes with nothing said out loud is still time at the keyboard.
    `text` carries _record_text()'s human/assistant text and is None for those
    records, so a caller can still build a prompt from what was actually said."""
    out = []
    for path in _session_files(start):
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            continue

        events, project, slug = [], None, None
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = _parse_ts(record.get("timestamp"))
            if ts is None:
                continue
            ts_local = ts.astimezone(start.tzinfo)
            if not (start <= ts_local <= end):
                continue
            if project is None and record.get("cwd"):
                project = Path(record["cwd"]).name
            if not slug and record.get("slug"):
                slug = record["slug"]
            events.append((ts_local, _record_text(record)))

        if not events:
            continue
        # Real session files sometimes carry no cwd on any of a day's events.
        # Claude Code's per-project dir name is the cwd with its separators
        # flattened ("-Users-x-Projects-MyApp"), so its last segment is the
        # same directory name Path(cwd).name would have given.
        fallback = path.parent.name.rsplit("-", 1)[-1] or "unknown"
        for ts_local, text in events:
            out.append({
                "ts": ts_local,
                "project": project or fallback,
                "slug": slug or "",
                "session": path.stem,
                "text": text,
            })

    out.sort(key=lambda e: e["ts"])
    return out


def gemini_dir() -> Path:
    # expanduser: .env.example documents this as a ~-prefixed path, and without
    # expansion a literal "~/..." dir never exists — fetch_gemini_chats would
    # silently return [] forever rather than reading the drop folder.
    return Path(os.getenv("WREN_GEMINI_CHATS_DIR", DEFAULT_GEMINI_DIR)).expanduser()


def fetch_gemini_chats(processed: dict, max_chars: int = DEFAULT_MAX_CHARS) -> list[dict]:
    """Unprocessed Gemini export files in the drop folder, as
    [{"name", "mtime", "text"}]. `processed` maps filename -> mtime already
    summarized; a file is re-summarized only if it's re-dropped (mtime changes).
    Returns [] if the folder doesn't exist (feature simply idle until used)."""
    directory = gemini_dir()
    if not directory.exists():
        return []

    out = []
    for path in sorted(directory.iterdir()):
        if path.suffix.lower() not in (".md", ".txt", ".json"):
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if processed.get(path.name) == mtime:
            continue
        try:
            text = path.read_text(errors="replace").strip()
        except OSError:
            continue
        if not text:
            continue
        out.append({"name": path.name, "mtime": mtime, "text": _compact([text], max_chars)})
    return out
