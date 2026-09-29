"""Tests for tasks/blog_comments.py — main() pushes one count-only summary when
comments are pending, stays silent when none are, and takes the failure path on
an API error. The D1 call and notify are monkeypatched; no network."""

import pytest
import requests

from tasks import blog_comments as bc


class _Resp:
    def __init__(self, data, status=200):
        self._data, self.status_code = data, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code}", response=self)

    def json(self):
        return self._data


def _d1(rows):
    return {"success": True, "errors": [], "result": [{"results": rows}]}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("CF_ACCOUNT_ID", "acct")
    monkeypatch.setenv("CF_D1_DATABASE_ID", "db")
    monkeypatch.setenv("CF_D1_TOKEN", "tok")
    pushes, failures, calls = [], [], []
    monkeypatch.setattr(bc, "notify", lambda **kw: pushes.append(kw) or {"ok": True})
    monkeypatch.setattr(bc, "notify_failure", lambda *a, **k: failures.append(a))

    def respond(data, status=200):
        def post(url, **kw):
            calls.append((url, kw))
            return _Resp(data, status)
        monkeypatch.setattr(bc.requests, "post", post)

    return {"pushes": pushes, "failures": failures, "calls": calls, "respond": respond}


def test_nothing_pending_sends_no_push(env):
    env["respond"](_d1([]))
    assert bc.main() == 0
    assert env["pushes"] == [] and env["failures"] == []


def test_pending_sends_one_count_only_push(env):
    env["respond"](_d1([{"post_slug": "cheap-enough-to-be-wrong", "n": 2},
                        {"post_slug": "managing-the-context-window", "n": 1}]))
    assert bc.main() == 0
    assert len(env["pushes"]) == 1
    push = env["pushes"][0]
    assert push["message"] == ("3 comments waiting (2 on cheap-enough-to-be-wrong, "
                               "1 on managing-the-context-window)")
    assert push["actions"][0]["url"] == bc.MODERATION_URL
    assert push["email_fallback"] is True
    assert env["failures"] == []


def test_query_selects_no_names_or_bodies(env):
    # Stranger text never leaves D1: the query itself asks for counts and slugs.
    env["respond"](_d1([]))
    bc.main()
    url, kw = env["calls"][0]
    assert url.endswith("/accounts/acct/d1/database/db/query")
    assert kw["headers"]["Authorization"] == "Bearer tok"
    assert kw["timeout"] == bc.TIMEOUT_S
    sql = kw["json"]["sql"].lower()
    assert "body" not in sql and "name" not in sql and "status = 'pending'" in sql


def test_http_error_takes_failure_path(env):
    env["respond"]({}, status=403)
    assert bc.main() == 1
    assert env["pushes"] == []
    assert env["failures"] and "HTTP 403" in env["failures"][0][1]


def test_unsuccessful_response_takes_failure_path(env):
    env["respond"]({"success": False, "errors": [{"code": 7500, "message": "bad"}]})
    assert bc.main() == 1
    assert env["pushes"] == [] and len(env["failures"]) == 1


def test_missing_setting_fails_without_calling_cloudflare(env, monkeypatch):
    monkeypatch.setenv("CF_D1_TOKEN", "")
    env["respond"](_d1([]))
    assert bc.main() == 1
    assert env["calls"] == []
    assert "CF_D1_TOKEN" in env["failures"][0][1]


def test_push_failure_with_email_fallback_still_completes(env, monkeypatch):
    env["respond"](_d1([{"post_slug": "a", "n": 1}]))
    monkeypatch.setattr(bc, "notify", lambda **kw: {"error": "down", "email_fallback": {"ok": True}})
    assert bc.main() == 0


def test_push_and_email_both_failing_returns_error(env, monkeypatch):
    env["respond"](_d1([{"post_slug": "a", "n": 1}]))
    monkeypatch.setattr(bc, "notify", lambda **kw: {"error": "down", "email_fallback": {"error": "no"}})
    assert bc.main() == 1


def test_summary_singular():
    assert bc.summarize([{"post_slug": "a", "n": 1}]) == "1 comment waiting (1 on a)"
