"""Tests for agent.loop._ollama_chat — the single choke point for model calls.

Focus: the payload sets an explicit `num_ctx` (env-overridable) and the call
logs the effective context window plus the actual prompt token count, warning
when the prompt reaches the ceiling (likely front-truncation).
"""

import json as _json
import logging

import pytest

from agent import loop


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


def test_num_ctx_honors_env_override(monkeypatch):
    monkeypatch.setenv("OLLAMA_NUM_CTX", "16384")
    captured = {}
    _patch_post(monkeypatch, captured, {"message": {"content": "hi"}})

    loop._ollama_chat([{"role": "user", "content": "hey"}])

    assert captured["payload"]["options"]["num_ctx"] == 16384


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


def test_advance_checks_cancel_before_calling_model(monkeypatch):
    """advance() raises TurnCancelled up front without hitting the model when
    the turn is already cancelled."""
    def unexpected(*a, **k):
        raise AssertionError("model should not be called once cancelled")

    monkeypatch.setattr(loop, "_ollama_chat", unexpected)
    with pytest.raises(loop.TurnCancelled):
        loop.advance([], [], {}, should_cancel=lambda: True)


def test_oversized_tool_result_is_truncated(monkeypatch):
    """A tool result larger than the cap is trimmed before being appended, so a
    single huge result can't blow past the context window."""
    monkeypatch.setattr(loop, "MAX_TOOL_RESULT_CHARS", 100)
    call = {"function": {"name": "search_web", "arguments": {}}}
    messages = []
    loop._execute_tool_call(
        call, {"search_web": lambda **_: {"blob": "x" * 5000}}, messages, logger=None
    )

    content = messages[-1]["content"]
    assert "truncated" in content
    assert len(content) < 5000
