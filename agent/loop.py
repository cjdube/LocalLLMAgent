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

from agent import prefs

# The Gemini backend lives behind the _llm_chat seam in its own module; import
# its entry point and default-model constant here. The module's google.genai
# imports are function-local, so this import never pulls the cloud SDK on the
# local-only path. (Imported here rather than lazily inside _llm_chat so the
# tests' loop._gemini_chat reference resolves.)
from agent.backends.gemini import GEMINI_DEFAULT_MODEL, _gemini_chat

_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_ROOT / "config" / ".env")

# Bounds the number of model round-trips in one agent turn. 6 was enough for
# the original single-fetch tools, but navigating the learnings wiki
# (search_wiki -> a few read_wiki_page calls -> answer) legitimately chains
# more reads, so allow more headroom before giving up.
MAX_TOOL_ITERATIONS = 10

# How many confirmation-gated calls one user-turn may pause on before advance()
# stops offering them. MAX_TOOL_ITERATIONS bounds the auto-execute loop INSIDE a
# single advance() call, but a gated call returns out of advance() entirely — so
# the counter resets on every continuation and the pause/resolve chain had no
# bound at all. Observed 2026-08-18: one "add a calendar event" request produced
# a confirmation card, created the event, then produced three more cards for the
# same write; declining each one only fed the next. Only a fresh user message
# broke it (chat/server.py declines the pending call on a new message).
#
# 3 is well clear of legitimate use — the model emits one call at a time, and a
# genuine multi-write request ("add the event and email me about it") needs two.
MAX_GATED_PAUSES_PER_TURN = 3

# Cap the size of a single tool result before it's appended to the conversation.
# One oversized result (e.g. a web search dumping page after page of listings)
# can otherwise push the prompt past num_ctx, at which point Ollama silently
# truncates the FRONT of the conversation (dropping the system prompt) and the
# model tends to run away in a repetition loop. Trimming the result keeps the
# window intact. ~8000 chars is roughly 2000 tokens — big enough for a useful
# result, small enough that a few of them still fit a 16k window.
MAX_TOOL_RESULT_CHARS = int(os.getenv("OLLAMA_MAX_TOOL_RESULT_CHARS", "8000"))

# Per-tool overrides of that cap. The default is sized for a feed nobody bounds
# — a search dumping listings until it runs out. A tool that returns ONE curated
# document of known size is a different case, and the flat cap actively misled:
# 7 of the vault's 390 wiki pages exceed 8000 chars, so read_wiki_page handed
# back 42-95% of a page and Wren answered "SVPG isn't in your wiki" about a page
# with a section on SVPG in the part that was cut. A wiki page is bounded by
# what ObsidianWikiAgent wrote, so it gets room for the whole thing.
#
# Keep any entry here in step with the tool's own internal budget (wiki.py's
# MAX_PAGE_CHARS): the tool trims first, deliberately, keeping the [[link]]
# footer and naming what it dropped. This cap is only the backstop, and if it
# ever fires it undoes that careful trim by cutting the footer off again.
TOOL_RESULT_CHAR_CAPS = {
    "read_wiki_page": 16000,
    # Same shape as read_wiki_page — one document the user asked for, not an
    # unbounded feed — so it gets the same 14000/16000 pairing: fetch_webpage
    # caps its own markdown at WEB_FETCH_MAX_CHARS (14000) and this leaves room
    # for the wrapper around it. Keep the gap: when the two numbers were equal,
    # every truncated fetch landed ~520 over and the loop trimmed the tail of a
    # result the tool had already trimmed on purpose.
    "fetch_webpage": 16000,
    # These two bounded their result COUNT but not its size — a row cap never
    # bounds a payload — so the flat cap was the only thing holding them, and it
    # fired on ordinary use: a 5-result news search and a plain 20-row week of
    # notifications. Each now trims to its own MAX_PAYLOAD_CHARS (12000), drops
    # whole rows rather than slicing one, and leads with the field that must
    # survive (web_search's `answer`, push_log's `summary`). These are the
    # backstops sitting just above those budgets, and a test in each tool's suite
    # pins the worst case underneath the number here.
    "search_web": 13000,
    "list_notifications": 13000,
}


class TurnCancelled(Exception):
    """Raised inside advance()/_ollama_chat when the caller's should_cancel()
    reports the running turn was cancelled. The caller rolls back the partial
    turn and reports it as stopped (see chat/server.py's /chat handlers)."""


class OllamaUnavailable(Exception):
    """Raised when a model call produced nothing and we have classified why.

    Carries a message written for a human, because chat/server.py surfaces
    str(e) straight into the chat UI — the alternative was the raw urllib3
    ReadTimeout, which says "connection failed" for a server that is actually
    up and healthy."""


def _diagnose_stall(host: str, got_bytes: bool, waited: float) -> str:
    """Explain a model call that timed out without finishing.

    A read timeout alone cannot tell these apart, because both look identical
    from the socket — no bytes either way:

      * Ollama is down.
      * Ollama is up but serving someone else. It runs ONE request at a time
        (OLLAMA_NUM_PARALLEL defaults to 1) and queues the rest silently, so a
        chat turn stuck behind a long background job gets nothing until that
        job finishes. Observed 2026-08-03: a daily wiki-ingest run held the
        slot for ~3h and every Wren turn in that window reported a connection
        error against a perfectly healthy Ollama.
      * The runner wedged — accepts the request, completes prefill, never
        generates. Same upstream shape as ml-explore/mlx-lm#1493.

    Probing /api/ps afterwards separates "down" from the other two, and the
    loaded-model list names what is holding the slot. Best-effort: if the probe
    itself fails we just report the timeout we already know about.

    Telling a busy Ollama from a wedged runner (and what has already been ruled
    out as a cause) is in docs/ollama-serving.md."""
    waited_s = f"{waited:.0f}s"
    stalled = "mid-reply" if got_bytes else "without producing any output"
    try:
        resp = requests.get(f"{host}/api/ps", timeout=5)
        resp.raise_for_status()
        loaded = [m.get("name", "?") for m in (resp.json().get("models") or [])]
    except Exception:
        return (f"Ollama at {host} did not respond within {waited_s} and is not "
                f"answering status checks either — it looks down. Check that it "
                f"is running.")
    if not loaded:
        return (f"Ollama at {host} is up but has no model loaded and stalled "
                f"{stalled} after {waited_s}.")
    return (f"Ollama at {host} is up (model {', '.join(loaded)} loaded) but "
            f"stalled {stalled} after {waited_s}. It serves one request at a "
            f"time, so it is either busy with another job or its runner is "
            f"wedged. Retry; if it repeats, restart Ollama.")


def load_persona(filename: str) -> str:
    """Load a persona/context markdown file from agent/, stripping HTML
    comments (those are notes for maintainers, not the model)."""
    try:
        raw = (Path(__file__).resolve().parent / filename).read_text()
        return re.sub(r"<!--.*?-->", "", raw, flags=re.DOTALL).strip()
    except FileNotFoundError:
        return ""


WREN_CORE = load_persona("wren.md")
# Gitignored (see agent/identity.example.md): who Wren serves is personal data,
# so a clone without one just gets "" and runs on wren.md alone.
USER_CONTEXT = load_persona("identity.md")


def with_identity(system_prompt: str) -> str:
    # Memories are rendered at call time (not import) so a fact saved mid-session
    # is present in the next conversation's system prompt.
    from agent.tools.memory import render_memory_block

    # The model has no way to know its own serving name — that's a runtime
    # config value (OLLAMA_MODEL etc.), not something in its weights. Without
    # this line, asking "what model are you" gets a guess from pretraining
    # instead of the answer. Computed at call time (not import) so it tracks
    # a config change without a restart.
    model_line = f"The model you are running as right now is: {active_model_label()}."

    parts = [p for p in (WREN_CORE, USER_CONTEXT, model_line, render_memory_block(), system_prompt) if p]
    return "\n\n---\n\n".join(parts)


def _ollama_chat(
    messages: list[dict],
    model: str = None,
    host: str = None,
    tools: Optional[list[dict]] = None,
    timeout: float = None,
    logger: Optional[logging.Logger] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    think: Optional[bool] = None,
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
    # Cap tokens generated per call: the small model can fall into a repetition
    # loop and, uncapped, run away for thousands of junk tokens that then live
    # in the session history (observed: a 5,459-token "list memories" reply).
    # 3072 clears the longest legitimate reply seen (a ~2,200-token teardown).
    # Note this budget covers THINKING TOKENS TOO, and it was sized before the
    # model had a thinking channel: a reasoning-heavy call can spend all 3072 on
    # scratchpad and return empty content (see `think` below).
    num_predict = int(os.getenv("OLLAMA_NUM_PREDICT", "3072"))

    payload = {
        "model": model,
        "messages": messages,
        "stream": True,
        # Keep the (large, slow-to-load) model resident between calls so the
        # next one doesn't pay the cold-load cost inside its read-timeout window.
        "keep_alive": os.getenv("OLLAMA_KEEP_ALIVE", "30m"),
        "options": {"num_ctx": num_ctx, "num_predict": num_predict},
    }
    if tools is not None:
        payload["tools"] = tools
    # Omitted entirely unless a caller opts out, so chat keeps the model's
    # default behaviour. See complete_text() for why a task turns it off.
    if think is not None:
        payload["think"] = think

    content_parts: list[str] = []
    tool_calls: list[dict] = []
    prompt_tokens = eval_tokens = None
    # Tracked so a timeout can say whether the stream never started (queued
    # behind another request, or a wedged runner) or died mid-reply.
    got_bytes = False
    t0 = time.monotonic()
    try:
        with requests.post(f"{host}/api/chat", json=payload, timeout=timeout,
                           stream=True) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if should_cancel is not None and should_cancel():
                    raise TurnCancelled()
                got_bytes = True
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
    except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
        # Both arrive as a bare "connection failed" that blames the network for
        # what is usually a busy or wedged server; _diagnose_stall probes Ollama
        # and says which it was. TurnCancelled is not a requests error, so a
        # user-cancelled turn passes through untouched.
        detail = _diagnose_stall(host, got_bytes, time.monotonic() - t0)
        if logger:
            logger.warning("ollama_chat stalled: %s", detail)
        raise OllamaUnavailable(detail) from e

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
        if isinstance(eval_tokens, int) and eval_tokens >= num_predict:
            logger.warning(
                "ollama generation (%d tokens) reached num_predict=%d and was "
                "cut off — healthy replies stay well under the cap, so this "
                "means either a repetition loop or a thinking model spending "
                "the whole budget on scratchpad (which returns EMPTY content; "
                "pass think=False for template-filling calls)",
                eval_tokens, num_predict,
            )
    return message


def warm_model(
    model: str = None,
    host: str = None,
    timeout: float = None,
    logger: Optional[logging.Logger] = None,
    backend: Optional[str] = None,
) -> bool:
    """Force-load the model into Ollama's memory before a heavy generation.

    A no-op (returns True) for any non-Ollama backend: there's nothing to
    pre-load for a cloud model, and the caller passes the task's resolved
    backend so a cloud-routed task doesn't needlessly poke a local Ollama.

    A cold local model (gemma4:26b-mlx is ~17GB) can't emit its first streamed
    chunk until it's loaded AND the prompt is prefilled; stacked together on a
    cold start these exceed the streamed call's read timeout, so a big-prompt
    big-prompt task times out before any token arrives. Loading the
    model first — with the SAME num_ctx and keep_alive, so the real call reuses
    this resident instance rather than reloading — moves the 17GB load out of
    that window and leaves only prefill.

    Sends an empty-messages /api/chat, which Ollama loads-and-returns without
    generating. Gets its own generous timeout (OLLAMA_WARM_TIMEOUT, default
    600s) because the load is the slow part. Degrades to a warning and returns
    False on failure — the caller still attempts the generation cold."""
    if _resolve_backend(backend) != "ollama":
        return True
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


# --------------------------------------------------------------------------- #
# Backend seam. Wren speaks one canonical message/tool shape internally (the
# Ollama/OpenAI shape every caller already builds); _llm_chat dispatches to the
# selected backend, which translates to/from its provider format *inside itself*
# so advance()/complete_text() and their callers never change. The default is
# local Ollama — a cloud backend is opt-in per the local-first design.
# --------------------------------------------------------------------------- #

def _resolve_backend(backend: Optional[str] = None) -> str:
    """explicit arg -> WREN_LLM_BACKEND env -> 'ollama' fallback."""
    return (backend or os.getenv("WREN_LLM_BACKEND") or "ollama").strip().lower()


def resolve_backend(task_key: str) -> Optional[str]:
    """Per-task backend override for scheduled tasks: WREN_<TASK_KEY>_BACKEND
    falls back to the global WREN_LLM_BACKEND. Returns None when neither is set,
    which lets _llm_chat apply its own 'ollama' default. Lets chat stay local
    while an individual task (e.g. opportunity_digest) opts into a cloud model."""
    return os.getenv(f"WREN_{task_key.upper()}_BACKEND") or os.getenv("WREN_LLM_BACKEND") or None


def active_model_label(backend: Optional[str] = None) -> str:
    """A human 'model (backend)' label for the current backend — surfaced in the
    dashboard so the UI reflects the model actually in use, not a hardcoded one."""
    b = _resolve_backend(backend)
    if b in ("gemini", "google"):
        return f"{os.getenv('WREN_GEMINI_MODEL', GEMINI_DEFAULT_MODEL)} ({b})"
    return f"{os.getenv('OLLAMA_MODEL', 'gemma4')} ({b})"


def escalation_backend() -> Optional[str]:
    """The frontier backend the chat 'redo with the frontier model' button
    escalates to, from WREN_ESCALATION_BACKEND (lowercased), or None when unset.

    Deliberately its OWN variable rather than WREN_LLM_BACKEND: in a local-first
    setup the global default is 'ollama', which names no frontier target, so
    reusing it couldn't say where an escalation should go. Provider-neutral by
    design — see docs/frontier-escalation.md."""
    b = os.getenv("WREN_ESCALATION_BACKEND")
    return b.strip().lower() if b and b.strip() else None


def escalation_available() -> bool:
    """True only when escalation is configured AND the target provider's
    credentials are present — the gate for offering the escalation button, so a
    tap can't be a guaranteed failure. An unset backend, or one whose key is
    missing (or whose provider this function doesn't yet know how to verify),
    means the button is simply not shown. Adding a provider adds a branch here."""
    b = escalation_backend()
    if b in ("gemini", "google"):
        return bool(os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"))
    return False


def _llm_chat(
    messages: list[dict],
    backend: Optional[str] = None,
    model: str = None,
    host: str = None,
    tools: Optional[list[dict]] = None,
    timeout: float = None,
    logger: Optional[logging.Logger] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    think: Optional[bool] = None,
) -> dict:
    """Dispatch a chat completion to the selected backend, returning the same
    canonical `message` dict shape regardless of provider.

    `think` says the caller's answer needs the whole token budget, so the
    scratchpad shouldn't compete for it. Only the Ollama path acts on it; the
    Gemini backend already defaults its thinking budget to 0 and exposes
    WREN_GEMINI_THINKING_BUDGET as the per-model override (some models reject
    0 outright), so forcing it there would break more than it fixed."""
    b = _resolve_backend(backend)
    if b == "ollama":
        return _ollama_chat(messages, model=model, host=host, tools=tools,
                            timeout=timeout, logger=logger, should_cancel=should_cancel,
                            think=think)
    if b in ("gemini", "google"):
        return _gemini_chat(messages, model=model, tools=tools, timeout=timeout,
                            logger=logger, should_cancel=should_cancel, think=think)
    raise ValueError(f"unknown WREN_LLM_BACKEND {b!r} (expected 'ollama' or 'gemini')")


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
    cap = TOOL_RESULT_CHAR_CAPS.get(fn_name, MAX_TOOL_RESULT_CHARS)
    if len(content) > cap:
        dropped = len(content) - cap
        content = content[:cap] + f"... [truncated {dropped} chars to fit the context window]"
        if logger:
            logger.warning(
                "tool_call %s result trimmed: %d chars over the %d cap",
                fn_name, dropped, cap,
            )
    if logger:
        logger.info(f"tool_call {fn_name}({_redact_args(fn_args)}) -> {content}")
    # tool_name is what ties this result back to the call it answers. Without it
    # the model sees an unlabelled blob after its own tool_call and can't tell
    # the call was ever executed — one of the three reasons it re-issued a
    # confirmation-gated write instead of reporting it (see
    # MAX_GATED_PAUSES_PER_TURN). Ollama passes it into the chat template; the
    # Gemini backend prefers it over positional pairing.
    messages.append({"role": "tool", "tool_name": fn_name, "content": content})


def _call_key(call: dict) -> tuple[str, str]:
    """(name, canonical args) identifying a tool call. Args are dumped with
    sorted keys so a re-emission that merely reorders them still matches."""
    fn = call["function"]
    args = fn.get("arguments")
    try:
        canonical = json.dumps(args, sort_keys=True, default=str)
    except (TypeError, ValueError):
        canonical = str(args)
    return fn["name"], canonical


def _answered_gated_calls(messages: list[dict], confirm_before: frozenset) -> dict:
    """{_call_key(call): "approved"|"declined"|"capped"} for every confirm-gated
    call in the CURRENT user-turn that already has a tool result — one the user
    has answered either way, or one this guard already suppressed.

    Read out of `messages` rather than tracked as caller state, so the chat
    server and tasks/bg_worker.py both get the guard without passing anything
    new through their pause/resolve chains.

    Results are paired with calls positionally and the pending list is RESET per
    assistant turn, exactly as _gemini_contents does and for the same reason:
    advance() drops any batched calls after a confirm-gated one, so a turn can
    emit two calls and yield one result, and a global queue would then pop the
    wrong name for every later result. Resetting confines the mismatch to the
    turn where the drop happened.
    """
    start = 0
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            start = i
            break

    answered: dict = {}
    pending: list[dict] = []
    for m in messages[start:]:
        role = m.get("role")
        if role == "assistant":
            pending = list(m.get("tool_calls") or [])
        elif role == "tool":
            if not pending:
                continue
            call = pending.pop(0)
            name = call["function"]["name"]
            if name not in confirm_before:
                continue
            try:
                result = json.loads(m.get("content") or "")
            except (ValueError, TypeError):
                result = None
            if not isinstance(result, dict):
                result = {}
            if result.get("not_run"):
                # A call this guard already suppressed. It carries the outcome of
                # the call it duplicated, so a THIRD copy is told the same story
                # as the second rather than being told it succeeded.
                outcome = result.get("already", "approved")
            elif result.get("declined"):
                outcome = "declined"
            else:
                outcome = "approved"
            answered[_call_key(call)] = outcome
    return answered


def _repeat_call_note(name: str, outcome: str) -> str:
    """What to hand the model in place of a second confirmation for a write it
    already got an answer on."""
    who = prefs.user_name()
    if outcome == "capped":
        return (f"You already called {name} with these exact arguments in this "
                "conversation turn, and it was not run then either. Do not call "
                f"it again. Answer {who} in words.")
    if outcome == "declined":
        return (f"You already asked {who} to confirm {name} with these exact "
                f"arguments in this conversation turn, and {who} declined it. It "
                "was NOT run, and it has not been run again now. Do not call it "
                f"again. Tell {who} it is cancelled and ask what they would like "
                "changed.")
    return (f"You already called {name} with these exact arguments in this "
            f"conversation turn, {who} confirmed it, and it succeeded. It has "
            "NOT been run a second time, so nothing is duplicated. Do not call "
            f"it again. Tell {who} in words that it is done, using the details "
            "from the earlier result.")


def advance(
    messages: list[dict],
    tools: list[dict],
    dispatch: dict[str, Callable[..., dict]],
    model: str = None,
    host: str = None,
    logger: Optional[logging.Logger] = None,
    confirm_before: frozenset[str] = frozenset(),
    should_cancel: Optional[Callable[[], bool]] = None,
    backend: Optional[str] = None,
    timeout: float = None,
) -> dict:
    """Advance a tool-calling conversation already seeded in `messages`
    (system + user turns, and any prior assistant/tool turns).

    timeout: per-model-call read timeout, forwarded to the backend. Interactive
             callers pass a tighter one than the scheduled tasks' default (see
             chat/server.py:CHAT_MODEL_TIMEOUT).

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

    Two guards bound that pause/resolve chain, which is otherwise unbounded
    because each gated call returns out of advance() and resets the
    MAX_TOOL_ITERATIONS counter: a gated call identical to one already answered
    in this user-turn is never re-offered, and no turn offers more than
    MAX_GATED_PAUSES_PER_TURN of them. A suppressed call is NOT executed — the
    model gets a tool result telling it to answer in words, and the suppression
    is logged at WARNING.
    """
    for _ in range(MAX_TOOL_ITERATIONS):
        if should_cancel is not None and should_cancel():
            raise TurnCancelled()
        message = _llm_chat(
            messages, backend=backend, model=model, host=host, tools=tools,
            logger=logger, should_cancel=should_cancel, timeout=timeout,
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
            name = call["function"]["name"]
            if name in confirm_before:
                # Never offer the same write twice, and never offer more than a
                # handful in one turn. Both paths leave the call UNEXECUTED and
                # hand the model a result explaining why, so it answers in words
                # instead of pausing again. See MAX_GATED_PAUSES_PER_TURN.
                answered = _answered_gated_calls(messages, confirm_before)
                outcome = answered.get(_call_key(call))
                if outcome is not None:
                    if logger:
                        logger.warning(
                            "advance() suppressed a repeat confirmation: %s was "
                            "already %s with identical arguments in this turn; "
                            "not re-offering it", name, outcome,
                        )
                    note = _repeat_call_note(name, outcome)
                    already = outcome
                elif len(answered) >= MAX_GATED_PAUSES_PER_TURN:
                    if logger:
                        logger.warning(
                            "advance() suppressed %s: this turn has already "
                            "asked for %d confirmations (cap %d)",
                            name, len(answered), MAX_GATED_PAUSES_PER_TURN,
                        )
                    note = (
                        f"You have already asked {prefs.user_name()} to confirm "
                        f"{len(answered)} actions in this one turn, which is the "
                        f"limit. {name} was NOT run. Stop calling tools and "
                        "answer in words, summarizing what was and was not done."
                    )
                    already = "capped"
                else:
                    return {"type": "confirm", "call": call}
                # "already" is what a THIRD copy of this call reads back, so it
                # inherits this call's story rather than being told it succeeded.
                messages.append({
                    "role": "tool", "tool_name": name,
                    "content": json.dumps({"not_run": True, "already": already,
                                           "note": note}),
                })
                continue
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
        name = call["function"]["name"]
        if logger:
            logger.info(f"tool_call {name} declined by user")
        # Deliberately NOT an {"error": ...} shape. That is what
        # _execute_tool_call returns when a tool genuinely crashes, and retrying
        # a failure is correct model behaviour — so a decline read as a
        # transient error and the model re-issued the same gated write, drawing
        # a fresh confirmation card out of the next advance(). A decline is a
        # decision, not a failure. _answered_gated_calls keys off "declined".
        messages.append({
            "role": "tool", "tool_name": name,
            "content": json.dumps({
                "declined": True,
                "note": f"{prefs.user_name()} declined this action, so it was "
                        "not performed. Do not try it again — say it is "
                        "cancelled and ask what they would like changed.",
            }),
        })


def complete_text(
    system_prompt: str,
    user_prompt: str,
    model: str = None,
    host: str = None,
    timeout: float = None,
    logger: Optional[logging.Logger] = None,
    backend: Optional[str] = None,
    think: Optional[bool] = None,
) -> str:
    """Single-turn, tool-free completion — for tasks like writing a short
    summary paragraph where the caller assembles the surrounding structure
    itself rather than trusting the model to produce it. `backend` lets a
    scheduled task route just itself to a cloud model (see resolve_backend).

    Pass `think=False` for a call that fills in a template — a classification,
    a score, a fixed output format. Thinking tokens are drawn from the same
    num_predict budget as the answer, so a model that reasons too long returns
    EMPTY content rather than a truncated answer, which reads downstream as a
    parse failure. Measured on 40-lead digest scoring: thinking on, 0 of 3 runs
    produced output (all cut off at the cap); thinking off, 3 of 3, and 5x
    faster. The chat path leaves this None — there, reasoning is the point."""
    message = _llm_chat(
        [
            {"role": "system", "content": with_identity(system_prompt)},
            {"role": "user", "content": user_prompt},
        ],
        backend=backend,
        model=model,
        host=host,
        timeout=timeout,
        logger=logger,
        think=think,
    )
    return message.get("content", "").strip()
