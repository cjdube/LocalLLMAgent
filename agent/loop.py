"""Tool-calling agent loop against a local Ollama model.

Drives a conversation: send messages + tool schemas to Ollama, dispatch any
tool_calls to local Python functions, feed results back, repeat until the
model returns a final text response or the iteration cap is hit.
"""

import json
import logging
import os
import re
from pathlib import Path
from typing import Callable, Optional

import requests
from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / "config" / ".env")

MAX_TOOL_ITERATIONS = 6


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
    parts = [p for p in (WREN_CORE, CRAIG_CONTEXT, system_prompt) if p]
    return "\n\n---\n\n".join(parts)


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
    if logger:
        logger.info(f"tool_call {fn_name}({fn_args}) -> {json.dumps(result)}")
    messages.append({"role": "tool", "content": json.dumps(result)})


def advance(
    messages: list[dict],
    tools: list[dict],
    dispatch: dict[str, Callable[..., dict]],
    model: str = None,
    host: str = None,
    logger: Optional[logging.Logger] = None,
    confirm_before: frozenset[str] = frozenset(),
) -> dict:
    """Advance a tool-calling conversation already seeded in `messages`
    (system + user turns, and any prior assistant/tool turns).

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
    model = model or os.getenv("OLLAMA_MODEL", "gemma4")
    host = host or os.getenv("OLLAMA_HOST", "http://localhost:11434")

    for _ in range(MAX_TOOL_ITERATIONS):
        resp = requests.post(
            f"{host}/api/chat",
            json={"model": model, "messages": messages, "tools": tools, "stream": False},
            timeout=120,
        )
        resp.raise_for_status()
        message = resp.json()["message"]
        messages.append(message)

        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            return {"type": "final", "text": message.get("content", "")}

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


def run_agent(
    system_prompt: str,
    user_prompt: str,
    tools: list[dict],
    dispatch: dict[str, Callable[..., dict]],
    model: str = None,
    host: str = None,
    logger: Optional[logging.Logger] = None,
) -> str:
    """Run the tool-calling loop and return the model's final text response.

    tools: list of OpenAI-style tool schemas (see agent/tools/*.py TOOL_SCHEMA).
    dispatch: {function_name: callable} mapping — callable takes the parsed
              arguments dict via **kwargs and returns a JSON-serializable dict.
    """
    messages = [
        {"role": "system", "content": with_identity(system_prompt)},
        {"role": "user", "content": user_prompt},
    ]
    result = advance(messages, tools, dispatch, model=model, host=host, logger=logger)
    return result["text"]


def complete_text(
    system_prompt: str,
    user_prompt: str,
    model: str = None,
    host: str = None,
) -> str:
    """Single-turn, tool-free completion — for tasks like writing a short
    summary paragraph where the caller assembles the surrounding structure
    itself rather than trusting the model to produce it."""
    model = model or os.getenv("OLLAMA_MODEL", "gemma4")
    host = host or os.getenv("OLLAMA_HOST", "http://localhost:11434")

    resp = requests.post(
        f"{host}/api/chat",
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": with_identity(system_prompt)},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
        },
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()["message"].get("content", "").strip()
