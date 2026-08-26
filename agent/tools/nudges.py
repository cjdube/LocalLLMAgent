"""Read back the nudges tasks/daily_synthesis.py has pushed — the "these line up"
suggestions Wren sends every morning.

The push is a one-shot alert: it scrolls off the phone and there is no way to
ask what it said. The durable copy is a dated Markdown file in SYNTHESIS_DIR
(`<vault>/nudges`, deliberately outside the vault's `raw/` ingest queue — see
tasks/daily_synthesis.py's docstring), which until this module nothing could
read. agent/tools/wiki.py is scoped to `<vault>/wiki/`, so Wren could see the
whole notes wiki and none of her own suggestions.

This module owns the directory resolution (DEFAULT_SYNTHESIS_DIR,
_synthesis_dir) for both sides: daily_synthesis imports it from here so the
writer and the reader can't drift onto different paths.

Read-only. A missing directory is surfaced as an error dict rather than raising,
like wiki.py, so a misconfigured SYNTHESIS_DIR degrades to "no nudges" instead
of breaking the chat turn that asked.

Usage:
    python -m agent.tools.nudges --days 30
"""

import argparse
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from agent import prefs
from agent.dates import local_timezone
from agent.tools._http import load_env, print_result

# Whose nudges these are, for the model-facing description below.
_NAME = prefs.user_name()

load_env()

DEFAULT_SYNTHESIS_DIR = str(Path.home() / "Vaults" / "llm-wiki-learnings" / "nudges")

# The window the tool looks back over when the caller doesn't say. Two weeks is
# long enough to cover "what have you been suggesting lately" and short enough
# that the answer stays readable at ~2 nudges a day.
DEFAULT_DAYS = 14

# Bounds on the requested window. The floor stops a 0 or negative from silently
# meaning "nothing"; the ceiling keeps a chat turn from pulling a year of the
# archive into the small model's context.
MIN_DAYS = 1
MAX_DAYS = 90

# How many rows the chat path returns. At the 90-day ceiling the archive ran 36
# rows and 8938 chars — over the loop's 8000 cap — because every nudge appears
# twice, raw and inside `summary`. daily_synthesis passes max_rows=None.
MAX_CHAT_ROWS = 25

# The budget that actually bounds it. 25 rows assumed short nudges; real ones
# run long enough that 25 reached 9480 chars. Each row is also paid for TWICE —
# once raw in `nudges`, once rendered into `summary` — so the accounting below
# doubles it. Fifth tool in this pass to need a char budget beside its row cap.
MAX_CHAT_CHARS = 5000

# The filename daily_synthesis writes: `Daily-Synthesis-<YYYY-MM-DD>.md`. The
# date comes from here rather than from anything inside the file — Python owns
# date math (CLAUDE.md), and the writer already resolved the local calendar day.
FILE_PREFIX = "Daily-Synthesis-"
_FILENAME_RE = re.compile(rf"^{re.escape(FILE_PREFIX)}(\d{{4}}-\d{{2}}-\d{{2}})\.md$")


def _synthesis_dir() -> Path:
    """Read at call time, like learnings_file._learnings_dir(), so a .env edit
    (and the test suite's monkeypatch) takes effect without reimporting."""
    return Path(os.getenv("SYNTHESIS_DIR", DEFAULT_SYNTHESIS_DIR)).expanduser()


def _today() -> date:
    """Today in the local zone. The archive's filenames are local calendar days
    (agent/activity_log.prior_day resolves in local tz), so the cutoff has
    to be computed in the same zone — comparing them against a UTC "today" would
    shift the window by a day for part of every evening."""
    return datetime.now(ZoneInfo(local_timezone())).date()


def _parse_file(path: Path) -> list[str]:
    """The `- ` bullet lines of one archive file. Same shape daily_synthesis
    wrote (parse_nudges), so the `## Synthesis Suggestions` heading and any
    blank lines fall out naturally. An unreadable file contributes nothing."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    return [line.strip()[2:].strip()
            for line in text.splitlines()
            if line.strip().startswith("- ") and line.strip()[2:].strip()]


def _render(rows: list, days: int, total: int = None) -> str:
    """The rows as a finished answer the model relays instead of composing.

    Measured against the live model on the real 16-row archive, not precautionary.
    Told to write the rows out itself, it paraphrased on 1 of 3 runs — answering
    with "The daily synthesis note on 'screenwatcher' fits your automation
    interests", a suggestion that was never sent. That is the failure the
    description's anti-fabrication clause exists to prevent, and asking a small
    model to transcribe a long list is what induces it (CLAUDE.md: don't make the
    model copy content Python can render). Relaying this block instead: 6 of 6
    runs reproduced all 16 rows verbatim, the only difference being a curly
    apostrophe normalized to a straight one."""
    if not rows:
        return (f"Nothing was suggested in the last {days} days. That is normal — "
                "most days produce no nudge at all.")
    lines = [f"- **{row['date']}** — {row['text']}" for row in rows]
    # `total` is the count before the max_rows slice; len(rows) alone would
    # state the shown count as the real one (see push_log._render).
    if total is None or total == len(rows):
        header = f"{len(rows)} suggestion(s) from the last {days} days:"
    else:
        header = (f"The {len(rows)} most recent of {total} suggestion(s) from the "
                  f"last {days} days — ask about a shorter window to see them grouped:")
    return header + "\n" + "\n".join(lines)


def list_nudges(days: int = DEFAULT_DAYS, max_rows: int | None = MAX_CHAT_ROWS) -> dict:
    """The nudges pushed in the last `days` days, newest first, as flat
    {date, text} rows, plus `summary` — the same rows already formatted (see
    _render), which is what the model is told to reply with.

    Flat rather than grouped by day because both callers want it that way:
    _render walks it once, and daily_synthesis only needs the text to compare
    against. `days` is clamped to MIN_DAYS..MAX_DAYS.

    `max_rows` is Python-only, absent from TOOL_SCHEMA, and defaults to capped.
    The payload carries every nudge twice — once raw, once inside `summary` — so
    at the 90-day ceiling the model can ask for, it reached 8938 chars against
    the loop's 8000 cap. `summary` was the LAST key, so the trim ate the end of
    the one block that exists to stop the model inventing nudges it never sent.
    tasks/daily_synthesis.py passes max_rows=None: it reads `nudges` to suppress
    repeats and needs every row, and never goes near a context window.

    A day with no file (nothing genuine to say — the common case) simply isn't
    in the result. A missing directory is an error dict."""
    try:
        days = max(MIN_DAYS, min(MAX_DAYS, int(days)))
    except (TypeError, ValueError):
        days = DEFAULT_DAYS

    directory = _synthesis_dir()
    if not directory.is_dir():
        return {"error": f"nudge archive not found (check SYNTHESIS_DIR): {directory}"}

    cutoff = _today() - timedelta(days=days)
    rows = []
    for path in directory.glob(f"{FILE_PREFIX}*.md"):
        match = _FILENAME_RE.match(path.name)
        if not match:
            continue
        try:
            day = date.fromisoformat(match.group(1))
        except ValueError:
            continue
        if day < cutoff:
            continue
        rows.extend({"date": match.group(1), "text": text} for text in _parse_file(path))

    # Newest first: the last thing she said is what's usually being asked about.
    rows.sort(key=lambda r: r["date"], reverse=True)
    total = len(rows)
    if max_rows is not None:
        kept, used = [], 0
        for row in rows[:max_rows]:
            used += len(str(row)) * 2  # raw row + its rendered copy in `summary`
            if used > MAX_CHAT_CHARS and kept:
                break
            kept.append(row)
        rows = kept
    # summary leads, for the same reason it does in push_log: it's what the
    # model relays, so it's what has to survive a trim. The raw rows are the
    # redundant half.
    return {"summary": _render(rows, days, total), "total": total,
            "shown": len(rows), "days": days, "nudges": rows}


TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "list_nudges",
        # Registry-style "what exists?" tool, so the wording carries the same
        # weight it does in games.py: asked what she has been suggesting, the
        # model has a plausible answer available from the conversation and from
        # pretraining, and a fabricated suggestion is shaped exactly like a real
        # one. Hence the flat statement that the list is not something it knows.
        "description": (
            f"List the suggestions (nudges) Wren has pushed to {_NAME} from the daily "
            "synthesis — the 'you looked at X yesterday, it fits your Y note' "
            f"connections. Call this whenever {_NAME} asks what you have suggested, "
            "recommended, noticed or nudged him about, or refers back to a suggestion. "
            "This list is NOT something you know: only the nudges this tool returns "
            "were ever sent. Never invent, paraphrase or reconstruct a suggestion you "
            "did not get back from the tool. Reply with the tool's `summary` field "
            "exactly as it comes back — it is the finished answer, already listing "
            "every suggestion with its date. Do not retype the list yourself, do not "
            "shorten it, and do not describe what it was about; the suggestions ARE "
            "the answer. `summary` also carries the wording for an empty window, "
            "which is normal — most days produce no nudge at all."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": (
                        f"How many days back to look. Defaults to {DEFAULT_DAYS}; "
                        f"capped at {MAX_DAYS}."
                    ),
                },
            },
        },
    },
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS)
    args = parser.parse_args()
    return print_result(list_nudges(days=args.days))


if __name__ == "__main__":
    sys.exit(main())
