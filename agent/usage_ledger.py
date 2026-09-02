"""Append-only record of every model call, one JSON object per line.

The token counts were always there — `agent/loop.py` has logged
`ollama_chat ... prompt_tokens=N eval_tokens=N` on every call for months — but
only as prose inside a log that a RotatingFileHandler eventually throws away.
This module keeps the same numbers as data instead, so "how much is Wren
actually spending on models, and on what?" is a question with an answer.

One row per model call. The writer lives behind `agent/loop.py:_llm_chat`, the
single seam both backends already pass through, so there is exactly one call
site and no backend can be instrumented and then quietly forgotten.

Two properties this file must keep:

  * It never raises. Accounting is not worth a failed chat turn, so `record()`
    swallows everything — a lost row is strictly better than a lost reply.
  * It never grows without bound. `logs/` is not blanket-gitignored and this
    file is written on every single model call, so it prunes itself on write
    (see `_prune_if_large`).

Deliberately `.jsonl`, not `.log`: `chat/logview.py` globs `logs/*.log` to build
the log viewer's catalogue, and a machine-readable ledger has no business
showing up there as a "log" a human is invited to tail.
"""

import json
import logging
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from agent.store import locked

_ROOT = Path(__file__).resolve().parent.parent

# Resolved exactly the way tasks/_common.py:LOGS_DIR is, and for the same
# reason: the env var is the one redirect that survives into a child
# interpreter, which is what makes tests/conftest.py's isolation hold across a
# subprocess. Copied rather than imported — agent/ importing tasks/ at module
# scope would invert the dependency the two packages are arranged around.
LOGS_DIR = Path(os.getenv("WREN_LOGS_DIR") or _ROOT / "logs")
LEDGER_PATH = LOGS_DIR / "usage.jsonl"

logger = logging.getLogger(__name__)


def _retention_days() -> int:
    try:
        return int(os.getenv("WREN_USAGE_RETENTION_DAYS", "90"))
    except ValueError:
        return 90


def _max_bytes() -> int:
    try:
        return int(os.getenv("WREN_USAGE_MAX_BYTES", "5000000"))
    except ValueError:
        return 5_000_000


# USD per MILLION tokens, as (input, output), keyed by model-name prefix so a
# pinned version ("gemini-2.5-flash-preview-09-2025") matches its family. Longest
# prefix wins.
#
# THESE RATES GO STALE. They are a hand-maintained convenience, not a billing
# record: check the provider's own pricing page before trusting a total, and the
# provider's console for what was actually charged. A model that matches nothing
# here records cost_usd=None and is counted separately in the summary — it must
# never be silently priced at zero, which would read as "free" rather than
# "unknown".
#
# Local models are genuinely free at the point of use (the Mac mini is already
# paid for), so the ollama backend short-circuits to 0.0 without consulting this
# table at all.
_PRICES = {
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-pro": (1.25, 10.00),
    "gemini-3.6-flash": (0.30, 2.50),
    "gemini-3.7-flash": (0.30, 2.50),
}


def estimate_cost(backend: str, model: str, prompt_tokens, output_tokens) -> float | None:
    """USD for one call, or None when the model isn't in `_PRICES`.

    Thinking tokens are NOT added on top: every provider here already counts
    them inside its output-token total, so adding them again would double-bill
    exactly the calls that reason the most.
    """
    if (backend or "").lower() == "ollama":
        return 0.0
    name = (model or "").strip()
    match = None
    for prefix in _PRICES:
        if name.startswith(prefix) and (match is None or len(prefix) > len(match)):
            match = prefix
    if match is None:
        return None
    in_rate, out_rate = _PRICES[match]
    prompt = prompt_tokens if isinstance(prompt_tokens, int) else 0
    output = output_tokens if isinstance(output_tokens, int) else 0
    return round((prompt * in_rate + output * out_rate) / 1_000_000, 6)


def _prune_if_large(path: Path) -> None:
    """Drop rows older than the retention window once the file gets big.

    Size is the trigger and age is the rule, on purpose. Checking the size is
    one stat() on every call; rewriting a 5MB file is not, so a busy day pays
    the rewrite about once and a quiet one never pays it at all. Callers hold
    the lock.
    """
    try:
        if path.stat().st_size <= _max_bytes():
            return
    except OSError:
        return
    cutoff = (datetime.now() - timedelta(days=_retention_days())).isoformat()
    kept = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            # A row whose ts is missing or unreadable is kept: the point of the
            # prune is to shed old rows, and "I can't tell how old this is" is
            # not evidence that it is old.
            try:
                ts = json.loads(line).get("ts") or ""
            except ValueError:
                kept.append(line)
                continue
            if not ts or ts >= cutoff:
                kept.append(line)
    # Same shape as agent/store.py:atomic_write_json — a dot-prefixed mkstemp
    # beside the target, replaced into place, and unlinked if anything throws.
    # `path.with_suffix(".jsonl.tmp")` left a visible, untracked, multi-megabyte
    # logs/usage.jsonl.tmp behind on a crash mid-prune, with nothing to clean it
    # up and no .gitignore rule covering it.
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("".join(kept))
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def record(
    agent: str,
    task: str,
    backend: str,
    model: str,
    *,
    prompt_tokens=None,
    output_tokens=None,
    thinking_tokens=None,
    num_ctx=None,
    duration_ms=None,
    finish_reason=None,
    caller=None,
    tools_offered=None,
    ok: bool = True,
    error=None,
    cost_usd=None,
) -> None:
    """Append one row for one model call. Never raises.

    `cost_usd` is only passed by callers that were told the price by the
    provider (the Claude Code runs in tasks/build_worker.py report their own
    total_cost_usd); everyone else leaves it None and gets `estimate_cost`.

    The timestamp is naive local time, matching what `logging` writes into the
    run logs beside it. That is deliberate — both this file and the reader in
    chat/usage.py are local-only, so there is no UTC boundary to cross and
    converting would introduce the very skew the AGENTS.md timezone rule exists
    to prevent.
    """
    try:
        if cost_usd is None:
            cost_usd = estimate_cost(backend, model, prompt_tokens, output_tokens)
        row = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "agent": agent,
            "task": task,
            "caller": caller,
            "backend": backend,
            "model": model,
            "prompt_tokens": prompt_tokens,
            "output_tokens": output_tokens,
            "thinking_tokens": thinking_tokens,
            "num_ctx": num_ctx,
            "duration_ms": duration_ms,
            "finish_reason": finish_reason,
            "tools_offered": tools_offered,
            "ok": ok,
            "error": error,
            "cost_usd": cost_usd,
        }
        LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
        # The chat server, bg_worker and every launchd task write this same
        # file, so the append is serialized across processes, not just threads.
        with locked(LEDGER_PATH):
            with LEDGER_PATH.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")
            _prune_if_large(LEDGER_PATH)
    except Exception:
        # Debug, not warning: this fires on every call if it fires at all, and a
        # broken ledger must not drown the log that the actual work writes to.
        logger.debug("usage_ledger.record failed", exc_info=True)
