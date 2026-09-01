"""Reads the usage ledgers and aggregates them for the /activity page.

Sits to `agent/usage_ledger.py` as `chat/insights.py` sits to the task logs: the
writer knows nothing about presentation, and every bit of parsing, path
resolution and arithmetic lives here rather than in the Flask edge.

Three ledgers, one page. Wren writes `logs/usage.jsonl`; ScribeJay and
ObsidianWikiAgent each write their own inside their own checkout, and this reads
all three through `insights._external_roots()` — the same WREN_EXTERNAL_TASK_ROOTS
mechanism that already federates their launchd runs onto the dashboard. Knowledge
still runs one way: this reads their files as data and neither sibling knows Wren
exists. A sibling that has not been instrumented yet simply has no file, which
reads as no rows.

Timestamps are naive local, written by us, and are compared against a naive
local `now`. That is on purpose and matches insights.run_stats — the AGENTS.md
UTC rule is about feeds that hand us UTC, and a file we wrote ourselves is not
one of those.
"""

import json
import threading
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median

from chat import insights

LEDGER_NAME = "usage.jsonl"

# Display names for the agents whose ledgers this page shows. The key is the
# short name from WREN_EXTERNAL_TASK_ROOTS (plus "wren" for the local one); an
# unlisted root falls back to str.title(), which is right for most and wrong for
# an internally-capitalised name — the same reason insights._SOURCE_TITLES exists.
_AGENT_TITLES = {"wren": "Wren", "scribejay": "ScribeJay", "wiki": "Wiki"}

# Reading and parsing every ledger on each poll is the same cost shape
# parse_runs has, so it gets the same treatment: cache on the files' (mtime,
# size), which invalidates for free the moment any agent appends a row. The lock
# is because the chat server runs Flask threaded.
_CACHE: dict[str, tuple] = {}
_CACHE_LOCK = threading.Lock()


def agent_title(name: str) -> str:
    return _AGENT_TITLES.get(name, name.title())


def _ledger_paths() -> list[tuple[str, Path]]:
    """[(agent_name, ledger_path)] for every agent that could have written one.

    Paths are returned whether or not the file exists — the caller reads what is
    there, and a missing sibling ledger is "that agent has not been instrumented
    yet", never an error.
    """
    paths = [("wren", insights.LOGS_DIR / LEDGER_NAME)]
    for name, root, _prefix in insights._external_roots():
        paths.append((name, root / "logs" / LEDGER_NAME))
    return paths


def _signature(paths: list[tuple[str, Path]]) -> tuple:
    sig = []
    for _name, path in paths:
        try:
            st = path.stat()
        except OSError:
            continue
        sig.append((str(path), st.st_mtime_ns, st.st_size))
    return tuple(sig)


def _read_ledger(agent: str, path: Path, cutoff: str) -> list[dict]:
    """Rows from one ledger newer than `cutoff` (an ISO timestamp string).

    The comparison is on the string, not a parsed datetime: ISO-8601 sorts
    lexicographically, the file is append-ordered, and skipping the parse for
    rows we are about to discard is the difference between reading a year of
    history cheaply and not.

    `agent` is stamped from the file the row came from rather than trusted from
    inside it, so a sibling that copies the writer without changing its agent
    name still lands in the right bucket.
    """
    rows = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    # One malformed line (a half-written row from a killed
                    # process) must not cost the whole file.
                    continue
                if not isinstance(row, dict) or (row.get("ts") or "") < cutoff:
                    continue
                row["agent"] = agent
                rows.append(row)
    except OSError:
        return []
    return rows


def read_rows(days: int = 7, now: datetime | None = None) -> list[dict]:
    """Every model call from every agent in the last `days`, oldest first."""
    paths = _ledger_paths()
    cutoff = ((now or datetime.now()) - timedelta(days=days)).isoformat(timespec="seconds")
    key = f"{days}:{cutoff[:13]}"
    sig = _signature(paths)
    with _CACHE_LOCK:
        hit = _CACHE.get(key)
        if hit and hit[0] == sig:
            return hit[1]
    rows = []
    for agent, path in paths:
        rows.extend(_read_ledger(agent, path, cutoff))
    rows.sort(key=lambda r: r.get("ts") or "")
    with _CACHE_LOCK:
        _CACHE[key] = (sig, rows)
    return rows


def _num(value) -> int:
    """A token count as a number. Any field can be null — Ollama does not always
    report a count, and a failed call has none at all — and None must total as
    zero without poisoning the sum."""
    return value if isinstance(value, int) else 0


def _bucket(totals: dict, key: str, row: dict) -> None:
    slot = totals.setdefault(key, {"tokens": 0, "prompt_tokens": 0, "output_tokens": 0,
                                   "calls": 0, "cost_usd": 0.0})
    slot["prompt_tokens"] += _num(row.get("prompt_tokens"))
    slot["output_tokens"] += _num(row.get("output_tokens"))
    slot["tokens"] += _num(row.get("prompt_tokens")) + _num(row.get("output_tokens"))
    slot["calls"] += 1
    cost = row.get("cost_usd")
    if isinstance(cost, (int, float)):
        slot["cost_usd"] += cost


def _sorted_buckets(totals: dict, name_key: str) -> list[dict]:
    out = [{name_key: name, **vals} for name, vals in totals.items()]
    out.sort(key=lambda d: d["tokens"], reverse=True)
    for entry in out:
        entry["cost_usd"] = round(entry["cost_usd"], 4)
    return out


def summarize(days: int = 7, now: datetime | None = None) -> dict:
    """Everything the /activity page draws, computed in one pass over the rows.

    `unpriced_calls` is reported alongside the cost rather than folded into it:
    a model missing from the price table records cost_usd=None, and a total that
    quietly counted those as $0 would read as "this was free" when it means
    "we don't know what this cost".
    """
    rows = read_rows(days, now=now)

    by_model: dict = {}
    by_agent: dict = {}
    by_task: dict = {}
    by_backend: dict = {}
    by_day: dict = {}
    durations: list[int] = []

    prompt_total = output_total = thinking_total = 0
    cost_total = 0.0
    unpriced = cut_off = failed = 0

    for row in rows:
        model = row.get("model") or "unknown"
        _bucket(by_model, model, row)
        _bucket(by_agent, row.get("agent") or "unknown", row)
        _bucket(by_task, row.get("task") or "unknown", row)
        _bucket(by_backend, row.get("backend") or "unknown", row)

        day = (row.get("ts") or "")[:10]
        if day:
            per_day = by_day.setdefault(day, {})
            per_day[model] = per_day.get(model, 0) + _num(row.get("prompt_tokens")) \
                + _num(row.get("output_tokens"))

        prompt_total += _num(row.get("prompt_tokens"))
        output_total += _num(row.get("output_tokens"))
        thinking_total += _num(row.get("thinking_tokens"))

        cost = row.get("cost_usd")
        if isinstance(cost, (int, float)):
            cost_total += cost
        else:
            unpriced += 1

        duration = row.get("duration_ms")
        if isinstance(duration, int):
            durations.append(duration)

        if row.get("ok") is False:
            failed += 1
        # 'length' is Ollama's word for it, MAX_TOKENS is Gemini's. Both mean the
        # reply stopped because it ran out of budget, not because it was done.
        reason = str(row.get("finish_reason") or "")
        if reason == "length" or "MAX_TOKENS" in reason:
            cut_off += 1

    # Every day in the window gets a column, including the quiet ones — a gap
    # drawn as a missing bar reads as "no data", where an empty column reads as
    # "nothing ran", which is the true and more useful statement.
    end = (now or datetime.now()).date()
    day_keys = [(end - timedelta(days=n)).isoformat() for n in range(days - 1, -1, -1)]

    # Local calls are free at the point of use, so they are counted separately
    # rather than dropped: "198 of 214 calls cost nothing" is the headline that
    # makes a small cloud bill make sense.
    local_calls = sum(1 for r in rows if (r.get("backend") or "") == "ollama")

    return {
        "days": days,
        "totals": {
            "calls": len(rows),
            "tokens": prompt_total + output_total,
            "prompt_tokens": prompt_total,
            "output_tokens": output_total,
            "thinking_tokens": thinking_total,
            "cost_usd": round(cost_total, 4),
            "unpriced_calls": unpriced,
            "local_calls": local_calls,
            "cut_off": cut_off,
            "failed": failed,
            "median_ms": round(median(durations)) if durations else None,
        },
        "by_day": [{"day": d, "models": by_day.get(d, {})} for d in day_keys],
        "by_model": _sorted_buckets(by_model, "model"),
        "by_agent": [{**b, "title": agent_title(b["agent"])}
                     for b in _sorted_buckets(by_agent, "agent")],
        "by_task": _sorted_buckets(by_task, "task"),
        "by_backend": _sorted_buckets(by_backend, "backend"),
    }
