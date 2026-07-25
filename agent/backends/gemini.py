"""Gemini cloud backend behind agent.loop's _llm_chat seam.

Translates Wren's canonical (Ollama/OpenAI-shaped) messages and tool schemas
to/from the Gemini SDK, and reassembles a streamed reply into the same
canonical `message` dict the Ollama path returns — so agent.loop.advance() and
complete_text() are unchanged whichever backend is selected. Opt-in per the
local-first design (WREN_LLM_BACKEND / WREN_<TASK>_BACKEND=gemini).

The google.genai imports stay inside the functions so importing this module (at
agent.loop import time) never pulls the cloud SDK on the common local-only path;
TurnCancelled is imported from agent.loop lazily for the same reason (and to
keep the import one-way — agent.loop imports this module, not vice versa)."""

import base64
import json
import logging
import os
from typing import Callable, Optional

# Default cloud model when the Gemini backend is selected but no model is pinned.
GEMINI_DEFAULT_MODEL = "gemini-2.5-flash"


def _coerce_response(content) -> dict:
    """A canonical tool message carries its result as a JSON string; Gemini's
    functionResponse needs a dict. Parse it back, wrapping non-dicts."""
    if content is None:
        return {}
    if isinstance(content, dict):
        return content
    try:
        val = json.loads(content)
    except (ValueError, TypeError):
        return {"result": content}
    return val if isinstance(val, dict) else {"result": val}


def _gemini_contents(messages: list[dict]):
    """Translate canonical messages into (system_instruction, contents) for the
    Gemini SDK. System turns are hoisted out; assistant tool_calls become
    functionCall parts and each following `tool` result is paired (FIFO, within
    the emitting assistant turn) with its call name to build the matching
    functionResponse.

    The canonical `tool` message carries no call id (see _execute_tool_call), so
    pairing is positional — hence the per-turn reset below rather than a global
    FIFO queue."""
    from google.genai import types

    system_parts: list[str] = []
    contents = []
    pending_names: list[str] = []  # function-call names awaiting their tool result
    for m in messages:
        role = m.get("role")
        if role == "system":
            if m.get("content"):
                system_parts.append(m["content"])
        elif role == "user":
            contents.append(types.Content(
                role="user", parts=[types.Part.from_text(text=m.get("content") or "")]))
        elif role == "assistant":
            parts = []
            if m.get("content"):
                parts.append(types.Part.from_text(text=m["content"]))
            # Reset, don't extend: pending_names holds only the calls from THIS
            # assistant turn. advance() drops any batched calls after a
            # confirm-gated one, so a turn can emit two calls and yield one
            # result — extending would leave the orphan name in the queue and
            # every later result would pop the wrong name, silently mislabelling
            # the rest of the conversation. Resetting confines that to the turn
            # where the drop happened.
            pending_names = []
            for call in m.get("tool_calls") or []:
                fn = call["function"]
                args = fn.get("arguments") or {}
                if not isinstance(args, dict):
                    try:
                        args = json.loads(args)
                    except (ValueError, TypeError):
                        args = {"_raw": args}
                part = types.Part.from_function_call(name=fn["name"], args=args)
                # Replay the thought_signature captured in _gemini_chat: Gemini's
                # thinking models stamp each functionCall with an opaque signature and
                # reject a follow-up turn whose prior functionCall has lost it ("Function
                # call is missing a thought_signature"). Only present when this call came
                # from Gemini itself — a call replayed from another backend (e.g. an
                # escalated turn's truncated history) has none, and none is required
                # because that call isn't in the model turns we send back.
                sig = call.get("thought_signature")
                if sig:
                    part.thought_signature = base64.b64decode(sig)
                parts.append(part)
                pending_names.append(fn["name"])
            if parts:
                contents.append(types.Content(role="model", parts=parts))
        elif role == "tool":
            name = pending_names.pop(0) if pending_names else "tool"
            contents.append(types.Content(role="user", parts=[
                types.Part.from_function_response(name=name, response=_coerce_response(m.get("content")))]))
    system = "\n\n".join(system_parts) if system_parts else None
    return system, contents


def _tools_to_gemini(tools: list[dict]):
    """Translate canonical OpenAI-style TOOL_SCHEMA dicts into a Gemini Tool of
    functionDeclarations. A no-parameter tool gets parameters=None (Gemini
    rejects an OBJECT schema with empty properties)."""
    from google.genai import types

    decls = []
    for t in tools:
        fn = t.get("function", t)
        params = fn.get("parameters") or {}
        if not params.get("properties"):
            params = None
        decls.append(types.FunctionDeclaration(
            name=fn["name"], description=fn.get("description", ""), parameters=params))
    return types.Tool(function_declarations=decls)


def _gemini_client(timeout: float = None):
    """Build a Gemini client. The key comes from GEMINI_API_KEY or GOOGLE_API_KEY
    (the SDK's own fallback order). Isolated so tests can stub it — and so the
    conftest guard can block any un-mocked real call."""
    from google import genai
    from google.genai import types

    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    kwargs = {"api_key": api_key}
    if timeout is not None:
        kwargs["http_options"] = types.HttpOptions(timeout=int(timeout * 1000))  # ms
    return genai.Client(**kwargs)


def _gemini_chat(
    messages: list[dict],
    model: str = None,
    tools: Optional[list[dict]] = None,
    timeout: float = None,
    logger: Optional[logging.Logger] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    think: Optional[bool] = None,
) -> dict:
    """Gemini backend. Streams (consulting should_cancel between chunks so the
    chat cancel button still lands), reassembling the reply into the same
    canonical `message` dict the Ollama path returns — content concatenated,
    tool_calls collected with a dict `arguments` so _execute_tool_call is
    unchanged."""
    from google.genai import types

    from agent.loop import TurnCancelled

    model = model or os.getenv("WREN_GEMINI_MODEL", GEMINI_DEFAULT_MODEL)
    max_out = int(os.getenv("WREN_GEMINI_MAX_OUTPUT_TOKENS", "8192"))
    # Gemini 2.5 models are *thinking* models, and thinking tokens count against
    # max_output_tokens. Left unbounded, the model can spend nearly the whole
    # budget on invisible reasoning and get cut off mid-answer (observed: a
    # weekly-learnings draft truncated to 162 visible tokens). Default the budget
    # to 0 (thinking off) — the scheduled tasks that use Gemini fill in a
    # template and don't need chain-of-thought. Note: 0 is valid for
    # gemini-2.5-flash (the default model); gemini-2.5-pro can't disable thinking,
    # so pin a positive WREN_GEMINI_THINKING_BUDGET if you switch to pro.
    thinking_budget = int(os.getenv("WREN_GEMINI_THINKING_BUDGET", "0"))
    # `think` is deliberately NOT honoured here. The seam's think=False means
    # "don't spend the answer's budget on scratchpad", which on this backend is
    # already the default above — and forcing 0 would override the per-model
    # escape hatch the env var exists to provide. Not every model accepts 0:
    # gemini-2.5-pro rejects it, and so does gemini-3.6-flash (400
    # INVALID_ARGUMENT; -1 for dynamic, or any positive budget, is accepted).
    # Tune WREN_GEMINI_THINKING_BUDGET per model instead.
    _ = think
    system, contents = _gemini_contents(messages)

    cfg_kwargs = dict(
        max_output_tokens=max_out,
        thinking_config=types.ThinkingConfig(thinking_budget=thinking_budget),
        # We drive the tool loop ourselves; disable the SDK's auto tool-calling
        # so it returns functionCall parts instead of trying to invoke Python.
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )
    if system:
        cfg_kwargs["system_instruction"] = system
    if tools:
        cfg_kwargs["tools"] = [_tools_to_gemini(tools)]
    config = types.GenerateContentConfig(**cfg_kwargs)

    client = _gemini_client(timeout=timeout)
    content_parts: list[str] = []
    tool_calls: list[dict] = []
    prompt_tokens = output_tokens = thinking_tokens = None
    finish_reason = None
    stream = client.models.generate_content_stream(model=model, contents=contents, config=config)
    for chunk in stream:
        if should_cancel is not None and should_cancel():
            raise TurnCancelled()
        cand = (chunk.candidates or [None])[0]
        if cand and cand.content and cand.content.parts:
            for p in cand.content.parts:
                if getattr(p, "text", None):
                    content_parts.append(p.text)
                fc = getattr(p, "function_call", None)
                if fc:
                    tc = {"function": {"name": fc.name, "arguments": dict(fc.args or {})}}
                    # Carry Gemini's per-functionCall thought_signature through the
                    # canonical shape so _gemini_contents can echo it back next turn
                    # (thinking models require it, or the follow-up 400s). base64 keeps
                    # the message JSON-serializable for _message_chars/_trim_history.
                    # Absent when thinking is off — nothing to carry.
                    sig = getattr(p, "thought_signature", None)
                    if sig:
                        tc["thought_signature"] = base64.b64encode(sig).decode()
                    tool_calls.append(tc)
        if cand and getattr(cand, "finish_reason", None):
            finish_reason = cand.finish_reason
        um = getattr(chunk, "usage_metadata", None)
        if um:
            prompt_tokens = getattr(um, "prompt_token_count", None) or prompt_tokens
            output_tokens = getattr(um, "candidates_token_count", None) or output_tokens
            thinking_tokens = getattr(um, "thoughts_token_count", None) or thinking_tokens

    message: dict = {"role": "assistant", "content": "".join(content_parts)}
    if tool_calls:
        message["tool_calls"] = tool_calls
    if logger:
        # finish_reason may be an enum; str() so it logs readably regardless.
        reason = str(finish_reason) if finish_reason is not None else None
        logger.info(
            "gemini_chat model=%s prompt_tokens=%s output_tokens=%s "
            "thinking_tokens=%s finish_reason=%s",
            model, prompt_tokens, output_tokens, thinking_tokens, reason,
        )
        # MAX_TOKENS means the reply was cut off before the model was done —
        # mirrors the Ollama num_predict warning above. With a thinking model,
        # the usual cause is thinking eating the output budget (see thinking
        # config above); WREN_GEMINI_THINKING_BUDGET=0 avoids that.
        if reason and "MAX_TOKENS" in reason:
            logger.warning(
                "gemini generation hit finish_reason=MAX_TOKENS and was cut off "
                "(output_tokens=%s, thinking_tokens=%s) — the draft is likely "
                "incomplete; raise WREN_GEMINI_MAX_OUTPUT_TOKENS or lower "
                "WREN_GEMINI_THINKING_BUDGET",
                output_tokens, thinking_tokens,
            )
    return message
