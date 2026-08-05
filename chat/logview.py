"""Log-file browsing for the /logs view — the reader behind chat/routes_logs.py.

chat/insights.py already parses logs, but it answers a different question: it
groups a *scheduled task's* lines into runs, and refuses daemons outright ("no
discrete scheduled runs"). That leaves the chat server's own log — the largest
one, and the one you want while testing — with no way to read it at all short of
ssh. This module answers "show me this file", for any log we write.

Three things it owns, all of which the file sizes force:

  1. A bounded read. logs/<name>.log is capped at 8MB by setup_logger's
     RotatingFileHandler, but logs/<name>.launchd.log is launchd's
     StandardOutPath — append-only, rotated by nothing, growing ~19KB/day for
     the chat server. So reads seek backwards from the end of the file over a
     fixed window rather than loading it; that is what keeps this page working
     in a year, not a micro-optimisation.

  2. Entry folding. ~31% of lines in these logs are continuations — a drafted
     markdown digest under one INFO line, a traceback under one ERROR — and a
     line-per-row viewer shreds them. An entry is a timestamped line plus every
     continuation line beneath it.

  3. Truncation with an honest count. The longest line ever written here is
     46,683 chars (a 250-event calendar result, 2026-07-10, back when tool
     results were uncapped — see MAX_TOOL_RESULT_CHARS in agent/loop.py). Both
     the message and the continuation list are capped, and what was dropped is
     reported rather than silently swallowed.

Deliberately reads only the LIVE file, not the rotated .log.1/.2/.3 siblings
that insights._read_lines stitches together. A byte offset is this module's
paging cursor and its expand key, and a cursor spanning several files would have
to become a (file, offset) pair — real complexity for a case that has never
occurred (nothing has reached the 2MB rotation threshold yet). list_logs()
reports `rotated` so the page can say older data exists instead of implying the
file is all there is.

Standalone-runnable, like chat/insights.py:

    python -m chat.logview                  # list readable logs
    python -m chat.logview wren             # dump the tail of one
"""

from pathlib import Path

from chat import insights
from chat.insights import _LINE_RE, _is_run_start, _is_run_success, discover_tasks

# How far back from the end of the file (or from a paging cursor) a single read
# reaches. 512KB is ~4000 typical entries — far more than the 300 a page shows,
# so a level filter still has plenty to select from without a second round trip.
WINDOW_BYTES = 512_000

# Per-entry caps. 4000 chars is half the 8000-char tool-result cap the model
# itself gets, which is the largest thing routinely logged; anything past it is
# a payload to expand, not a message to read.
MAX_MSG_CHARS = 4000
MAX_EXTRA_LINES = 200

DEFAULT_LIMIT = 300
MAX_LIMIT = 1000

# Rows of surrounding context kept around each hit when a filter is active. The
# reason is a real incident: the WARNING "prompt reached num_ctx" sits three
# lines below the 46KB tool result that caused it, so a filtered view showing
# only matching lines reports the symptom and hides the cause.
FILTER_CONTEXT = 2

_LEVEL_ORDER = {"debug": 0, "info": 1, "warning": 2, "error": 3, "critical": 4}

# Files in logs/ that are nobody's live log: frozen hand-made snapshots. Not
# hidden because they're uninteresting but because they are never appended to,
# so a viewer built around "tail the end" has nothing to offer them.
_SKIP_SUFFIXES = (".bak",)


# --------------------------------------------------------------------------- #
# What's readable
# --------------------------------------------------------------------------- #

def _stream_info(path: Path) -> dict | None:
    try:
        st = path.stat()
    except OSError:
        return None
    rotated = sum(1 for i in (1, 2, 3) if path.with_name(f"{path.name}.{i}").exists())
    return {"size": st.st_size, "mtime": int(st.st_mtime), "rotated": rotated}


def _catalogue() -> list[tuple[dict, dict[str, Path]]]:
    """[(public_entry, {stream: path})] — the one place a log's path is decided.

    Two streams per task, and they are not redundant:
      - "log"    logs/<key>.log — what setup_logger's file handler wrote.
      - "stdout" logs/<key>.launchd.log — launchd's StandardOutPath capture,
                 which is where a crash *before* the logger initialises lands.

    Anything else in logs/ that nothing claims is listed after the tasks, so a
    log from a retired task stays reachable instead of vanishing from the UI.
    Paths are kept out of the public entry: the page needs a basename to show,
    not this machine's directory layout.
    """
    out: list[tuple[dict, dict[str, Path]]] = []
    claimed: set[Path] = set()

    for task in discover_tasks():
        structured = Path(task["log_path"])
        paths = {"log": structured,
                 "stdout": structured.with_name(f"{structured.stem}.launchd.log")}
        streams, live = {}, {}
        for name, path in paths.items():
            info = _stream_info(path)
            if info:
                claimed.add(path)
                streams[name] = info
                live[name] = path
        if not streams:
            continue
        out.append(({
            "key": task["key"],
            "display_name": task["display_name"],
            "human_schedule": task["human_schedule"],
            "is_daemon": task["is_daemon"],
            "external": task["external"],
            "streams": streams,
        }, live))

    for path in sorted(insights.LOGS_DIR.glob("*.log")):
        if path in claimed or path.name.endswith(_SKIP_SUFFIXES):
            continue
        info = _stream_info(path)
        if not info:
            continue
        is_stdout = path.name.endswith(".launchd.log")
        stem = path.name[:-len(".launchd.log")] if is_stdout else path.stem
        stream = "stdout" if is_stdout else "log"
        out.append(({
            "key": f"file:{path.name}",
            "display_name": stem.replace("_", " ").title(),
            "human_schedule": "—",
            "is_daemon": False,
            "external": False,
            "orphan": True,
            "streams": {stream: info},
        }, {stream: path}))
    return out


def list_logs() -> list[dict]:
    """Every log the viewer will open, task-ordered. The /api/logs payload."""
    return [entry for entry, _ in _catalogue()]


def resolve(key: str, stream: str = "log") -> Path | None:
    """The file for (key, stream), or None.

    The whitelist IS the path resolution: a caller-supplied string is only ever
    compared against keys the catalogue produced, never joined to a directory.
    So "../../etc/passwd" is an unknown key rather than a traversal to defend
    against — no code path here builds a path out of input.
    """
    for entry, paths in _catalogue():
        if entry["key"] == key:
            return paths.get(stream)
    return None


# --------------------------------------------------------------------------- #
# Bounded reads
# --------------------------------------------------------------------------- #

def _drop_trailing_partial(raw: bytes) -> bytes:
    """Everything up to and including the last newline.

    A log being written to can be read mid-line. Returning that fragment would
    show a half entry and — worse for the live tail — the next poll would read
    the same line again from the start and render it twice. Whole lines only;
    the remainder arrives on the next read.
    """
    cut = raw.rfind(b"\n")
    return raw[:cut + 1] if cut != -1 else b""


def _read_window(path: Path, end: int | None = None) -> tuple[str, int, int]:
    """The last WINDOW_BYTES of `path` before `end`, as (text, start, end).

    A window that doesn't start at byte 0 begins mid-line, so the partial first
    line is discarded and start advanced past it — otherwise the oldest entry on
    every page would be a fragment presented as a whole line.
    """
    size = path.stat().st_size
    end = size if end is None else max(0, min(end, size))
    start = max(0, end - WINDOW_BYTES)
    with path.open("rb") as fh:
        fh.seek(start)
        raw = fh.read(end - start)
    if start > 0:
        cut = raw.find(b"\n")
        if cut == -1:
            return "", end, end
        start += cut + 1
        raw = raw[cut + 1:]
    raw = _drop_trailing_partial(raw)
    return raw.decode("utf-8", errors="replace"), start, start + len(raw)


def _read_forward(path: Path, after: int) -> tuple[str, int, int, bool]:
    """Everything appended since byte `after`, as (text, start, end, skipped).

    The live tail's read. Bounded like every other read here: if more than a
    window's worth arrived while the page wasn't looking — a task that logged a
    burst, or a laptop that slept — it jumps to the last WINDOW_BYTES and says
    so with `skipped`, rather than growing the read to match the gap.

    A truncated file (rotation happened, or someone emptied it) reads as
    after > size; that restarts from the current end instead of erroring.
    """
    size = path.stat().st_size
    start = max(0, min(after, size))
    skipped = size - start > WINDOW_BYTES
    if skipped:
        start = size - WINDOW_BYTES
    with path.open("rb") as fh:
        fh.seek(start)
        raw = fh.read(size - start)
    if skipped:
        cut = raw.find(b"\n")
        if cut == -1:
            return "", size, size, True
        start += cut + 1
        raw = raw[cut + 1:]
    raw = _drop_trailing_partial(raw)
    return raw.decode("utf-8", errors="replace"), start, start + len(raw), skipped


def _parse_window(text: str, base_offset: int) -> list[dict]:
    """Group a window's lines into entries, oldest first.

    `offset` on each entry is the absolute byte offset of its first line, which
    is what makes it usable as both a paging cursor and a stable row id.
    """
    entries: list[dict] = []
    offset = base_offset
    for line in text.split("\n"):
        line_bytes = len(line.encode("utf-8")) + 1
        m = _LINE_RE.match(line)
        if m:
            ts, level, msg = m.group(1), m.group(2), m.group(3)
            entries.append({
                "offset": offset,
                "ts": ts,
                "level": level.lower(),
                "msg": msg,
                "extra": [],
                "boundary": ("start" if _is_run_start(msg)
                             else "end" if _is_run_success(msg) else None),
            })
        elif entries and line:
            entries[-1]["extra"].append(line)
        offset += line_bytes
    return entries


def _finalize(entry: dict) -> dict:
    """Apply the per-entry caps, reporting what they dropped."""
    msg, dropped_chars = entry["msg"], 0
    if len(msg) > MAX_MSG_CHARS:
        dropped_chars = len(msg) - MAX_MSG_CHARS
        msg = msg[:MAX_MSG_CHARS]
    extra, dropped_lines = entry["extra"], 0
    if len(extra) > MAX_EXTRA_LINES:
        dropped_lines = len(extra) - MAX_EXTRA_LINES
        extra = extra[:MAX_EXTRA_LINES]
    return {
        "offset": entry["offset"],
        "ts": entry["ts"],
        "level": entry["level"],
        "msg": msg,
        "dropped_chars": dropped_chars,
        "extra": extra,
        "dropped_lines": dropped_lines,
        "boundary": entry["boundary"],
    }


# --------------------------------------------------------------------------- #
# The read the API serves
# --------------------------------------------------------------------------- #

def _matches(entry: dict, min_level: int, query: str) -> bool:
    if _LEVEL_ORDER.get(entry["level"], 1) < min_level:
        return False
    if query:
        haystack = (entry["msg"] + "\n" + "\n".join(entry["extra"])).lower()
        if query not in haystack:
            return False
    return True


def _select(entries: list[dict], level: str, query: str) -> list[dict]:
    """Filtered entries, each hit carrying FILTER_CONTEXT rows either side.

    Context rows are marked `context: True` so the page can dim them — they are
    there to explain a hit, not to be read as hits themselves.
    """
    min_level = _LEVEL_ORDER.get((level or "").lower(), 0)
    query = (query or "").strip().lower()
    if not min_level and not query:
        return [dict(e, context=False) for e in entries]

    keep: dict[int, bool] = {}
    for i, entry in enumerate(entries):
        if not _matches(entry, min_level, query):
            continue
        keep[i] = True
        for j in range(max(0, i - FILTER_CONTEXT), min(len(entries), i + FILTER_CONTEXT + 1)):
            keep.setdefault(j, False)
    return [dict(entries[i], context=not keep[i]) for i in sorted(keep)]


def read_log(key: str, stream: str = "log", limit: int = DEFAULT_LIMIT,
             before: int | None = None, after: int | None = None,
             level: str = "", query: str = "") -> dict | None:
    """One page of `key`'s log, always CHRONOLOGICAL (oldest first). None if the
    key is unknown.

    Ordering is the caller's business, not this module's. The page renders
    newest-first, but runs have to be paired start-to-end and continuation lines
    belong under the line above them — both of which are natural forwards and
    fiddly backwards. So this returns time order and the client reverses.

    Two cursors, and they page in opposite directions:
      `before`  older, for "load older" — pass the previous `next_before`.
      `after`   newer, for the live tail — pass the previous `next_after`.

    The counts are over the SCANNED WINDOW, not the whole file, and `scanned`
    says so — a viewer that reports "3 errors" while having read the last 512KB
    of an unbounded file would be stating something it didn't check.
    """
    path = resolve(key, stream)
    if path is None:
        return None

    size = path.stat().st_size
    skipped = False
    if after is not None:
        text, base, end, skipped = _read_forward(path, after)
    else:
        text, base, end = _read_window(path, end=before)
    entries = _parse_window(text, base)
    counts: dict[str, int] = {}
    for entry in entries:
        counts[entry["level"]] = counts.get(entry["level"], 0) + 1

    selected = _select(entries, level, query)
    limit = max(1, min(limit, MAX_LIMIT))
    # Keep the NEWEST `limit` entries: on a live-tail read the newest are the
    # point, and on a backwards page the oldest returned becomes the next
    # cursor, so trimming the old end never skips anything.
    page = selected[-limit:]
    # Page backwards from the oldest entry actually returned, so nothing between
    # this page and the next is skipped even when the window held more than
    # `limit` entries or the filter dropped the rows in between.
    oldest = page[0]["offset"] if page else base
    return {
        "key": key,
        "stream": stream,
        "path": path.name,
        "size": size,
        "entries": [_finalize(e) | {"context": e["context"]} for e in page],
        "counts": counts,
        "scanned": {"from": base, "to": end, "entries": len(entries),
                    "complete": base == 0, "skipped": skipped},
        "matched": len(selected),
        "next_before": oldest if oldest > 0 else None,
        # Where a live tail should resume. Always the end of the bytes actually
        # consumed — never the file size — so a line still being written is read
        # once, on the poll after it is complete.
        "next_after": end,
    }


def main() -> int:
    import sys
    if len(sys.argv) < 2:
        for entry in list_logs():
            streams = ", ".join(
                f"{name} {info['size'] // 1024}KB" for name, info in entry["streams"].items())
            print(f"{entry['key']:<28} {streams}")
        return 0
    data = read_log(sys.argv[1], stream=sys.argv[2] if len(sys.argv) > 2 else "log", limit=40)
    if data is None:
        print(f"unknown log: {sys.argv[1]}")
        return 1
    for entry in data["entries"]:
        print(f"{entry['ts']} [{entry['level'].upper()}] {entry['msg'][:160]}")
        for line in entry["extra"][:3]:
            print(f"    | {line[:150]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
