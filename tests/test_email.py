"""Tests for agent/tools/email.py — the real Gmail send path (builds the MIME
message, pins the recipient, maps errors) and the model-facing wrapper that
drops injected arguments. The Gmail client is stubbed.

conftest's autouse _block_email_send replaces the *attribute* email.send_email
with a raising stub; importing the function by name here binds the real
implementation, which the attribute patch can't shadow."""

import base64
from email import message_from_bytes

from agent.tools import email as email_mod
from agent.tools.email import (  # real functions, bound at import time
    reply_plan,
    reply_to_thread,
    send_email,
)


def _patch_gmail(monkeypatch, box):
    """Stub build_service with a fluent fake that records the sent raw message."""
    class _Chain:
        def users(self):
            return self

        def messages(self):
            return self

        def send(self, userId=None, body=None):
            box["userId"] = userId
            box["raw"] = body["raw"]
            # The whole send body, so a reply test can assert threadId — that is
            # what keeps the reply in the conversation on our side.
            box["send_body"] = body
            return self

        def execute(self):
            return {"id": "msg-1"}

    def fake_build_service(api, version):
        box["api"] = (api, version)
        return _Chain()

    monkeypatch.setattr(email_mod, "build_service", fake_build_service)


def _decode(raw):
    return message_from_bytes(base64.urlsafe_b64decode(raw))


def test_send_email_builds_plaintext_to_configured_recipient(monkeypatch):
    monkeypatch.setenv("BRIEF_TO_EMAIL", "owner@example.com")
    box = {}
    _patch_gmail(monkeypatch, box)
    result = send_email("Subject line", "Hello there")
    assert result == {"message_id": "msg-1"}
    assert box["userId"] == "me" and box["api"] == ("gmail", "v1")
    msg = _decode(box["raw"])
    assert msg["to"] == "owner@example.com"
    assert msg["subject"] == "Subject line"
    assert msg.get_content_type() == "text/plain"
    assert "Hello there" in msg.get_payload(decode=True).decode()


def test_send_email_html_flag_sets_html_content_type(monkeypatch):
    monkeypatch.setenv("BRIEF_TO_EMAIL", "owner@example.com")
    box = {}
    _patch_gmail(monkeypatch, box)
    send_email("S", "<b>hi</b>", html=True)
    assert _decode(box["raw"]).get_content_type() == "text/html"


def test_send_email_explicit_to_overrides_default(monkeypatch):
    monkeypatch.setenv("BRIEF_TO_EMAIL", "owner@example.com")
    box = {}
    _patch_gmail(monkeypatch, box)
    send_email("S", "b", to="ops@example.com")
    assert _decode(box["raw"])["to"] == "ops@example.com"


def test_send_email_errors_when_no_recipient(monkeypatch):
    monkeypatch.delenv("BRIEF_TO_EMAIL", raising=False)
    # The recipient check must short-circuit before any Gmail client is built.
    monkeypatch.setattr(email_mod, "build_service",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("build_service reached")))
    out = send_email("S", "b")
    assert "BRIEF_TO_EMAIL" in out["error"]


def test_send_email_maps_api_exception_to_error(monkeypatch):
    monkeypatch.setenv("BRIEF_TO_EMAIL", "owner@example.com")

    def boom(*a, **k):
        raise RuntimeError("gmail 500")
    monkeypatch.setattr(email_mod, "build_service", boom)
    assert send_email("S", "b")["error"] == "gmail 500"


def test_tool_wrapper_drops_injected_recipient_and_html(monkeypatch):
    # The model-facing dispatch entry must ignore a hallucinated/injected to=/
    # html= so a prompt injection can't redirect or reformat the mail — the
    # confirmation card only shows subject and body.
    seen = {}

    def fake(subject, body, to=None, html=False):
        seen.update(subject=subject, body=body, to=to, html=html)
        return {"message_id": "x"}
    monkeypatch.setattr(email_mod, "send_email", fake)
    email_mod.send_email_tool(subject="Hi", body="b", to="attacker@evil.com", html=True)
    assert seen["subject"] == "Hi" and seen["body"] == "b"
    assert seen["to"] is None and seen["html"] is False


# --------------------------------------------------------------------------- #
# reply_to_thread — the recipients are the point.
#
# They are computed from the thread's own headers, never from an argument, so
# these tests are the guard on "an injected address cannot be mailed". Both
# gmail_read collaborators are stubbed: reply_plan reads the thread and the
# mailbox owner's own address, and neither may reach Google here.
# --------------------------------------------------------------------------- #

def _msg(sender, to, cc="", message_id="<a@x>", references="", subject="Walkthrough"):
    return {"from": sender, "to": to, "cc": cc, "subject": subject,
            "rfc_message_id": message_id, "references": references}


def _patch_thread(monkeypatch, messages, me="craig@example.com", count=None,
                  error=None):
    """Stub the two gmail_read calls reply_plan makes."""
    def fake_get_thread(thread_id, char_budget=None):
        box_budget["value"] = char_budget
        if error:
            return {"error": error}
        return {"thread_id": thread_id, "subject": messages[0]["subject"],
                "message_count": len(messages) if count is None else count,
                "messages": messages}

    box_budget = {}
    monkeypatch.setattr(email_mod.gmail_read, "get_thread", fake_get_thread)
    monkeypatch.setattr(email_mod.gmail_read, "my_address", lambda: me)
    return box_budget


def test_reply_goes_to_everyone_already_on_the_thread_except_him(monkeypatch):
    box = {}
    _patch_gmail(monkeypatch, box)
    _patch_thread(monkeypatch, [
        _msg("Dana Fox <dana@acme.com>", "craig@example.com", cc="Sam <sam@acme.com>"),
        _msg("craig@example.com", "Dana Fox <dana@acme.com>", message_id="<b@x>"),
    ])

    result = reply_to_thread("t1", "Thursday works.")

    assert result["message_id"] == "msg-1"
    # Everyone on the thread, himself dropped, first-seen order kept.
    assert result["to"] == ["Dana Fox <dana@acme.com>", "Sam <sam@acme.com>"]
    assert "craig@example.com" not in _decode(box["raw"])["to"]


def test_reply_drops_a_model_supplied_recipient(monkeypatch):
    """The wrapper takes exactly what the schema declares. There is no `to`
    parameter to inject into, and an emitted one must not survive as a kwarg."""
    box = {}
    _patch_gmail(monkeypatch, box)
    _patch_thread(monkeypatch, [_msg("Dana <dana@acme.com>", "craig@example.com")])
    # The wrapper looks reply_to_thread up on the module at call time, so
    # conftest's _block_email_send stub is what it finds. Put the real one back
    # for this test — the wrapper's argument dropping is exactly what's under
    # test, and it only shows on the real send path.
    monkeypatch.setattr(email_mod, "reply_to_thread", reply_to_thread)

    result = email_mod.reply_to_thread_tool(
        thread_id="t1", body="ok", to="attacker@evil.com", cc="attacker@evil.com")

    assert result["to"] == ["Dana <dana@acme.com>"]
    assert "attacker@evil.com" not in _decode(box["raw"])["to"]


def test_reply_sets_the_threading_headers_from_the_newest_message(monkeypatch):
    """Without these the recipient's client shows a new conversation, however
    tidy it looks in our own mailbox."""
    box = {}
    _patch_gmail(monkeypatch, box)
    _patch_thread(monkeypatch, [
        _msg("Dana <dana@acme.com>", "craig@example.com", message_id="<one@x>"),
        _msg("Dana <dana@acme.com>", "craig@example.com",
             message_id="<two@x>", references="<one@x>"),
    ])

    reply_to_thread("t1", "ok")

    msg = _decode(box["raw"])
    assert msg["In-Reply-To"] == "<two@x>"
    assert msg["References"] == "<one@x> <two@x>"


def test_reply_is_sent_on_the_same_gmail_thread(monkeypatch):
    box = {}
    _patch_gmail(monkeypatch, box)
    _patch_thread(monkeypatch, [_msg("Dana <dana@acme.com>", "craig@example.com")])

    reply_to_thread("t1", "ok")

    assert box["send_body"]["threadId"] == "t1"


def test_reply_subject_gets_one_re_prefix_and_only_one(monkeypatch):
    box = {}
    _patch_gmail(monkeypatch, box)
    _patch_thread(monkeypatch,
                  [_msg("Dana <dana@acme.com>", "craig@example.com", subject="Walkthrough")])
    assert reply_to_thread("t1", "ok")["subject"] == "Re: Walkthrough"

    _patch_thread(monkeypatch,
                  [_msg("Dana <dana@acme.com>", "craig@example.com", subject="Re: Walkthrough")])
    assert reply_to_thread("t1", "ok")["subject"] == "Re: Walkthrough"


def test_reply_reads_the_whole_thread_not_the_model_sized_slice(monkeypatch):
    """gmail_read's budget protects the model's context. Trimming here would
    drop the OLDEST messages, and with them anyone who has not written lately."""
    box = {}
    _patch_gmail(monkeypatch, box)
    budget = _patch_thread(monkeypatch,
                           [_msg("Dana <dana@acme.com>", "craig@example.com")])

    reply_to_thread("t1", "ok")

    assert budget["value"] == email_mod.THREAD_READ_BUDGET


def test_reply_refuses_when_older_messages_were_dropped(monkeypatch):
    """A short recipient list is the dangerous degrade: the reply looks sent and
    quietly leaves someone off. Say so instead."""
    box = {}
    _patch_gmail(monkeypatch, box)
    _patch_thread(monkeypatch,
                  [_msg("Dana <dana@acme.com>", "craig@example.com")], count=4)

    out = reply_to_thread("t1", "ok")

    assert "incomplete" in out["error"]
    assert "raw" not in box  # nothing was sent


def test_reply_refuses_a_thread_it_cannot_read(monkeypatch):
    box = {}
    _patch_gmail(monkeypatch, box)
    _patch_thread(monkeypatch, [], error="not found")

    assert reply_to_thread("t1", "ok") == {"error": "not found"}
    assert "raw" not in box


def test_reply_refuses_a_thread_with_no_one_but_him_on_it(monkeypatch):
    box = {}
    _patch_gmail(monkeypatch, box)
    _patch_thread(monkeypatch, [_msg("craig@example.com", "craig@example.com")])

    assert "no one on it but you" in reply_to_thread("t1", "ok")["error"]
    assert "raw" not in box


def test_reply_refuses_a_thread_with_too_many_participants(monkeypatch):
    """A mailing list. Silently trimming the list would be worse than refusing."""
    box = {}
    _patch_gmail(monkeypatch, box)
    crowd = ", ".join(f"p{i}@acme.com" for i in range(email_mod.MAX_REPLY_RECIPIENTS + 1))
    _patch_thread(monkeypatch, [_msg("Dana <dana@acme.com>", crowd)])

    assert "reply in Gmail instead" in reply_to_thread("t1", "ok")["error"]
    assert "raw" not in box


def test_reply_needs_a_body(monkeypatch):
    box = {}
    _patch_gmail(monkeypatch, box)
    # The body check must short-circuit before the thread is even read.
    monkeypatch.setattr(email_mod.gmail_read, "get_thread",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("thread read")))

    assert "needs a body" in reply_to_thread("t1", "   ")["error"]


def test_reply_needs_a_thread_id(monkeypatch):
    assert "needs a thread_id" in reply_plan("")["error"]


def test_a_profile_failure_still_replies_rather_than_dropping_it(monkeypatch):
    """my_address() returning "" means we cannot tell his messages from anyone
    else's. The cost of guessing is a copy to himself, which he can see; the
    cost of failing is a reply he believes went out."""
    box = {}
    _patch_gmail(monkeypatch, box)
    _patch_thread(monkeypatch,
                  [_msg("Dana <dana@acme.com>", "craig@example.com")], me="")
    monkeypatch.setenv("BRIEF_TO_EMAIL", "")

    result = reply_to_thread("t1", "ok")

    assert result["message_id"] == "msg-1"
    assert "dana@acme.com" in ", ".join(result["to"])
