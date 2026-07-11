"""Tool-calling agent loop against a local Ollama model.

Drives a conversation: send messages + tool schemas to Ollama, dispatch any
tool_calls to local Python functions, feed results back, repeat until the
model returns a final text response or the iteration cap is hit.
"""

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Callable, Optional

import requests
from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / "config" / ".env")

# Bounds the number of model round-trips in one agent turn. 6 was enough for
# the original single-fetch tools, but navigating the learnings wiki
# (read_wiki_index -> a few read_wiki_page calls -> answer) legitimately chains
# more reads, so allow more headroom before giving up.
MAX_TOOL_ITERATIONS = 10

# Cap the size of a single tool result before it's appended to the conversation.
# One oversized result (e.g. a web search dumping page after page of listings)
# can otherwise push the prompt past num_ctx, at which point Ollama silently
# truncates the FRONT of the conversation (dropping the system prompt) and the
# model tends to run away in a repetition loop. Trimming the result keeps the
# window intact. ~8000 chars is roughly 2000 tokens — big enough for a useful
# result, small enough that a few of them still fit a 16k window.
MAX_TOOL_RESULT_CHARS = int(os.getenv("OLLAMA_MAX_TOOL_RESULT_CHARS", "8000"))


class TurnCancelled(Exception):
    """Raised inside advance()/_ollama_chat when the caller's should_cancel()
    reports the running turn was cancelled. The caller rolls back the partial
    turn and reports it as stopped (see chat/server.py's /chat handlers)."""


def load_persona(filename: str) -> str:
    """Load a persona/context markdown file from agent/, stripping HTML
    comments (those are notes for maintainers, not the model)."""
    try:
        raw = (Path(__file__).resolve().parent / filename).read_text()
        return re.sub(r"<!--.*?-->", "", raw, flags=re.DOTALL).strip()
    except FileNotFoundError:
        return ""


WREN_CORE = load_persona("wren.md")
CRAIG_CONTEXT = load_persona("identity.md")


def with_identity(system_prompt: str) -> str:
    # Memories are rendered at call time (not import) so a fact saved mid-session
    # is present in the next conversation's system prompt.
    from agent.tools.memory import render_memory_block

    parts = [p for p in (WREN_CORE, CRAIG_CONTEXT, render_memory_block(), system_prompt) if p]
    return "\n\n---\n\n".join(parts)


def _ollama_chat(
    messages: list[dict],
    model: str = None,
    host: str = None,
    tools: Optional[list[dict]] = None,
    timeout: float = None,
    logger: Optional[logging.Logger] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> dict:
    """POST a chat completion to Ollama and return the reassembled response
    `message` dict. Centralizes model/host/timeout env defaulting so advance()
    and complete_text() stay in sync.

    Streams (`stream=True`) so a runaway generation can be interrupted: after
    each chunk we consult `should_cancel` and, if it fires, close the stream
    (which tells Ollama to stop generating) and raise TurnCancelled. This model
    emits a chunk roughly per token, so a cancel lands within a token of being
    requested. The response is reassembled from the streamed chunks — content
    concatenated, tool_calls collected — so callers see the same shape as a
    non-streamed reply.

    With streaming, `timeout` is the read timeout *between* chunks, not a cap on
    total generation: a healthy stream keeps the socket fed, while a wedged
    runner (no bytes at all) still trips it. Overridable via OLLAMA_TIMEOUT or
    per-call `timeout` — a large prompt can spend tens of seconds in prefill
    before the first chunk, so the default stays generous."""
    model = model or os.getenv("OLLAMA_MODEL", "gemma4")
    host = host or os.getenv("OLLAMA_HOST", "http://localhost:11434")
    if timeout is None:
        timeout = float(os.getenv("OLLAMA_TIMEOUT", "300"))
    # Set num_ctx explicitly — otherwise Ollama falls back to a small default
    # (~4096) and silently truncates the FRONT of the prompt, where Wren's
    # system prompt (identity + tool-use rules) lives.
    num_ctx = int(os.getenv("OLLAMA_NUM_CTX", "8192"))

    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        # Keep the (large, slow-to-load) model resident between calls so the
        # next one doesn't pay the cold-load cost inside its read-timeout window.
        "keep_alive": os.getenv("OLLAMA_KEEP_ALIVE", "30m"),
        "options": {"num_ctx": num_ctx},
    }
    if tools is not None:
        payload["tools"] = tools

    content_parts: list[str] = []
    tool_calls: list[dict] = []
    prompt_tokens = eval_tokens = None
    with requests.post(f"{host}/api/chat", json=payload, timeout=timeout, stream=True) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines():
            if should_cancel is not None and should_cancel():
                raise TurnCancelled()
            if not line:
                continue
            chunk = json.loads(line)
            msg = chunk.get("message") or {}
            if msg.get("content"):
                content_parts.append(msg["content"])
            if msg.get("tool_calls"):
                tool_calls.extend(msg["tool_calls"])
            if chunk.get("done"):
                # The terminal chunk carries the token accounting.
                prompt_tokens = chunk.get("prompt_eval_count")
                eval_tokens = chunk.get("eval_count")

    message: dict = {"role": "assistant", "content": "".join(content_parts)}
    if tool_calls:
        message["tool_calls"] = tool_calls

    if logger:
        # prompt_eval_count is the actual prompt size Ollama processed; compare
        # it to num_ctx to catch (and flag) likely front-truncation.
        logger.info(
            "ollama_chat model=%s num_ctx=%d prompt_tokens=%s eval_tokens=%s",
            model, num_ctx, prompt_tokens, eval_tokens,
        )
        if isinstance(prompt_tokens, int) and prompt_tokens >= num_ctx:
            logger.warning(
                "ollama prompt (%d tokens) reached num_ctx=%d — the front of "
                "the conversation (system prompt) was likely truncated",
                prompt_tokens, num_ctx,
            )
    return message


def warm_model(
    model: str = None,
    host: str = None,
    timeout: float = None,
    logger: Optional[logging.Logger] = None,
) -> bool:
    """Force-load the model into Ollama's memory before a heavy generation.

    A cold local model (gemma4:26b-mlx is ~17GB) can't emit its first streamed
    chunk until it's loaded AND the prompt is prefilled; stacked together on a
    cold start these exceed the streamed call's read timeout, so a big-prompt
    task like weekly_learnings times out before any token arrives. Loading the
    model first — with the SAME num_ctx and keep_alive, so the real call reuses
    this resident instance rather than reloading — moves the 17GB load out of
    that window and leaves only prefill.

    Sends an empty-messages /api/chat, which Ollama loads-and-returns without
    generating. Gets its own generous timeout (OLLAMA_WARM_TIMEOUT, default
    600s) because the load is the slow part. Degrades to a warning and returns
    False on failure — the caller still attempts the generation cold."""
    model = model or os.getenv("OLLAMA_MODEL", "gemma4")
    host = host or os.getenv("OLLAMA_HOST", "http://localhost:11434")
    if timeout is None:
        timeout = float(os.getenv("OLLAMA_WARM_TIMEOUT", "600"))
    num_ctx = int(os.getenv("OLLAMA_NUM_CTX", "8192"))
    payload = {
        "model": model,
        "messages": [],
        "stream": False,
        "keep_alive": os.getenv("OLLAMA_KEEP_ALIVE", "30m"),
        "options": {"num_ctx": num_ctx},
    }
    try:
        t0 = time.monotonic()
        resp = requests.post(f"{host}/api/chat", json=payload, timeout=timeout)
        resp.raise_for_status()
        if logger:
            logger.info("warm_model loaded %s in %.1fs", model, time.monotonic() - t0)
        return True
    except Exception as e:
        if logger:
            logger.warning("warm_model failed (%s); attempting generation cold", e)
        return False


# Argument keys that may carry secrets — redacted before they reach the logs.
_SENSITIVE_ARG_KEYS = ("api_key", "token", "secret", "password", "credential")


def _redact_args(args) -> dict:
    if not isinstance(args, dict):
        return args
    return {
        k: ("***" if any(s in k.lower() for s in _SENSITIVE_ARG_KEYS) else v)
        for k, v in args.items()
    }


def _execute_tool_call(
    call: dict,
    dispatch: dict[str, Callable[..., dict]],
    messages: list[dict],
    logger: Optional[logging.Logger],
) -> None:
    fn_name = call["function"]["name"]
    fn_args = call["function"].get("arguments", {})
    fn = dispatch.get(fn_name)
    if fn is None:
        result = {"error": f"unknown tool '{fn_name}'"}
    else:
        try:
            result = fn(**fn_args)
        except Exception as e:
            result = {"error": f"tool '{fn_name}' raised: {e}"}
    content = json.dumps(result)
    if len(content) > MAX_TOOL_RESULT_CHARS:
        dropped = len(content) - MAX_TOOL_RESULT_CHARS
        content = content[:MAX_TOOL_RESULT_CHARS] + f"... [truncated {dropped} chars to fit the context window]"
        if logger:
            logger.warning(
                "tool_call %s result trimmed: %d chars over the %d cap",
                fn_name, dropped, MAX_TOOL_RESULT_CHARS,
            )
    if logger:
        logger.info(f"tool_call {fn_name}({_redact_args(fn_args)}) -> {content}")
    messages.append({"role": "tool", "content": content})


def advance(
    messages: list[dict],
    tools: list[dict],
    dispatch: dict[str, Callable[..., dict]],
    model: str = None,
    host: str = None,
    logger: Optional[logging.Logger] = None,
    confirm_before: frozenset[str] = frozenset(),
    should_cancel: Optional[Callable[[], bool]] = None,
) -> dict:
    """Advance a tool-calling conversation already seeded in `messages`
    (system + user turns, and any prior assistant/tool turns).

    tools: list of OpenAI-style tool schemas (see agent/tools/*.py TOOL_SCHEMA).
    dispatch: {function_name: callable} mapping — each callable takes the
              parsed arguments dict via **kwargs and returns a
              JSON-serializable dict.

    Sends to Ollama, auto-executing any tool_calls whose name is NOT in
    confirm_before (appending assistant/tool messages to `messages` in
    place as it goes — same behavior as before this was extracted). Stops
    and returns either:
        {"type": "final", "text": <model's final text>}
    or, the moment a tool_call's name IS in confirm_before:
        {"type": "confirm", "call": <the tool_call dict>}
    without executing it. The assistant message containing that tool_call
    has already been appended to `messages` — call resolve() to append its
    result, then call advance() again to continue. (Assumes at most one
    tool_call needing confirmation is acted on per turn; any tool_calls
    after it in the same batch are picked up on the next advance() once
    resolved, consistent with how this model calls tools one at a time in
    practice.)
    """
    for _ in range(MAX_TOOL_ITERATIONS):
        if should_cancel is not None and should_cancel():
            raise TurnCancelled()
        message = _ollama_chat(
            messages, model=model, host=host, tools=tools, logger=logger,
            should_cancel=should_cancel,
        )
        messages.append(message)

        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            return {"type": "final", "text": message.get("content", "")}

        # This loop pauses at the first confirm-gated call and returns; any
        # calls after it in the same batch are dropped (they aren't replayed
        # once the write resolves). Harmless while the model emits one call at
        # a time, but a stronger tool-caller could batch, so make the drop
        # visible rather than silent — see advance()'s docstring caveat.
        if logger and len(tool_calls) > 1 and any(
            c["function"]["name"] in confirm_before for c in tool_calls
        ):
            names = [c["function"]["name"] for c in tool_calls]
            logger.warning(
                "advance() got %d tool calls in one turn including a "
                "confirm-gated call; only the first confirm-gated call is "
                "acted on and later calls in this batch are dropped: %s",
                len(tool_calls),
                names,
            )

        for call in tool_calls:
            if call["function"]["name"] in confirm_before:
                return {"type": "confirm", "call": call}
            _execute_tool_call(call, dispatch, messages, logger)

    raise RuntimeError(f"agent loop exceeded MAX_TOOL_ITERATIONS={MAX_TOOL_ITERATIONS} without a final answer")


def resolve(
    messages: list[dict],
    call: dict,
    approved: bool,
    dispatch: dict[str, Callable[..., dict]],
    logger: Optional[logging.Logger] = None,
) -> None:
    """Append the result of a tool_call previously paused by advance()
    (returned as {"type": "confirm", "call": call}) to `messages`. Call
    advance() again afterward to continue the conversation."""
    if approved:
        _execute_tool_call(call, dispatch, messages, logger)
    else:
        if logger:
            logger.info(f"tool_call {call['function']['name']} declined by user")
        messages.append(
            {"role": "tool", "content": json.dumps({"error": "user declined this action"})}
        )


def complete_text(
    system_prompt: str,
    user_prompt: str,
    model: str = None,
    host: str = None,
    timeout: float = None,
    logger: Optional[logging.Logger] = None,
) -> str:
    """Single-turn, tool-free completion — for tasks like writing a short
    summary paragraph where the caller assembles the surrounding structure
    itself rather than trusting the model to produce it."""
    message = _ollama_chat(
        [
            {"role": "system", "content": with_identity(system_prompt)},
            {"role": "user", "content": user_prompt},
        ],
        model=model,
        host=host,
        timeout=timeout,
        logger=logger,
    )
    return message.get("content", "").strip()
