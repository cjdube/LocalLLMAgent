"""Tests for the ntfy push tool. requests.post is monkeypatched, so no network
runs — the tests exercise the missing-config guard, header/body assembly, and
the never-raise error contract."""

import requests

from agent.tools import notify as notify_mod
from agent.tools.notify import notify


class _FakeResp:
    def __init__(self, status=200):
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"HTTP {self.status_code}", response=self)


def _capture_post(monkeypatch, resp=None, raises=None):
    """Wire requests.post to capture its call and return resp (or raise).
    Also no-op load_env so a real config/.env can't override the test's env."""
    monkeypatch.setattr(notify_mod, "load_env", lambda: None)
    captured = {}

    def fake_post(url, data=None, headers=None, timeout=None):
        captured.update(url=url, data=data, headers=headers, timeout=timeout)
        if raises is not None:
            raise raises
        return resp or _FakeResp()

    monkeypatch.setattr(notify_mod.requests, "post", fake_post)
    return captured


def test_missing_url_returns_error_without_posting(monkeypatch):
    monkeypatch.setattr(notify_mod, "load_env", lambda: None)
    monkeypatch.delenv("NTFY_URL", raising=False)
    # A stub that would blow up if called, proving we never reach the network.
    monkeypatch.setattr(notify_mod.requests, "post", lambda *a, **k: 1 / 0)
    result = notify("anything")
    assert "error" in result and "NTFY_URL" in result["error"]


def test_posts_body_and_auth_header(monkeypatch):
    monkeypatch.setenv("NTFY_URL", "http://box.ts.net:2586/wren-alerts")
    monkeypatch.setenv("NTFY_TOKEN", "tk_secret")
    captured = _capture_post(monkeypatch)

    result = notify("brief failed", title="Wren", priority="high")

    assert result == {"ok": True}
    assert captured["url"] == "http://box.ts.net:2586/wren-alerts"
    assert captured["data"] == b"brief failed"
    assert captured["headers"]["Authorization"] == "Bearer tk_secret"
    assert captured["headers"]["Title"] == "Wren"
    assert captured["headers"]["Priority"] == "high"


def test_omits_auth_header_when_no_token(monkeypatch):
    monkeypatch.setenv("NTFY_URL", "http://box.ts.net:2586/wren-alerts")
    monkeypatch.delenv("NTFY_TOKEN", raising=False)
    captured = _capture_post(monkeypatch)

    assert notify("hi") == {"ok": True}
    assert "Authorization" not in captured["headers"]


def test_http_error_becomes_error_dict(monkeypatch):
    monkeypatch.setenv("NTFY_URL", "http://box.ts.net:2586/wren-alerts")
    _capture_post(monkeypatch, resp=_FakeResp(status=403))
    result = notify("hi")
    assert "error" in result and "403" in result["error"]


def test_network_exception_never_raises(monkeypatch):
    monkeypatch.setenv("NTFY_URL", "http://box.ts.net:2586/wren-alerts")
    _capture_post(monkeypatch, raises=requests.exceptions.ConnectionError("refused"))
    result = notify("hi")
    assert "error" in result


def test_actions_publish_as_json_to_base_url(monkeypatch):
    # With action buttons, notify() must JSON-publish to the server BASE url
    # (with a "topic" field), not POST plaintext to the topic url.
    monkeypatch.setattr(notify_mod, "load_env", lambda: None)
    monkeypatch.setenv("NTFY_URL", "http://box:2586/wren-alerts")
    monkeypatch.setenv("NTFY_TOKEN", "tk_x")
    captured = {}

    def fake_post(url, data=None, json=None, headers=None, timeout=None):
        captured.update(url=url, data=data, json=json, headers=headers)
        return _FakeResp()

    monkeypatch.setattr(notify_mod.requests, "post", fake_post)
    actions = [{"action": "http", "label": "Approve", "url": "https://h/x"}]

    result = notify("do it?", title="T", priority="high", actions=actions)

    assert result == {"ok": True}
    assert captured["url"] == "http://box:2586"           # base, not topic
    assert captured["data"] is None                       # JSON body, not plaintext
    assert captured["json"]["topic"] == "wren-alerts"
    assert captured["json"]["actions"] == actions
    assert captured["json"]["priority"] == 4              # "high" -> int
    assert captured["headers"]["Authorization"] == "Bearer tk_x"
