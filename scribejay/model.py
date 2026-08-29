"""Which LLM backend ScribeJay's tasks run on.

Deliberately its OWN environment chain, separate from Wren's `WREN_*` variables:
ScribeJay is a second agent with a second model dial, and the whole reason the split
exists is so the journal can be pointed somewhere else (a free OpenRouter model,
say) without touching what chat runs on.

    SCRIBEJAY_<TASK_KEY>_BACKEND  ->  SCRIBEJAY_LLM_BACKEND  ->  None

`None` means "no opinion", which agent.loop._llm_chat resolves to local Ollama —
the local-first default.

There is deliberately NO fallback to `WREN_<TASK_KEY>_BACKEND`. A silent fallback
would hide a missed .env rename; resolving to ollama and SAYING SO in the run log
(see log_backend below) is the louder failure, and the one that matters here:
agent/activity_log.py's compaction caps are sized for a cloud model, so a task
that quietly drops to the small local model loses whole sections of its draft
rather than erroring.

Usage:
    from scribejay.model import backend, log_backend
    b = backend("daily_chrome_learnings")
    log_backend(logger, "daily_chrome_learnings", b)
"""

from scribejay import config


def backend(task_key: str) -> str | None:
    """SCRIBEJAY_<TASK_KEY>_BACKEND, else SCRIBEJAY_LLM_BACKEND, else None."""
    return (
        config.getenv(f"SCRIBEJAY_{task_key.upper()}_BACKEND")
        or config.getenv("SCRIBEJAY_LLM_BACKEND")
        or None
    )


def log_backend(logger, task_key: str, resolved: str | None) -> None:
    """Say which backend the run resolved to, and where it came from.

    Logged on every run because the failure this guards against is silent: an
    unset variable is not an error, it is just a different (smaller) model, and
    the only visible symptom is a thinner draft nobody compares against
    yesterday's."""
    if config.getenv(f"SCRIBEJAY_{task_key.upper()}_BACKEND"):
        source = f"SCRIBEJAY_{task_key.upper()}_BACKEND"
    elif config.getenv("SCRIBEJAY_LLM_BACKEND"):
        source = "SCRIBEJAY_LLM_BACKEND"
    else:
        source = "unset"
    logger.info(f"backend: {resolved or 'ollama (default)'} (from {source})")
