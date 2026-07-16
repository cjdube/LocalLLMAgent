"""Tests for the ntfy push tool. requests.post/get are monkeypatched, so no
network runs — the tests exercise the missing-config guard, header/body
assembly, the never-raise error contract, and ntfy_health's three states."""

import requests

from agent.tools import email as email_mod
from agent.tools import notify as notify_mod
from agent.tools.notify import notify, ntfy_health


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


# --------------------------------------------------------------------------- #
# email fallback — so a dead push channel can't silently swallow an alert
# --------------------------------------------------------------------------- #

def _capture_email(monkeypatch):
    """Capture the fallback's send_email. _fallback_email imports it inside the
    function, so patching the email module is what that call resolves — and it
    overrides conftest's suite-wide _block_email_send guard for these tests."""
    sent = []

    def fake_send(subject, body):
        sent.append({"subject": subject, "body": body})
        return {"message_id": "m1"}

    monkeypatch.setattr(email_mod, "send_email", fake_send)
    return sent


def test_failed_push_falls_back_to_email(monkeypatch):
    monkeypatch.setenv("NTFY_URL", "http://box.ts.net:2586/wren-alerts")
    _capture_post(monkeypatch, raises=requests.exceptions.ConnectionError("refused"))
    sent = _capture_email(monkeypatch)

    result = notify("brief failed", title="Wren", email_fallback=True)

    assert "error" in result                      # still reports the push failure
    assert result["email_fallback"] == {"message_id": "m1"}
    assert len(sent) == 1
    assert "brief failed" in sent[0]["body"]
    assert "push failed" in sent[0]["subject"]
    assert "refused" in sent[0]["body"]           # why it didn't land


def test_fallback_is_off_by_default(monkeypatch):
    """reminder_sweep retries a failed push every 60s — a default-on fallback
    would have emailed thousands of times during the July 2026 outage."""
    monkeypatch.setenv("NTFY_URL", "http://box.ts.net:2586/wren-alerts")
    _capture_post(monkeypatch, raises=requests.exceptions.ConnectionError("refused"))
    sent = _capture_email(monkeypatch)

    result = notify("a reminder")

    assert "error" in result
    assert sent == []
    assert "email_fallback" not in result


def test_successful_push_never_emails(monkeypatch):
    monkeypatch.setenv("NTFY_URL", "http://box.ts.net:2586/wren-alerts")
    _capture_post(monkeypatch)
    sent = _capture_email(monkeypatch)

    assert notify("hi", email_fallback=True) == {"ok": True}
    assert sent == []


def test_unset_url_does_not_email(monkeypatch):
    """An unset NTFY_URL means push is switched off on purpose, not broken."""
    monkeypatch.setattr(notify_mod, "load_env", lambda: None)
    monkeypatch.delenv("NTFY_URL", raising=False)
    sent = _capture_email(monkeypatch)

    result = notify("hi", email_fallback=True)

    assert "error" in result
    assert sent == []


def test_email_failure_never_raises(monkeypatch):
    """The fallback must not mask the push failure it's reporting."""
    monkeypatch.setenv("NTFY_URL", "http://box.ts.net:2586/wren-alerts")
    _capture_post(monkeypatch, raises=requests.exceptions.ConnectionError("refused"))
    monkeypatch.setattr(email_mod, "send_email",
                        lambda **k: (_ for _ in ()).throw(RuntimeError("gmail down")))

    result = notify("hi", title="T", email_fallback=True)

    assert "error" in result                                  # the original failure survives
    assert "gmail down" in result["email_fallback"]["error"]


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


# --------------------------------------------------------------------------- #
# ntfy_health — the dashboard pill's probe (and the log inspector's 8am check)
# --------------------------------------------------------------------------- #

class _FakeHealthResp:
    def __init__(self, body=None, status=200):
        self._body = body if body is not None else {"healthy": True}
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"HTTP {self.status_code}", response=self)

    def json(self):
        return self._body


def _capture_get(monkeypatch, resp=None, raises=None):
    """Wire requests.get to capture its call and return resp (or raise).
    Also no-op load_env so a real config/.env can't override the test's env."""
    monkeypatch.setattr(notify_mod, "load_env", lambda: None)
    captured = {}

    def fake_get(url, timeout=None):
        captured.update(url=url, timeout=timeout)
        if raises is not None:
            raise raises
        return resp or _FakeHealthResp()

    monkeypatch.setattr(notify_mod.requests, "get", fake_get)
    return captured


def test_health_off_when_url_unset_without_probing(monkeypatch):
    monkeypatch.setattr(notify_mod, "load_env", lambda: None)
    monkeypatch.delenv("NTFY_URL", raising=False)
    # A stub that would blow up if called, proving we never reach the network.
    monkeypatch.setattr(notify_mod.requests, "get", lambda *a, **k: 1 / 0)

    # "off" is push switched off on purpose (see README), so it carries no
    # error — the log inspector reads `error` alone and must stay quiet here.
    assert ntfy_health() == {"state": "off", "error": None}


def test_health_ok_probes_base_url_not_topic(monkeypatch):
    monkeypatch.setenv("NTFY_URL", "http://box.ts.net:2586/wren-alerts")
    captured = _capture_get(monkeypatch)

    assert ntfy_health() == {"state": "ok", "error": None}
    # /v1/health hangs off the server root; the topic path would 404.
    assert captured["url"] == "http://box.ts.net:2586/v1/health"
    assert captured["timeout"] == notify_mod._HEALTH_TIMEOUT_S


def test_health_down_when_server_reports_unhealthy(monkeypatch):
    monkeypatch.setenv("NTFY_URL", "http://box.ts.net:2586/wren-alerts")
    _capture_get(monkeypatch, resp=_FakeHealthResp({"healthy": False}))

    result = ntfy_health()

    assert result["state"] == "down"
    assert "unhealthy" in result["error"]


def test_health_down_when_unreachable_never_raises(monkeypatch):
    monkeypatch.setenv("NTFY_URL", "http://box.ts.net:2586/wren-alerts")
    _capture_get(monkeypatch, raises=requests.exceptions.ConnectionError("refused"))

    # Same never-raise contract as notify(): the 8am inspector and the dashboard
    # endpoint both call this on a path where an exception would be the bug.
    result = ntfy_health()

    assert result["state"] == "down"
    assert "ntfy unreachable" in result["error"] and "refused" in result["error"]
