"""Tests for agent.loop._ollama_chat — the single choke point for model calls.

Focus: the payload sets an explicit `num_ctx` (env-overridable) and the call
logs the effective context window plus the actual prompt token count, warning
when the prompt reaches the ceiling (likely front-truncation).
"""

import base64
import json as _json
import logging

import pytest

from agent import loop
from agent.backends import gemini as gemini_backend


class _FakeResponse:
    """A streaming Ollama response: yields each chunk dict as an NDJSON line,
    and works as a context manager like requests' streamed Response."""
    def __init__(self, chunks):
        self._chunks = chunks

    def raise_for_status(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_lines(self):
        for chunk in self._chunks:
            yield _json.dumps(chunk).encode()


def _patch_post(monkeypatch, captured, response):
    """Capture the outgoing payload and return a canned Ollama stream. `response`
    is given in the old single-blob shape ({"message": ..., "prompt_eval_count":
    ..., "eval_count": ...}); it's wrapped as one terminal streamed chunk."""
    chunk = {
        "message": response.get("message", {}),
        "done": True,
        "prompt_eval_count": response.get("prompt_eval_count"),
        "eval_count": response.get("eval_count"),
    }
    _patch_post_chunks(monkeypatch, captured, [chunk])


def _patch_post_chunks(monkeypatch, captured, chunks):
    """Lower-level variant: drive the stream with explicit chunk dicts."""
    def fake_post(url, json=None, timeout=None, stream=None):
        captured["url"] = url
        captured["payload"] = json
        return _FakeResponse(chunks)

    monkeypatch.setattr(loop.requests, "post", fake_post)


def test_payload_sets_default_num_ctx(monkeypatch):
    monkeypatch.delenv("OLLAMA_NUM_CTX", raising=False)
    captured = {}
    _patch_post(monkeypatch, captured, {"message": {"content": "hi"}})

    message = loop._ollama_chat([{"role": "user", "content": "hey"}])

    assert captured["payload"]["options"]["num_ctx"] == 8192
    assert captured["payload"]["stream"] is True
    assert message["content"] == "hi"


def test_payload_sets_default_keep_alive(monkeypatch):
    monkeypatch.delenv("OLLAMA_KEEP_ALIVE", raising=False)
    captured = {}
    _patch_post(monkeypatch, captured, {"message": {"content": "hi"}})

    loop._ollama_chat([{"role": "user", "content": "hey"}])

    assert captured["payload"]["keep_alive"] == "30m"


def test_keep_alive_honors_env_override(monkeypatch):
    monkeypatch.setenv("OLLAMA_KEEP_ALIVE", "-1")
    captured = {}
    _patch_post(monkeypatch, captured, {"message": {"content": "hi"}})

    loop._ollama_chat([{"role": "user", "content": "hey"}])

    assert captured["payload"]["keep_alive"] == "-1"


def test_num_ctx_honors_env_override(monkeypatch):
    monkeypatch.setenv("OLLAMA_NUM_CTX", "16384")
    captured = {}
    _patch_post(monkeypatch, captured, {"message": {"content": "hi"}})

    loop._ollama_chat([{"role": "user", "content": "hey"}])

    assert captured["payload"]["options"]["num_ctx"] == 16384


def test_payload_sets_default_num_predict(monkeypatch):
    monkeypatch.delenv("OLLAMA_NUM_PREDICT", raising=False)
    captured = {}
    _patch_post(monkeypatch, captured, {"message": {"content": "hi"}})

    loop._ollama_chat([{"role": "user", "content": "hey"}])

    assert captured["payload"]["options"]["num_predict"] == 3072


def test_num_predict_honors_env_override(monkeypatch):
    monkeypatch.setenv("OLLAMA_NUM_PREDICT", "1024")
    captured = {}
    _patch_post(monkeypatch, captured, {"message": {"content": "hi"}})

    loop._ollama_chat([{"role": "user", "content": "hey"}])

    assert captured["payload"]["options"]["num_predict"] == 1024


def test_think_key_omitted_unless_a_caller_opts_out(monkeypatch):
    # Chat must keep the model's default behaviour: no key in the payload at all.
    captured = {}
    _patch_post(monkeypatch, captured, {"message": {"content": "hi"}})

    loop._ollama_chat([{"role": "user", "content": "hey"}])

    assert "think" not in captured["payload"]


def test_think_false_reaches_the_payload(monkeypatch):
    # A template-filling task turns thinking off: the scratchpad competes with
    # the answer for num_predict, and losing that race returns EMPTY content.
    captured = {}
    _patch_post(monkeypatch, captured, {"message": {"content": "hi"}})

    loop._ollama_chat([{"role": "user", "content": "hey"}], think=False)

    assert captured["payload"]["think"] is False


def test_complete_text_passes_think_through_the_seam(monkeypatch):
    seen = {}

    def fake_llm_chat(messages, **kwargs):
        seen.update(kwargs)
        return {"content": "ok"}

    monkeypatch.setattr(loop, "_llm_chat", fake_llm_chat)

    assert loop.complete_text("sys", "user", think=False) == "ok"
    assert seen["think"] is False


def test_warns_when_generation_reaches_num_predict(monkeypatch, caplog):
    monkeypatch.setenv("OLLAMA_NUM_PREDICT", "50")
    _patch_post(
        monkeypatch,
        {},
        {"message": {"content": "hi"}, "prompt_eval_count": 10, "eval_count": 50},
    )
    logger = logging.getLogger("test_loop.predict_warn")

    with caplog.at_level(logging.WARNING, logger=logger.name):
        loop._ollama_chat([{"role": "user", "content": "hey"}], logger=logger)

    assert any(r.levelno == logging.WARNING for r in caplog.records)
    assert "num_predict=50" in caplog.text and "cut off" in caplog.text


def test_logs_prompt_token_usage(monkeypatch, caplog):
    monkeypatch.delenv("OLLAMA_NUM_CTX", raising=False)
    _patch_post(
        monkeypatch,
        {},
        {"message": {"content": "hi"}, "prompt_eval_count": 123, "eval_count": 45},
    )
    logger = logging.getLogger("test_loop.usage")

    with caplog.at_level(logging.INFO, logger=logger.name):
        loop._ollama_chat([{"role": "user", "content": "hey"}], logger=logger)

    assert "prompt_tokens=123" in caplog.text
    assert "num_ctx=8192" in caplog.text


def test_warns_when_prompt_reaches_num_ctx(monkeypatch, caplog):
    monkeypatch.setenv("OLLAMA_NUM_CTX", "100")
    _patch_post(
        monkeypatch,
        {},
        {"message": {"content": "hi"}, "prompt_eval_count": 100, "eval_count": 5},
    )
    logger = logging.getLogger("test_loop.warn")

    with caplog.at_level(logging.WARNING, logger=logger.name):
        loop._ollama_chat([{"role": "user", "content": "hey"}], logger=logger)

    assert any(r.levelno == logging.WARNING for r in caplog.records)
    assert "likely truncated" in caplog.text


def test_no_logging_without_logger(monkeypatch, caplog):
    """A missing logger must not raise and must not emit records."""
    _patch_post(monkeypatch, {}, {"message": {"content": "hi"}, "prompt_eval_count": 10})

    with caplog.at_level(logging.INFO):
        loop._ollama_chat([{"role": "user", "content": "hey"}])

    assert caplog.records == []


def test_stream_reassembles_content_and_tool_calls(monkeypatch):
    """Content is concatenated across chunks and tool_calls are collected."""
    call = {"function": {"name": "search_web", "arguments": {"query": "x"}}}
    chunks = [
        {"message": {"content": "Hel"}},
        {"message": {"content": "lo"}},
        {"message": {"tool_calls": [call]}, "done": True,
         "prompt_eval_count": 5, "eval_count": 3},
    ]
    _patch_post_chunks(monkeypatch, {}, chunks)

    message = loop._ollama_chat([{"role": "user", "content": "hey"}], tools=[])

    assert message["content"] == "Hello"
    assert message["tool_calls"] == [call]


def test_should_cancel_interrupts_stream(monkeypatch):
    """When should_cancel fires, the stream is abandoned with TurnCancelled
    rather than returning a (partial) message."""
    chunks = [{"message": {"content": "partial"}}, {"message": {}, "done": True}]
    _patch_post_chunks(monkeypatch, {}, chunks)

    with pytest.raises(loop.TurnCancelled):
        loop._ollama_chat([{"role": "user", "content": "hey"}],
                          should_cancel=lambda: True)


def _patch_post_raising(monkeypatch, exc):
    """Drive _ollama_chat's request into a transport failure."""
    def fake_post(url, json=None, timeout=None, stream=None):
        raise exc
    monkeypatch.setattr(loop.requests, "post", fake_post)


def _patch_ps(monkeypatch, models=None, fail=False):
    """Stand in for the /api/ps probe _diagnose_stall makes."""
    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"models": [{"name": m} for m in (models or [])]}

    def fake_get(url, timeout=None):
        assert url.endswith("/api/ps")
        if fail:
            raise loop.requests.exceptions.ConnectionError("refused")
        return _Resp()

    monkeypatch.setattr(loop.requests, "get", fake_get)


def test_timeout_with_ollama_down_says_down(monkeypatch):
    """Ollama unreachable: the message names it as down, not as busy."""
    _patch_post_raising(monkeypatch, loop.requests.exceptions.ReadTimeout("timed out"))
    _patch_ps(monkeypatch, fail=True)

    with pytest.raises(loop.OllamaUnavailable) as excinfo:
        loop._ollama_chat([{"role": "user", "content": "hey"}])

    msg = str(excinfo.value)
    assert "looks down" in msg
    assert "busy" not in msg


def test_timeout_with_ollama_up_says_busy_and_names_model(monkeypatch):
    """The 2026-08-03 outage: Ollama healthy, but serving one request at a time
    with the slot held elsewhere. The user must not be told the connection
    failed — that sent us looking at the network instead of the queue."""
    _patch_post_raising(monkeypatch, loop.requests.exceptions.ReadTimeout("timed out"))
    _patch_ps(monkeypatch, models=["gemma4:26b-mlx"])

    with pytest.raises(loop.OllamaUnavailable) as excinfo:
        loop._ollama_chat([{"role": "user", "content": "hey"}])

    msg = str(excinfo.value)
    assert "is up" in msg
    assert "gemma4:26b-mlx" in msg
    assert "one request at a time" in msg
    assert "without producing any output" in msg


def test_timeout_after_partial_stream_says_mid_reply(monkeypatch):
    """A stream that starts and then stalls is a different fault from one that
    never starts, so the two must not report the same cause."""
    class _StallingResponse(_FakeResponse):
        def iter_lines(self):
            yield _json.dumps({"message": {"content": "par"}}).encode()
            raise loop.requests.exceptions.ReadTimeout("timed out")

    monkeypatch.setattr(loop.requests, "post",
                        lambda url, json=None, timeout=None, stream=None: _StallingResponse([]))
    _patch_ps(monkeypatch, models=["gemma4:26b-mlx"])

    with pytest.raises(loop.OllamaUnavailable) as excinfo:
        loop._ollama_chat([{"role": "user", "content": "hey"}])

    assert "mid-reply" in str(excinfo.value)


def test_cancel_is_not_swallowed_by_the_timeout_handler(monkeypatch):
    """TurnCancelled must still propagate as itself — a user pressing stop is
    not an Ollama fault and must not be reported as one."""
    chunks = [{"message": {"content": "partial"}}, {"message": {}, "done": True}]
    _patch_post_chunks(monkeypatch, {}, chunks)

    with pytest.raises(loop.TurnCancelled):
        loop._ollama_chat([{"role": "user", "content": "hey"}],
                          should_cancel=lambda: True)


def test_advance_forwards_timeout_to_the_backend(monkeypatch):
    """advance() must accept and forward `timeout` — chat/server.py passes one
    per interactive turn. Pinned against the real advance(), because the
    server-side doubles absorb **kwargs and would hide a signature mismatch."""
    seen = {}

    def fake_llm_chat(messages, **kwargs):
        seen.update(kwargs)
        return {"role": "assistant", "content": "done"}

    monkeypatch.setattr(loop, "_llm_chat", fake_llm_chat)
    result = loop.advance([{"role": "user", "content": "hey"}], [], {}, timeout=42.0)

    assert result == {"type": "final", "text": "done"}
    assert seen["timeout"] == 42.0


def test_advance_checks_cancel_before_calling_model(monkeypatch):
    """advance() raises TurnCancelled up front without hitting the model when
    the turn is already cancelled."""
    def unexpected(*a, **k):
        raise AssertionError("model should not be called once cancelled")

    monkeypatch.setattr(loop, "_ollama_chat", unexpected)
    with pytest.raises(loop.TurnCancelled):
        loop.advance([], [], {}, should_cancel=lambda: True)


class _WarmResponse:
    """Minimal non-streamed response for warm_model (raise_for_status only)."""
    def __init__(self, ok=True):
        self._ok = ok

    def raise_for_status(self):
        if not self._ok:
            raise loop.requests.exceptions.HTTPError("boom")


def test_warm_model_loads_with_empty_messages(monkeypatch):
    """warm_model preloads via an empty-messages, non-streamed /api/chat that
    carries the same num_ctx and keep_alive as a real call (so the model stays
    resident and is reused)."""
    monkeypatch.setenv("OLLAMA_NUM_CTX", "16384")
    monkeypatch.setenv("OLLAMA_KEEP_ALIVE", "30m")
    captured = {}

    def fake_post(url, json=None, timeout=None, stream=None):
        captured["url"] = url
        captured["payload"] = json
        captured["timeout"] = timeout
        return _WarmResponse(ok=True)

    monkeypatch.setattr(loop.requests, "post", fake_post)

    assert loop.warm_model() is True
    assert captured["url"].endswith("/api/chat")
    assert captured["payload"]["messages"] == []
    assert captured["payload"]["stream"] is False
    assert captured["payload"]["options"]["num_ctx"] == 16384
    assert captured["payload"]["keep_alive"] == "30m"


def test_warm_model_uses_warm_timeout_env(monkeypatch):
    monkeypatch.setenv("OLLAMA_WARM_TIMEOUT", "42")
    captured = {}
    monkeypatch.setattr(
        loop.requests, "post",
        lambda url, json=None, timeout=None, stream=None: captured.update(timeout=timeout)
        or _WarmResponse(ok=True),
    )

    loop.warm_model()

    assert captured["timeout"] == 42.0


def test_warm_model_degrades_on_failure(monkeypatch):
    """A failed preload returns False (not raises) so the caller still tries the
    real generation."""
    def boom(*a, **k):
        raise loop.requests.exceptions.ConnectionError("no server")

    monkeypatch.setattr(loop.requests, "post", boom)

    assert loop.warm_model() is False


def test_oversized_tool_result_is_truncated(monkeypatch):
    """A tool result larger than the cap is trimmed before being appended, so a
    single huge result can't blow past the context window."""
    monkeypatch.setattr(loop, "MAX_TOOL_RESULT_CHARS", 100)
    # A tool with no TOOL_RESULT_CHAR_CAPS entry, so this exercises the flat
    # default rather than an override.
    call = {"function": {"name": "some_unbounded_tool", "arguments": {}}}
    messages = []
    loop._execute_tool_call(
        call, {"some_unbounded_tool": lambda **_: {"blob": "x" * 5000}}, messages, logger=None
    )

    content = messages[-1]["content"]
    assert "truncated" in content
    assert len(content) < 5000


def test_a_tool_with_its_own_cap_gets_the_bigger_budget(monkeypatch):
    """The flat cap is sized for an unbounded feed. A tool returning one curated
    document of known size gets more room — read_wiki_page was handing back 42%
    of the vault's biggest page, and Wren reported the missing 58% as absent."""
    monkeypatch.setattr(loop, "MAX_TOOL_RESULT_CHARS", 100)
    monkeypatch.setattr(loop, "TOOL_RESULT_CHAR_CAPS", {"read_wiki_page": 5000})
    messages = []
    dispatch = {"read_wiki_page": lambda **_: {"content": "x" * 2000},
                "search_web": lambda **_: {"blob": "x" * 2000}}

    loop._execute_tool_call(
        {"function": {"name": "read_wiki_page", "arguments": {}}}, dispatch, messages, logger=None)
    assert "truncated" not in messages[-1]["content"]

    # Everything else still gets the flat cap.
    loop._execute_tool_call(
        {"function": {"name": "search_web", "arguments": {}}}, dispatch, messages, logger=None)
    assert "truncated" in messages[-1]["content"]


def test_the_real_wiki_page_cap_leaves_room_for_json_escaping(monkeypatch):
    """wiki.MAX_PAGE_CHARS counts page chars; the loop cap counts JSON-escaped
    chars. If the gap between them ever closes, the blind backstop cuts off the
    [[link]] footer _fit_page trims the body specifically to protect."""
    from agent.tools import wiki

    page = ("line of text\n" * 2000)[: wiki.MAX_PAGE_CHARS]  # worst case: all newlines
    fitted = wiki._fit_page(page)
    assert len(_json.dumps({"content": fitted})) < loop.TOOL_RESULT_CHAR_CAPS["read_wiki_page"]


# --------------------------------------------------------------------------- #
# Cloud (Gemini) backend + backend selection. The adapter must return the SAME
# canonical message shape as the Ollama path so advance()/_execute_tool_call and
# the confirm/resolve gate keep working unchanged. Fakes stand in for the Gemini
# SDK's streamed chunks; no network. (conftest blanket-blocks the real client.)
# --------------------------------------------------------------------------- #


class _FakeFC:
    def __init__(self, name, args):
        self.name = name
        self.args = args


class _FakePart:
    def __init__(self, text=None, function_call=None, thought_signature=None):
        self.text = text
        self.function_call = function_call
        self.thought_signature = thought_signature


class _FakeUsage:
    def __init__(self, prompt, out):
        self.prompt_token_count = prompt
        self.candidates_token_count = out


class _FakeChunk:
    def __init__(self, parts=None, usage=None):
        cand = type("C", (), {"content": type("Ct", (), {"parts": parts or []})()})()
        self.candidates = [cand]
        self.usage_metadata = usage


class _FakeGeminiClient:
    """Returns one canned chunk-stream per generate_content_stream() call, in
    order, and records the (contents, config) it was handed for assertions."""
    def __init__(self, responses, captured):
        self._responses = list(responses)
        self._captured = captured
        self._i = 0
        outer = self

        class _Models:
            def generate_content_stream(self, model=None, contents=None, config=None):
                outer._captured.setdefault("calls", []).append(
                    {"model": model, "contents": contents, "config": config})
                r = outer._responses[outer._i]
                outer._i += 1
                return iter(r)

        self.models = _Models()


def _patch_gemini(monkeypatch, responses, captured=None):
    """responses: a list of chunk-streams (one per model round-trip). Returns one
    persistent fake client so its per-call index survives across the multiple
    _gemini_client() constructions a multi-turn advance() makes."""
    captured = captured if captured is not None else {}
    client = _FakeGeminiClient(responses, captured)
    # _gemini_client now lives on the Gemini backend module, where _gemini_chat
    # resolves it — patch it there.
    monkeypatch.setattr(gemini_backend, "_gemini_client", lambda timeout=None: client)
    return captured


def test_gemini_backend_reassembles_text_and_tool_calls(monkeypatch):
    stream = [
        _FakeChunk(parts=[_FakePart(text="Hel")]),
        _FakeChunk(parts=[_FakePart(text="lo")]),
        _FakeChunk(parts=[_FakePart(function_call=_FakeFC("search_web", {"query": "x"}))],
                   usage=_FakeUsage(5, 3)),
    ]
    _patch_gemini(monkeypatch, [stream])

    message = loop._gemini_chat([{"role": "user", "content": "hey"}], tools=[])

    assert message["role"] == "assistant"
    assert message["content"] == "Hello"
    # canonical shape: arguments is a dict, so _execute_tool_call is unchanged
    assert message["tool_calls"] == [
        {"function": {"name": "search_web", "arguments": {"query": "x"}}}]


def test_gemini_captures_thought_signature_on_tool_calls(monkeypatch):
    # Thinking models stamp each functionCall with an opaque thought_signature; it
    # must be carried through the canonical shape (base64, to stay JSON-serializable)
    # so it can be echoed back next turn.
    stream = [_FakeChunk(parts=[_FakePart(
        function_call=_FakeFC("fetch_webpage", {"url": "x"}),
        thought_signature=b"sig-bytes")], usage=_FakeUsage(5, 3))]
    _patch_gemini(monkeypatch, [stream])

    message = loop._gemini_chat([{"role": "user", "content": "summarize x"}], tools=[])
    tc = message["tool_calls"][0]
    assert tc["function"]["name"] == "fetch_webpage"
    assert tc["thought_signature"] == base64.b64encode(b"sig-bytes").decode()
    # Still JSON-serializable — it flows through _message_chars / _trim_history.
    _json.dumps(tc)


def test_gemini_replays_thought_signature_into_the_functioncall_part(monkeypatch):
    # The follow-up turn must re-attach the signature to the model's functionCall
    # Part, or Gemini 400s ("Function call is missing a thought_signature"). This is
    # the second-round-trip failure that broke escalating a tool-using turn.
    captured = _patch_gemini(monkeypatch, [[_FakeChunk(parts=[_FakePart(text="ok")])]])
    messages = [
        {"role": "user", "content": "summarize x"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "fetch_webpage", "arguments": {"url": "x"}},
             "thought_signature": base64.b64encode(b"sig-bytes").decode()}]},
        {"role": "tool", "content": '{"text": "..."}'},
    ]

    loop._gemini_chat(messages, tools=[])
    model_part = captured["calls"][0]["contents"][1].parts[0]
    assert model_part.thought_signature == b"sig-bytes"  # decoded back to bytes


def test_gemini_tolerates_a_tool_call_without_a_thought_signature(monkeypatch):
    # Thinking off (or a call replayed from another backend) → no signature to
    # replay, and none is set. Must not crash.
    captured = _patch_gemini(monkeypatch, [[_FakeChunk(parts=[_FakePart(text="ok")])]])
    messages = [
        {"role": "user", "content": "weather?"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "fetch_weather", "arguments": {"city": "Boston"}}}]},
        {"role": "tool", "content": '{"temp": 70}'},
    ]

    loop._gemini_chat(messages, tools=[])
    model_part = captured["calls"][0]["contents"][1].parts[0]
    assert model_part.thought_signature is None


def test_gemini_disables_thinking_and_sets_output_cap_by_default(monkeypatch):
    # Thinking is a *thinking* model's default, and thinking tokens count against
    # the output cap — leaving it on starved the weekly-learnings draft. Default
    # to budget 0 (off) with generous output headroom.
    monkeypatch.delenv("WREN_GEMINI_THINKING_BUDGET", raising=False)
    monkeypatch.delenv("WREN_GEMINI_MAX_OUTPUT_TOKENS", raising=False)
    captured = _patch_gemini(monkeypatch, [[_FakeChunk(parts=[_FakePart(text="ok")])]])

    loop._gemini_chat([{"role": "user", "content": "hey"}], tools=[])

    config = captured["calls"][0]["config"]
    assert config.thinking_config.thinking_budget == 0
    assert config.max_output_tokens == 8192


def test_gemini_thinking_budget_and_cap_are_env_overridable(monkeypatch):
    monkeypatch.setenv("WREN_GEMINI_THINKING_BUDGET", "1024")
    monkeypatch.setenv("WREN_GEMINI_MAX_OUTPUT_TOKENS", "2048")
    captured = _patch_gemini(monkeypatch, [[_FakeChunk(parts=[_FakePart(text="ok")])]])

    loop._gemini_chat([{"role": "user", "content": "hey"}], tools=[])

    config = captured["calls"][0]["config"]
    assert config.thinking_config.thinking_budget == 1024
    assert config.max_output_tokens == 2048


def test_gemini_hoists_system_and_pairs_tool_result(monkeypatch):
    captured = _patch_gemini(monkeypatch, [[_FakeChunk(parts=[_FakePart(text="ok")])]])
    messages = [
        {"role": "system", "content": "You are Wren"},
        {"role": "user", "content": "weather?"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"function": {"name": "fetch_weather", "arguments": {"city": "Boston"}}}]},
        {"role": "tool", "content": '{"temp": 70}'},
    ]

    loop._gemini_chat(messages, tools=[])

    call = captured["calls"][0]
    # system message hoisted to system_instruction, not left in contents
    assert call["config"].system_instruction == "You are Wren"
    roles = [c.role for c in call["contents"]]
    assert roles == ["user", "model", "user"]
    # the tool result is paired (by name) with the preceding function call
    fr = call["contents"][2].parts[0].function_response
    assert fr.name == "fetch_weather"
    assert fr.response == {"temp": 70}


def test_gemini_pairing_survives_a_dropped_batched_call(monkeypatch):
    # advance() drops batched calls after a confirm-gated one, so an assistant
    # turn can emit two calls and produce only one result. The orphaned name must
    # not leak into the next turn: pairing is positional (canonical tool messages
    # carry no call id), so a stale name would mislabel every later result.
    captured = _patch_gemini(monkeypatch, [[_FakeChunk(parts=[_FakePart(text="ok")])]])
    messages = [
        {"role": "user", "content": "send it and check the weather"},
        # Two calls emitted; only send_email's result comes back (fetch_weather dropped).
        {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "send_email", "arguments": {"subject": "hi"}}},
            {"function": {"name": "fetch_weather", "arguments": {"city": "Boston"}}}]},
        {"role": "tool", "content": '{"sent": true}'},
        # A later, unrelated turn: its result must pair with ITS call.
        {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "list_reminders", "arguments": {}}}]},
        {"role": "tool", "content": '{"reminders": []}'},
    ]

    loop._gemini_chat(messages, tools=[])

    responses = [p.function_response for c in captured["calls"][0]["contents"]
                 for p in c.parts if getattr(p, "function_response", None)]
    assert [r.name for r in responses] == ["send_email", "list_reminders"]
    assert responses[1].response == {"reminders": []}


def test_gemini_prefers_the_tool_name_on_the_result(monkeypatch):
    # _execute_tool_call now stamps tool_name; the positional fallback above
    # stays for messages built before it, or replayed from another backend by
    # the escalation path. Here the name is authoritative even though the
    # positional queue would have picked the other call.
    captured = _patch_gemini(monkeypatch, [[_FakeChunk(parts=[_FakePart(text="ok")])]])
    messages = [
        {"role": "user", "content": "send it and check the weather"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "send_email", "arguments": {"subject": "hi"}}},
            {"function": {"name": "fetch_weather", "arguments": {"city": "Boston"}}}]},
        {"role": "tool", "tool_name": "fetch_weather", "content": '{"temp": 70}'},
    ]

    loop._gemini_chat(messages, tools=[])

    responses = [p.function_response for c in captured["calls"][0]["contents"]
                 for p in c.parts if getattr(p, "function_response", None)]
    assert [r.name for r in responses] == ["fetch_weather"]


def test_gemini_should_cancel_interrupts(monkeypatch):
    stream = [_FakeChunk(parts=[_FakePart(text="partial")]), _FakeChunk(parts=[])]
    _patch_gemini(monkeypatch, [stream])

    with pytest.raises(loop.TurnCancelled):
        loop._gemini_chat([{"role": "user", "content": "hey"}],
                          should_cancel=lambda: True)


def test_resolve_backend_precedence(monkeypatch):
    monkeypatch.delenv("WREN_LLM_BACKEND", raising=False)
    monkeypatch.delenv("WREN_DAILY_CHROME_LEARNINGS_BACKEND", raising=False)
    assert loop.resolve_backend("daily_chrome_learnings") is None

    monkeypatch.setenv("WREN_LLM_BACKEND", "ollama")
    assert loop.resolve_backend("daily_chrome_learnings") == "ollama"

    # per-task var wins over the global default
    monkeypatch.setenv("WREN_DAILY_CHROME_LEARNINGS_BACKEND", "gemini")
    assert loop.resolve_backend("daily_chrome_learnings") == "gemini"


def test_llm_chat_dispatch_and_unknown_backend(monkeypatch):
    monkeypatch.delenv("WREN_LLM_BACKEND", raising=False)

    # explicit arg routes to gemini even though the global default is ollama
    _patch_gemini(monkeypatch, [[_FakeChunk(parts=[_FakePart(text="cloud")])]])
    assert loop._llm_chat([{"role": "user", "content": "hi"}],
                          backend="gemini")["content"] == "cloud"

    # global env selects gemini
    monkeypatch.setenv("WREN_LLM_BACKEND", "gemini")
    _patch_gemini(monkeypatch, [[_FakeChunk(parts=[_FakePart(text="cloud2")])]])
    assert loop._llm_chat([{"role": "user", "content": "hi"}])["content"] == "cloud2"

    monkeypatch.setenv("WREN_LLM_BACKEND", "nonsense")
    with pytest.raises(ValueError):
        loop._llm_chat([{"role": "user", "content": "hi"}])


def test_advance_gemini_pauses_on_confirm_and_resumes(monkeypatch):
    """The tool-call round-trip and the confirm/resolve gate survive translation:
    advance() on the cloud backend pauses at a confirm-gated call and resumes to a
    final answer once resolved."""
    first = [_FakeChunk(parts=[_FakePart(
        function_call=_FakeFC("send_email", {"subject": "hi"}))])]
    second = [_FakeChunk(parts=[_FakePart(text="done")])]
    _patch_gemini(monkeypatch, [first, second])

    messages = [{"role": "user", "content": "email them"}]
    result = loop.advance(messages, tools=[], dispatch={}, backend="gemini",
                          confirm_before=frozenset({"send_email"}))
    assert result["type"] == "confirm"
    assert result["call"]["function"]["name"] == "send_email"

    loop.resolve(messages, result["call"], approved=True,
                 dispatch={"send_email": lambda **_: {"sent": True}})
    final = loop.advance(messages, tools=[], dispatch={}, backend="gemini")
    assert final == {"type": "final", "text": "done"}


def test_warm_model_noop_for_cloud_backend(monkeypatch):
    """warm_model short-circuits (True) for a cloud backend without poking Ollama."""
    def boom(*a, **k):
        raise AssertionError("Ollama must not be contacted for a cloud backend")
    monkeypatch.setattr(loop.requests, "post", boom)

    assert loop.warm_model(backend="gemini") is True


def test_active_model_label(monkeypatch):
    monkeypatch.setenv("OLLAMA_MODEL", "gemma4")
    monkeypatch.setenv("WREN_LLM_BACKEND", "ollama")
    assert loop.active_model_label() == "gemma4 (ollama)"

    monkeypatch.setenv("WREN_LLM_BACKEND", "gemini")
    monkeypatch.setenv("WREN_GEMINI_MODEL", "gemini-2.5-flash")
    assert loop.active_model_label() == "gemini-2.5-flash (gemini)"


def test_advance_resends_mutated_tools_list_next_iteration(monkeypatch):
    """The lazy-loading contract: advance() passes the SAME tools list object to
    the model each iteration, so a dispatched tool that appends to that list
    (as chat.server's load_tools does) makes the new schema visible on the very
    next model call — within the same turn."""
    def _schema(name):
        return {"type": "function", "function": {"name": name, "parameters": {}}}

    tools = [_schema("grow")]

    def grow(**_):
        tools.append(_schema("late_tool"))  # mutate the live list in place
        return {"ok": True}

    calls_tools_seen = []
    step = {"n": 0}

    def fake_post(url, json=None, timeout=None, stream=None):
        calls_tools_seen.append([t["function"]["name"] for t in json["tools"]])
        step["n"] += 1
        if step["n"] == 1:
            msg = {"role": "assistant", "content": "",
                   "tool_calls": [{"function": {"name": "grow", "arguments": {}}}]}
        else:
            msg = {"role": "assistant", "content": "done"}
        return _FakeResponse([{"message": msg, "done": True}])

    monkeypatch.setattr(loop.requests, "post", fake_post)
    result = loop.advance([{"role": "user", "content": "go"}], tools, {"grow": grow})

    assert result == {"type": "final", "text": "done"}
    assert calls_tools_seen[0] == ["grow"]                 # first call: pre-load list
    assert calls_tools_seen[1] == ["grow", "late_tool"]    # second call: expanded


# --------------------------------------------------------------------------- #
# The repeated-confirmation guard.
#
# 2026-08-18: "add a calendar event" produced a confirmation card, created the
# event, then produced three more cards for the same write — declining each one
# only fed the next. MAX_TOOL_ITERATIONS bounds the loop INSIDE one advance()
# call, but a gated call returns out of advance(), so the counter reset on every
# continuation and the pause/resolve chain had no bound at all.
# --------------------------------------------------------------------------- #

GATED = frozenset({"log_calendar_event"})
EVENT_ARGS = {"summary": "do yardwork", "start": "2026-08-19T10:00:00",
              "end": "2026-08-19T11:00:00"}


def _gated_tools():
    return [{"type": "function",
             "function": {"name": "log_calendar_event", "parameters": {}}}]


def _gated_call(args):
    return {"function": {"name": "log_calendar_event", "arguments": args}}


def _assistant(args):
    return {"role": "assistant", "content": "",
            "tool_calls": [_gated_call(args)]}


def _script(monkeypatch, replies):
    """Drive advance() with a fixed sequence of assistant messages; the last one
    repeats if the loop asks for more."""
    step = {"n": 0}

    def fake_post(url, json=None, timeout=None, stream=None):
        msg = replies[min(step["n"], len(replies) - 1)]
        step["n"] += 1
        return _FakeResponse([{"message": msg, "done": True}])

    monkeypatch.setattr(loop.requests, "post", fake_post)
    return step


def _counting_dispatch():
    calls = []

    def log_calendar_event(**kwargs):
        calls.append(kwargs)
        return {"created": True, "when": "Wednesday, August 19, 2026, 10:00 AM to 11:00 AM"}

    return {"log_calendar_event": log_calendar_event}, calls


def test_a_confirmed_write_re_emitted_verbatim_is_not_offered_again(monkeypatch, caplog):
    """The reported bug: tap Confirm, the event is created, and the very next
    model turn asks you to confirm the identical write."""
    dispatch, executed = _counting_dispatch()
    _script(monkeypatch, [_assistant(EVENT_ARGS), _assistant(EVENT_ARGS),
                          {"role": "assistant", "content": "Done — 10 AM Wednesday."}])
    logger = logging.getLogger("test_loop.repeat_confirm")

    messages = [{"role": "user", "content": "put yardwork on for 10"}]
    paused = loop.advance(messages, _gated_tools(), dispatch, confirm_before=GATED)
    assert paused["type"] == "confirm"
    loop.resolve(messages, paused["call"], True, dispatch)

    with caplog.at_level(logging.WARNING, logger=logger.name):
        result = loop.advance(messages, _gated_tools(), dispatch,
                              confirm_before=GATED, logger=logger)

    assert result == {"type": "final", "text": "Done — 10 AM Wednesday."}
    assert len(executed) == 1  # the repeat was suppressed, not run a second time
    assert "suppressed a repeat confirmation" in caplog.text
    assert "already approved" in caplog.text


def test_a_declined_write_re_emitted_verbatim_is_not_offered_again(monkeypatch, caplog):
    """The other half of the loop: declining used to look like a tool failure,
    so the model retried and drew a fresh card."""
    dispatch, executed = _counting_dispatch()
    _script(monkeypatch, [_assistant(EVENT_ARGS), _assistant(EVENT_ARGS),
                          {"role": "assistant", "content": "Cancelled."}])
    logger = logging.getLogger("test_loop.repeat_decline")

    messages = [{"role": "user", "content": "put yardwork on for 10"}]
    paused = loop.advance(messages, _gated_tools(), dispatch, confirm_before=GATED)
    loop.resolve(messages, paused["call"], False, dispatch)

    with caplog.at_level(logging.WARNING, logger=logger.name):
        result = loop.advance(messages, _gated_tools(), dispatch,
                              confirm_before=GATED, logger=logger)

    assert result == {"type": "final", "text": "Cancelled."}
    assert executed == []  # a declined action stays undone
    assert "already declined" in caplog.text


def test_key_order_alone_does_not_defeat_the_repeat_guard(monkeypatch):
    dispatch, executed = _counting_dispatch()
    reordered = {"end": EVENT_ARGS["end"], "start": EVENT_ARGS["start"],
                 "summary": EVENT_ARGS["summary"]}
    _script(monkeypatch, [_assistant(EVENT_ARGS), _assistant(reordered),
                          {"role": "assistant", "content": "Done."}])

    messages = [{"role": "user", "content": "yardwork"}]
    paused = loop.advance(messages, _gated_tools(), dispatch, confirm_before=GATED)
    loop.resolve(messages, paused["call"], True, dispatch)
    result = loop.advance(messages, _gated_tools(), dispatch, confirm_before=GATED)

    assert result["type"] == "final"
    assert len(executed) == 1


def test_a_corrected_write_still_gets_its_own_confirmation(monkeypatch):
    """The guard must not swallow a genuine second intent — a model fixing its
    own wrong time is a different action and still needs the user's tap."""
    dispatch, _ = _counting_dispatch()
    corrected = {**EVENT_ARGS, "start": "2026-08-19T14:00:00"}
    _script(monkeypatch, [_assistant(EVENT_ARGS), _assistant(corrected)])

    messages = [{"role": "user", "content": "yardwork"}]
    paused = loop.advance(messages, _gated_tools(), dispatch, confirm_before=GATED)
    loop.resolve(messages, paused["call"], True, dispatch)
    second = loop.advance(messages, _gated_tools(), dispatch, confirm_before=GATED)

    assert second["type"] == "confirm"
    assert second["call"]["function"]["arguments"]["start"] == "2026-08-19T14:00:00"


def test_a_new_user_message_starts_the_guard_over(monkeypatch):
    """The guard is scoped to the current user-turn: asking for the same event
    again later is a new request, not a repeat."""
    dispatch, _ = _counting_dispatch()
    _script(monkeypatch, [_assistant(EVENT_ARGS)])

    messages = [{"role": "user", "content": "yardwork"}]
    paused = loop.advance(messages, _gated_tools(), dispatch, confirm_before=GATED)
    loop.resolve(messages, paused["call"], True, dispatch)

    messages.append({"role": "user", "content": "actually add it again"})
    again = loop.advance(messages, _gated_tools(), dispatch, confirm_before=GATED)

    assert again["type"] == "confirm"


def test_the_pause_cap_bounds_a_chain_of_differing_writes(monkeypatch, caplog):
    """Exact-match dedupe alone would not stop a model that varied the end time
    by a minute, so the number of confirmations one turn may ask for is capped."""
    dispatch, executed = _counting_dispatch()
    varied = [{**EVENT_ARGS, "end": f"2026-08-19T11:0{i}:00"} for i in range(5)]
    _script(monkeypatch, [_assistant(v) for v in varied]
                         + [{"role": "assistant", "content": "Stopping."}])
    logger = logging.getLogger("test_loop.pause_cap")

    messages = [{"role": "user", "content": "yardwork"}]
    pauses = 0
    with caplog.at_level(logging.WARNING, logger=logger.name):
        while True:
            result = loop.advance(messages, _gated_tools(), dispatch,
                                  confirm_before=GATED, logger=logger)
            if result["type"] != "confirm":
                break
            pauses += 1
            assert pauses <= loop.MAX_GATED_PAUSES_PER_TURN  # never runs away
            loop.resolve(messages, result["call"], True, dispatch)

    assert pauses == loop.MAX_GATED_PAUSES_PER_TURN
    assert result == {"type": "final", "text": "Stopping."}
    assert len(executed) == loop.MAX_GATED_PAUSES_PER_TURN
    assert "which is the limit" in caplog.text or "cap 3" in caplog.text


def test_a_decline_is_not_shaped_like_a_tool_failure(monkeypatch):
    """{"error": ...} is what a crashed tool returns, and retrying a failure is
    correct model behaviour — which is why a decline used to draw a new card."""
    messages = []
    loop.resolve(messages, _gated_call(EVENT_ARGS), False, {})

    result = _json.loads(messages[0]["content"])
    assert "error" not in result
    assert result["declined"] is True
    assert messages[0]["tool_name"] == "log_calendar_event"


def test_an_executed_tool_result_names_the_call_it_answers(monkeypatch):
    """Without tool_name the model sees an unlabelled blob after its own
    tool_call and can't tell the call ran."""
    messages = []
    loop._execute_tool_call(_gated_call(EVENT_ARGS), {"log_calendar_event": lambda **k: {"ok": True}},
                            messages, None)

    assert messages[0]["tool_name"] == "log_calendar_event"
    assert messages[0]["role"] == "tool"


def test_suppressed_calls_are_not_offered_as_ungated_execution(monkeypatch):
    """A suppressed repeat must stay unexecuted — the whole point is that the
    user's single tap authorised exactly one write."""
    dispatch, executed = _counting_dispatch()
    _script(monkeypatch, [_assistant(EVENT_ARGS), _assistant(EVENT_ARGS),
                          {"role": "assistant", "content": "Done."}])

    messages = [{"role": "user", "content": "yardwork"}]
    paused = loop.advance(messages, _gated_tools(), dispatch, confirm_before=GATED)
    loop.resolve(messages, paused["call"], True, dispatch)
    loop.advance(messages, _gated_tools(), dispatch, confirm_before=GATED)

    suppressed = [m for m in messages
                  if m.get("role") == "tool" and '"not_run": true' in m["content"]]
    assert len(suppressed) == 1
    assert len(executed) == 1


def test_a_third_emission_inherits_the_suppressed_calls_story(monkeypatch):
    """A model that ignores the first suppression and emits the call a third
    time reads back the not_run result. It must inherit the ORIGINAL outcome —
    telling it the write "succeeded" when it was suppressed would be a lie, and
    telling it "declined" after an approval would be worse."""
    dispatch, executed = _counting_dispatch()
    # Two more identical emissions after the confirmed one, then a final.
    _script(monkeypatch, [_assistant(EVENT_ARGS), _assistant(EVENT_ARGS),
                          _assistant(EVENT_ARGS),
                          {"role": "assistant", "content": "Done."}])

    messages = [{"role": "user", "content": "yardwork"}]
    paused = loop.advance(messages, _gated_tools(), dispatch, confirm_before=GATED)
    loop.resolve(messages, paused["call"], True, dispatch)
    result = loop.advance(messages, _gated_tools(), dispatch, confirm_before=GATED)

    assert result["type"] == "final"
    assert len(executed) == 1  # still exactly the one write the user authorised
    suppressed = [_json.loads(m["content"]) for m in messages
                  if m.get("role") == "tool" and '"not_run"' in m["content"]]
    assert len(suppressed) == 2
    assert all(s["already"] == "approved" for s in suppressed)


def test_a_suppressed_call_past_the_cap_is_labelled_capped(monkeypatch):
    dispatch, _ = _counting_dispatch()
    varied = [{**EVENT_ARGS, "end": f"2026-08-19T11:0{i}:00"} for i in range(4)]
    _script(monkeypatch, [_assistant(v) for v in varied]
                         + [{"role": "assistant", "content": "Stopping."}])

    messages = [{"role": "user", "content": "yardwork"}]
    while True:
        result = loop.advance(messages, _gated_tools(), dispatch, confirm_before=GATED)
        if result["type"] != "confirm":
            break
        loop.resolve(messages, result["call"], True, dispatch)

    capped = [_json.loads(m["content"]) for m in messages
              if m.get("role") == "tool" and '"not_run"' in m["content"]]
    assert capped and capped[0]["already"] == "capped"


# --------------------------------------------------------------------------- #
# Leaked <think> markup. qwen3.8:27b-mlx returned a complete answer followed by
# a bare closing tag, which failed json.loads on output that was otherwise
# entirely correct (docs/model-eval.md, 2026-08-24).
# --------------------------------------------------------------------------- #

def _llm_returning(content, monkeypatch):
    monkeypatch.setattr(loop, "_ollama_chat",
                        lambda messages, **kwargs: {"role": "assistant", "content": content})


def test_orphan_closing_think_tag_is_stripped_so_strict_parsers_survive(monkeypatch):
    monkeypatch.setattr(loop, "_ollama_chat",
                        lambda messages, **kwargs: {"content": '{"1": "7"}\n</think>'})

    out = loop.complete_text("sys", "user")

    assert out == '{"1": "7"}'
    assert _json.loads(out) == {"1": "7"}


def test_a_matched_think_block_is_dropped_and_the_answer_kept(monkeypatch):
    _llm_returning("<think>weighing it up\nstill weighing</think>\nthe answer", monkeypatch)

    assert loop.complete_text("sys", "user") == "the answer"


def test_a_reply_that_is_only_scratchpad_reads_as_empty(monkeypatch):
    # Not a regression: stripping must not manufacture an answer out of
    # reasoning. Empty is what the num_predict warning is there to explain.
    _llm_returning("<think>never got to the point</think>", monkeypatch)

    assert loop.complete_text("sys", "user") == ""


def test_content_without_think_markup_keeps_its_exact_whitespace(monkeypatch):
    monkeypatch.setattr(loop, "_ollama_chat",
                        lambda messages, **kwargs: {"content": "  line\n\n  next  "})

    message = loop._llm_chat([{"role": "user", "content": "hi"}])

    assert message["content"] == "  line\n\n  next  "


def test_the_chat_path_strips_leaked_markup_too(monkeypatch):
    _llm_returning("Added it.\n</think>", monkeypatch)

    result = loop.advance([{"role": "user", "content": "hi"}], [], {})

    assert result["type"] == "final"
    assert result["text"] == "Added it."


def test_missing_content_does_not_crash_the_seam(monkeypatch):
    monkeypatch.setattr(loop, "_ollama_chat",
                        lambda messages, **kwargs: {"role": "assistant", "tool_calls": []})

    assert loop._llm_chat([{"role": "user", "content": "hi"}])["content"] == ""
