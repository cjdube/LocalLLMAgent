"""Tests for agent.loop._ollama_chat — the single choke point for model calls.

Focus: the payload sets an explicit `num_ctx` (env-overridable) and the call
logs the effective context window plus the actual prompt token count, warning
when the prompt reaches the ceiling (likely front-truncation).
"""

import logging

from agent import loop


class _FakeResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def _patch_post(monkeypatch, captured, response):
    """Capture the outgoing payload and return a canned Ollama response."""
    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["payload"] = json
        return _FakeResponse(response)

    monkeypatch.setattr(loop.requests, "post", fake_post)


def test_payload_sets_default_num_ctx(monkeypatch):
    monkeypatch.delenv("OLLAMA_NUM_CTX", raising=False)
    captured = {}
    _patch_post(monkeypatch, captured, {"message": {"content": "hi"}})

    message = loop._ollama_chat([{"role": "user", "content": "hey"}])

    assert captured["payload"]["options"]["num_ctx"] == 8192
    assert message == {"content": "hi"}


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
