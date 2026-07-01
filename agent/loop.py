"""Tool-calling agent loop against a local Ollama model.

Drives a conversation: send messages + tool schemas to Ollama, dispatch any
tool_calls to local Python functions, feed results back, repeat until the
model returns a final text response or the iteration cap is hit.
"""

import json
import logging
import os
from pathlib import Path
from typing import Callable, Optional

import requests
from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / "config" / ".env")

MAX_TOOL_ITERATIONS = 6


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
    model = model or os.getenv("OLLAMA_MODEL", "gemma4")
    host = host or os.getenv("OLLAMA_HOST", "http://localhost:11434")

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    for iteration in range(MAX_TOOL_ITERATIONS):
        resp = requests.post(
            f"{host}/api/chat",
            json={"model": model, "messages": messages, "tools": tools, "stream": False},
            timeout=120,
        )
        resp.raise_for_status()
        data = resp.json()
        message = data["message"]
        messages.append(message)

        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            return message.get("content", "")

        for call in tool_calls:
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
            messages.append(
                {
                    "role": "tool",
                    "content": json.dumps(result),
                }
            )

    raise RuntimeError(f"agent loop exceeded MAX_TOOL_ITERATIONS={MAX_TOOL_ITERATIONS} without a final answer")


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
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
        },
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()["message"].get("content", "").strip()
