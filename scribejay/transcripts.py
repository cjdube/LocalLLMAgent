"""Read past AI-agent chat transcripts for the daily tasks that review them —
ai_chat_learnings (what was accomplished) and AI Session Time Blocks (when it happened).

Three sources, all local and ToS-clean (there is no API to fetch past chats from
these consumer products, so we use what lands on disk):

- Claude Code writes every session to ~/.claude/projects/<slug>/<uuid>.jsonl as
  an append-only log of JSON events. We extract the human/assistant *text* for a
  given calendar day — dropping tool-call noise, sidechains, and injected
  system-reminders — so a session spanning several days is summarized once per
  day it was active ("new or revisited that day").
- Codex Desktop writes session JSONL under ~/.codex/sessions. We keep only
  top-level tasks the user started. Learnings keep only user text plus visible
  commentary/final answers; time tracking keeps every timestamp while exposing
  text only for those visible turns. Imported agent history, onboarding,
  guardians, subagents, and injected context are excluded.
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


# Module-level so tests can redirect them away from the real session stores.
CLAUDE_PROJECTS_DIR = claude_projects_dir()


def codex_sessions_dir() -> Path:
    """Codex's local session root, including its existing home override."""
    root = Path(os.getenv("CODEX_HOME") or (Path.home() / ".codex"))
    return root.expanduser() / "sessions"


CODEX_SESSIONS_DIR = codex_sessions_dir()

DEFAULT_GEMINI_DIR = str(Path.home() / "Vaults" / "llm-wiki-learnings" / "gemini_inbox")

# Bound the per-session text handed to the small local model. ~12k chars keeps a
# long session well inside the context window while preserving the goal (head)
# and the outcome (tail) — the two parts a "what did we accomplish" summary needs.
DEFAULT_MAX_CHARS = 12000

_SYSTEM_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)

_CODEX_INJECTED_PREFIXES = (
    "<app-context>",
    "<environment_context>",
    "<recommended_plugins>",
    "<task-notification>",
    "<user_instructions>",
    "# AGENTS.md instructions",
)
_CODEX_ASSISTANT_PHASES = {"commentary", "final_answer"}


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


def _codex_record_text(record) -> tuple[str | None, bool, bool]:
    """Extract one Codex user/assistant message.

    Returns (text, had_candidate_text, unknown_assistant_phase). The two flags
    let the caller distinguish expected injected/tool noise from private-schema
    drift that would otherwise make a daily page silently shorter.
    """
    if record.get("type") != "response_item":
        return None, False, False
    payload = record.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "message":
        return None, False, False

    role = payload.get("role")
    content = payload.get("content")
    if not isinstance(content, list):
        return None, False, False

    if role == "user":
        parts = []
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "input_text":
                continue
            text = block.get("text")
            if not isinstance(text, str) or text.startswith(_CODEX_INJECTED_PREFIXES):
                continue
            if text.strip():
                parts.append(text)
        text = "\n".join(parts).strip()
        return (f"User: {text}" if text else None), bool(text), False

    if role == "assistant":
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "output_text":
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    parts.append(text)
        if not parts:
            return None, False, False
        phase = payload.get("phase")
        if phase not in _CODEX_ASSISTANT_PHASES:
            return None, True, True
        text = "\n".join(parts).strip()
        return f"Assistant: {text}", True, False

    return None, False, False


def _codex_session_metadata(handle, path: Path, logger=None) -> tuple[dict, dict] | None:
    """Validate and return one top-level Codex task's first record and payload.

    This is the shared private-format boundary for both transcript summaries and
    activity tracking. Expected imported/system tasks are rejected before their
    potentially large bodies are read.
    """
    first = next((line.strip() for line in handle if line.strip()), "")
    try:
        meta = json.loads(first)
    except (json.JSONDecodeError, TypeError):
        if logger:
            logger.warning(f"Codex session has malformed metadata; skipping {path}")
        return None

    payload = (
        meta.get("payload")
        if isinstance(meta, dict) and meta.get("type") == "session_meta"
        else None
    )
    if not isinstance(payload, dict):
        if logger:
            logger.warning(f"Codex session has no session_meta first record; skipping {path}")
        return None

    thread_source = payload.get("thread_source")
    if thread_source != "user":
        # Imported agent history currently has legacy history and no
        # thread_source. Guardians and onboarding identify themselves. All are
        # expected non-user tasks, so skip them before reading the body.
        expected = (
            payload.get("history_mode") == "legacy" and thread_source is None
        ) or thread_source in {"guardian_review", "onboarding_checklist"}
        if not expected and logger:
            logger.warning(
                f"Codex session has unsupported thread_source {thread_source!r}; "
                f"skipping {path}"
            )
        return None
    return meta, payload


def _read_codex_session_day(path: Path, start: datetime, end: datetime,
                            max_chars: int, logger=None) -> dict | None:
    """One top-level Codex Desktop task's visible text for the local day."""
    try:
        handle = path.open(errors="replace")
    except OSError:
        return None

    with handle:
        metadata = _codex_session_metadata(handle, path, logger)
        if metadata is None:
            return None
        _, payload = metadata

        turns, first_ts = [], None
        candidates = 0
        unknown_phases = 0
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue

            ts = _parse_ts(record.get("timestamp"))
            if ts is None:
                continue
            ts_local = ts.astimezone(start.tzinfo)
            if not (start <= ts_local <= end):
                continue

            text, had_candidate, unknown_phase = _codex_record_text(record)
            candidates += int(had_candidate)
            unknown_phases += int(unknown_phase)
            if text is None:
                continue
            if first_ts is None:
                first_ts = ts_local
            turns.append(text)

    if unknown_phases and logger:
        logger.warning(
            f"Ignored {unknown_phases} Codex assistant message(s) with an unknown phase "
            f"in {path}"
        )
    if not turns:
        if candidates and logger:
            logger.warning(
                f"Codex session had {candidates} candidate message(s) but no extractable "
                f"turns in {path}"
            )
        return None
    cwd = payload.get("cwd")
    return {
        "project": Path(cwd).name if isinstance(cwd, str) and cwd else "unknown",
        "slug": "",
        "started_at": first_ts,
        "text": _compact(turns, max_chars),
    }


def _codex_session_files(start: datetime) -> list[Path]:
    """Codex logs whose append-only mtime could contain the requested window."""
    if not CODEX_SESSIONS_DIR.exists():
        return []

    start_ts = start.timestamp()
    paths = []
    for path in sorted(CODEX_SESSIONS_DIR.glob("*/*/*/*.jsonl")):
        try:
            if path.stat().st_mtime >= start_ts:
                paths.append(path)
        except OSError:
            continue
    return paths


def fetch_codex_sessions(start: datetime, end: datetime,
                         max_chars: int = DEFAULT_MAX_CHARS, logger=None) -> list[dict]:
    """Top-level Codex Desktop tasks active inside tz-aware local bounds.

    Codex's on-disk JSONL is a private, changeable format. Expected non-user
    histories are ignored; suspicious metadata/content changes log a warning and
    degrade to an empty session rather than failing the whole daily review.
    """
    sessions = []
    for path in _codex_session_files(start):
        session = _read_codex_session_day(path, start, end, max_chars, logger)
        if session:
            sessions.append(session)

    sessions.sort(key=lambda s: s["started_at"])
    return sessions


def fetch_codex_session_activity(start: datetime, end: datetime, logger=None) -> list[dict]:
    """Every timestamped event from top-level Codex Desktop tasks in the window.

    The return shape matches fetch_session_activity. Tool, reasoning, and other
    records contribute timestamps to the working timeline but carry no `text`;
    only visible user, commentary, and final-answer messages can reach a model.
    """
    out = []
    for path in _codex_session_files(start):
        try:
            handle = path.open(errors="replace")
        except OSError:
            continue

        with handle:
            metadata = _codex_session_metadata(handle, path, logger)
            if metadata is None:
                continue
            meta, payload = metadata
            project = payload.get("cwd")
            project = Path(project).name if isinstance(project, str) and project else "unknown"
            candidates = 0
            unknown_phases = 0
            events = []

            def add_record(record):
                nonlocal candidates, unknown_phases
                if not isinstance(record, dict):
                    return
                ts = _parse_ts(record.get("timestamp"))
                if ts is None:
                    return
                ts_local = ts.astimezone(start.tzinfo)
                if not (start <= ts_local <= end):
                    return
                text, had_candidate, unknown_phase = _codex_record_text(record)
                candidates += int(had_candidate)
                unknown_phases += int(unknown_phase)
                events.append((ts_local, text))

            add_record(meta)
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                add_record(record)

        if unknown_phases and logger:
            logger.warning(
                f"Ignored {unknown_phases} Codex assistant message(s) with an unknown phase "
                f"in {path}"
            )
        if events and candidates and not any(text for _, text in events) and logger:
            logger.warning(
                f"Codex session had {candidates} candidate message(s) but no extractable "
                f"turns in {path}"
            )
        for ts_local, text in events:
            out.append({
                "ts": ts_local,
                "project": project,
                "slug": "",
                "session": path.stem,
                "text": text,
            })

    out.sort(key=lambda event: event["ts"])
    return out


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
