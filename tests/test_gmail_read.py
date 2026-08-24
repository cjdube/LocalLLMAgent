"""Tests for the Gmail read tools.

The Gmail client is replaced wholesale with a fake, so nothing here touches the
network or the user's real mailbox. The fake mirrors the shapes the real API
returns — base64url part bodies, epoch-millisecond internalDate, headers as a
list of {name, value} — because those are exactly the details this module
exists to translate.
"""

import base64
import json

import pytest
from googleapiclient.errors import HttpError

from agent import loop
from agent.tools import gmail_read


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def _message(message_id="m1", thread_id="t1", subject="Hello", sender="Jane <jane@acme.com>",
             body="Can we meet Tuesday?", internal_date="1755000000000",
             html_body=None, labels=None, extra_headers=None):
    headers = [
        {"name": "From", "value": sender},
        {"name": "To", "value": "craig@example.com"},
        {"name": "Subject", "value": subject},
        {"name": "Message-ID", "value": f"<{message_id}@acme.com>"},
    ]
    headers += extra_headers or []

    payload = {"headers": headers, "mimeType": "multipart/alternative", "parts": []}
    if body is not None:
        payload["parts"].append({"mimeType": "text/plain", "body": {"data": _b64(body)}})
    if html_body is not None:
        payload["parts"].append({"mimeType": "text/html", "body": {"data": _b64(html_body)}})

    return {
        "id": message_id,
        "threadId": thread_id,
        "snippet": (body or "")[:80],
        "internalDate": internal_date,
        "labelIds": labels or ["INBOX", "Label_7"],
        "payload": payload,
    }


class _FakeGmail:
    """Just enough of the Gmail client for these tests."""

    def __init__(self, messages=None, threads=None, labels=None,
                 history_pages=None, profile_history_id="9000"):
        self.messages_by_id = {m["id"]: m for m in messages or []}
        self.thread_data = threads or {}
        self.label_data = labels if labels is not None else [
            {"id": "Label_7", "name": "Wren/Watch"},
            {"id": "INBOX", "name": "INBOX"},
        ]
        self.history_pages = history_pages or []
        self.profile_history_id = profile_history_id
        self.watch_calls = []
        self.search_results = []

    # The client is service.users().messages().get(...).execute()
    def users(self):
        return self

    def messages(self):
        return _FakeMessages(self)

    def threads(self):
        return _FakeThreads(self)

    def labels(self):
        return _FakeLabels(self)

    def history(self):
        return _FakeHistory(self)

    def getProfile(self, userId=None):
        return _Exec({"historyId": self.profile_history_id})

    def watch(self, userId=None, body=None):
        self.watch_calls.append(body)
        return _Exec({"historyId": "1234", "expiration": "1756000000000"})


class _Exec:
    def __init__(self, value):
        self.value = value

    def execute(self):
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class _FakeMessages:
    def __init__(self, gmail):
        self.gmail = gmail

    def get(self, userId=None, id=None, format=None):
        message = self.gmail.messages_by_id.get(id)
        if message is None:
            return _Exec(_http_error(404, "Not Found"))
        return _Exec(message)

    def list(self, userId=None, q=None, maxResults=None):
        ids = self.gmail.search_results[:maxResults]
        return _Exec({"messages": [{"id": i} for i in ids]})


class _FakeThreads:
    def __init__(self, gmail):
        self.gmail = gmail

    def get(self, userId=None, id=None, format=None):
        thread = self.gmail.thread_data.get(id)
        if thread is None:
            return _Exec(_http_error(404, "Not Found"))
        return _Exec(thread)


class _FakeLabels:
    def __init__(self, gmail):
        self.gmail = gmail

    def list(self, userId=None):
        return _Exec({"labels": self.gmail.label_data})


class _FakeHistory:
    def __init__(self, gmail):
        self.gmail = gmail

    def list(self, userId=None, startHistoryId=None, historyTypes=None,
             labelId=None, pageToken=None):
        self.gmail.last_history_call = {
            "startHistoryId": startHistoryId,
            "historyTypes": historyTypes,
            "labelId": labelId,
            "pageToken": pageToken,
        }
        page_index = 0 if pageToken is None else int(pageToken)
        if page_index >= len(self.gmail.history_pages):
            return _Exec({"historyId": self.gmail.profile_history_id})
        return _Exec(self.gmail.history_pages[page_index])


class _FakeResponse:
    def __init__(self, status):
        self.status = status
        self.reason = "error"


def _http_error(status, message):
    return HttpError(_FakeResponse(status), json.dumps({"error": message}).encode())


@pytest.fixture
def gmail(monkeypatch):
    """Install a fake client and hand it back so a test can shape it."""
    fake = _FakeGmail()
    monkeypatch.setattr(gmail_read, "_service", lambda: fake)
    return fake


# --------------------------------------------------------------------------- #
# Message parsing
# --------------------------------------------------------------------------- #

def test_compact_message_pulls_out_the_fields_python_owns():
    result = gmail_read.compact_message(_message())

    assert result["message_id"] == "m1"
    assert result["thread_id"] == "t1"
    assert result["from"] == "Jane <jane@acme.com>"
    assert result["subject"] == "Hello"
    assert result["body"] == "Can we meet Tuesday?"
    assert result["body_truncated"] is False


def test_headers_are_matched_case_insensitively():
    """Header casing varies by sender — 'Message-Id' and 'MESSAGE-ID' are both
    legal and both mean the same header."""
    raw = _message(extra_headers=[{"name": "in-reply-to", "value": "<earlier@acme.com>"}])

    assert gmail_read.compact_message(raw)["in_reply_to"] == "<earlier@acme.com>"


def test_threading_headers_are_returned():
    """Not used today. They cost nothing here and are what a threaded reply
    needs later, and retrofitting them means reopening this module."""
    raw = _message(extra_headers=[{"name": "References", "value": "<a@x> <b@x>"}])
    result = gmail_read.compact_message(raw)

    assert result["rfc_message_id"] == "<m1@acme.com>"
    assert result["references"] == "<a@x> <b@x>"


def test_internal_date_is_converted_to_the_local_day(monkeypatch):
    """Gmail's internalDate is epoch MILLISECONDS in UTC and Wren's days are
    local. The stamp below is 2026-08-13 01:30 UTC — which is still the 12th in
    New York. Slicing the UTC string would put it on the wrong day."""
    monkeypatch.setenv("TIMEZONE", "America/New_York")
    # 2026-08-13T01:30:00Z
    raw = _message(internal_date="1786584600000")

    assert gmail_read.compact_message(raw)["date"].startswith("2026-08-12 21:30")


def test_unparseable_internal_date_yields_an_empty_stamp_not_a_crash():
    assert gmail_read.compact_message(_message(internal_date=None))["date"] == ""


def test_body_is_trimmed_to_the_budget():
    raw = _message(body="word " * 2000)
    result = gmail_read.compact_message(raw, body_chars=100)

    assert len(result["body"]) <= 101  # the budget plus the ellipsis
    assert result["body_truncated"] is True


def test_plain_text_part_wins_over_html():
    raw = _message(body="the plain one", html_body="<p>the html one</p>")

    assert gmail_read.compact_message(raw)["body"] == "the plain one"


def test_html_only_message_falls_back_to_stripped_html():
    raw = _message(body=None, html_body="<style>x{}</style><p>Hi &amp; bye</p>")
    body = gmail_read.compact_message(raw)["body"]

    assert "Hi & bye" in body
    assert "<p>" not in body and "x{}" not in body


def test_quoted_reply_history_is_trimmed_off():
    raw = _message(body="Yes, Tuesday works.\n\nOn Mon, Aug 3, 2026 Jane wrote:\n> the whole first email")
    body = gmail_read.compact_message(raw)["body"]

    assert body == "Yes, Tuesday works."


def test_a_message_that_is_only_a_quote_keeps_its_text():
    """A forward with no note would otherwise trim to nothing and read as an
    empty email."""
    raw = _message(body="On Mon, Aug 3, 2026 Jane wrote:\n> please see below")

    assert gmail_read.compact_message(raw)["body"].startswith("On Mon")


def test_undecodable_part_is_skipped_rather_than_crashing():
    raw = _message(body="fine")
    raw["payload"]["parts"].append({"mimeType": "text/plain", "body": {"data": "!!!not base64!!!"}})

    assert "fine" in gmail_read.compact_message(raw)["body"]


# --------------------------------------------------------------------------- #
# Labels
# --------------------------------------------------------------------------- #

def test_label_id_is_looked_up_from_the_name(gmail):
    assert gmail_read.label_id("Wren/Watch") == {"label_id": "Label_7", "name": "Wren/Watch"}


def test_label_lookup_is_case_insensitive(gmail):
    assert gmail_read.label_id("wren/watch")["label_id"] == "Label_7"


def test_missing_label_returns_an_actionable_error(gmail):
    result = gmail_read.label_id("Wren/Nope")

    assert "error" in result and "Wren/Nope" in result["error"]


# --------------------------------------------------------------------------- #
# Threads
# --------------------------------------------------------------------------- #

def test_thread_is_returned_oldest_first(gmail):
    gmail.thread_data["t1"] = {"messages": [
        _message("m2", subject="Re: Hello", internal_date="1755000200000", body="second"),
        _message("m1", subject="Hello", internal_date="1755000000000", body="first"),
    ]}
    result = gmail_read.get_thread("t1")

    assert [m["body"] for m in result["messages"]] == ["first", "second"]
    assert result["message_count"] == 2


def test_oversized_thread_drops_whole_old_messages_and_says_so(gmail):
    gmail.thread_data["t1"] = {"messages": [
        _message(f"m{i}", internal_date=str(1755000000000 + i * 1000), body=f"message {i}")
        for i in range(10)
    ]}
    result = gmail_read.get_thread("t1", char_budget=900)

    assert result["message_count"] == 10
    assert len(result["messages"]) < 10
    # The newest must survive — it is the one that prompted the read.
    assert result["messages"][-1]["body"] == "message 9"
    assert "left out" in result["note"]


def test_thread_stays_under_its_tool_result_cap(gmail):
    """The number in loop.TOOL_RESULT_CHAR_CAPS is only honest if the tool's own
    budget keeps the payload under it — otherwise the loop re-trims a result
    this module already trimmed on purpose, cutting the note off the end."""
    gmail.thread_data["t1"] = {"messages": [
        _message(f"m{i}", internal_date=str(1755000000000 + i * 1000), body="word " * 5000)
        for i in range(30)
    ]}
    payload = json.dumps(gmail_read.get_thread("t1"))

    assert len(payload) <= loop.TOOL_RESULT_CHAR_CAPS["read_email"]


# --------------------------------------------------------------------------- #
# The model-facing pair
# --------------------------------------------------------------------------- #

def test_read_email_accepts_a_message_id_by_falling_back(gmail):
    """search_mail hands back both ids, and the model will sometimes pass the
    message one. Gmail 404s a message id used as a thread id."""
    gmail.messages_by_id["m1"] = _message("m1")
    result = gmail_read.read_email("m1")

    assert result["message_count"] == 1
    assert result["messages"][0]["message_id"] == "m1"


def test_read_email_needs_an_id():
    assert "error" in gmail_read.read_email("")


def test_search_mail_returns_a_row_per_match(gmail):
    gmail.search_results = ["m1", "m2"]
    gmail.messages_by_id = {
        "m1": _message("m1", subject="Invoice"),
        "m2": _message("m2", subject="Re: Invoice"),
    }
    result = gmail_read.search_mail("subject:invoice")

    assert result["count"] == 2
    assert [r["subject"] for r in result["results"]] == ["Invoice", "Re: Invoice"]


def test_search_mail_with_no_matches_says_to_say_so(gmail):
    """The catalogue rule: an empty result must tell the model to report nothing
    found, or it answers from pretraining and invents an email."""
    result = gmail_read.search_mail("from:nobody")

    assert result["count"] == 0
    assert "nothing" in result["note"].lower()


def test_search_mail_caps_the_requested_limit(gmail):
    gmail.search_results = [f"m{i}" for i in range(50)]
    gmail.messages_by_id = {f"m{i}": _message(f"m{i}") for i in range(50)}
    result = gmail_read.search_mail("anything", limit=999)

    assert result["count"] <= gmail_read.SEARCH_MAX_LIMIT


def test_search_mail_drops_whole_rows_to_stay_in_budget(gmail):
    gmail.search_results = [f"m{i}" for i in range(25)]
    gmail.messages_by_id = {
        f"m{i}": _message(f"m{i}", subject="x" * 200, body="y" * 400) for i in range(25)
    }
    result = gmail_read.search_mail("anything", limit=25)
    payload = json.dumps(result)

    assert len(payload) <= loop.TOOL_RESULT_CHAR_CAPS["search_mail"]
    assert "left out" in result["note"]
    # Every row that survived is whole — a sliced row reads as a real one.
    assert all(r["message_id"] and r["subject"] for r in result["results"])


def test_search_mail_needs_a_query():
    assert "error" in gmail_read.search_mail("   ")


# --------------------------------------------------------------------------- #
# History
# --------------------------------------------------------------------------- #

def test_list_history_collects_added_message_ids(gmail):
    gmail.history_pages = [{
        "history": [
            {"messagesAdded": [{"message": {"id": "m1"}}]},
            {"messagesAdded": [{"message": {"id": "m2"}}]},
        ],
        "historyId": "500",
    }]
    result = gmail_read.list_history("100", "Label_7")

    assert result["message_ids"] == ["m1", "m2"]
    assert result["history_id"] == "500"
    assert result["resynced"] is False
    assert gmail.last_history_call["labelId"] == "Label_7"
    assert gmail.last_history_call["historyTypes"] == ["messageAdded"]


def test_list_history_skips_mail_he_sent_himself(gmail):
    """A Gmail label covers the whole thread, so his own reply on a watched
    thread inherits the watch label. Without the SENT filter the watcher pushes
    an alert about an email he just wrote."""
    gmail.history_pages = [{
        "history": [
            {"messagesAdded": [
                {"message": {"id": "mine", "labelIds": ["SENT", "Label_7"]}},
                {"message": {"id": "theirs", "labelIds": ["INBOX", "Label_7"]}},
            ]},
        ],
        "historyId": "500",
    }]

    assert gmail_read.list_history("100", "Label_7")["message_ids"] == ["theirs"]


def test_list_history_keeps_messages_with_no_labels_listed(gmail):
    # The SENT filter must not turn a missing labelIds field into a drop.
    gmail.history_pages = [{
        "history": [{"messagesAdded": [{"message": {"id": "m1"}}]}],
        "historyId": "500",
    }]

    assert gmail_read.list_history("100")["message_ids"] == ["m1"]


def test_list_history_dedupes_a_message_named_twice(gmail):
    gmail.history_pages = [{
        "history": [
            {"messagesAdded": [{"message": {"id": "m1"}}]},
            {"messagesAdded": [{"message": {"id": "m1"}}]},
        ],
        "historyId": "500",
    }]

    assert gmail_read.list_history("100")["message_ids"] == ["m1"]


def test_list_history_follows_pages(gmail):
    gmail.history_pages = [
        {"history": [{"messagesAdded": [{"message": {"id": "m1"}}]}],
         "historyId": "400", "nextPageToken": "1"},
        {"history": [{"messagesAdded": [{"message": {"id": "m2"}}]}], "historyId": "500"},
    ]
    result = gmail_read.list_history("100")

    assert result["message_ids"] == ["m1", "m2"]
    assert result["history_id"] == "500"


class _Recorder:
    """Stands in for the task logger, so a test can read what was warned about."""

    def __init__(self):
        self.warnings = []

    def warning(self, message):
        self.warnings.append(message)


def test_aged_out_history_id_resyncs_and_logs_a_warning(gmail):
    """Gmail keeps about a week of history. Past that the call 404s — which is
    a LOST WATERMARK, not 'no new mail'. Degrading silently here would mean the
    watcher quietly reports nothing forever, so the resync has to be audible."""
    class _Failing(_FakeHistory):
        def list(self, **kwargs):
            return _Exec(_http_error(404, "Requested entity was not found."))

    gmail.history = lambda: _Failing(gmail)
    gmail.profile_history_id = "9999"
    logger = _Recorder()

    result = gmail_read.list_history("1", "Label_7", logger=logger)

    assert result == {"message_ids": [], "history_id": "9999", "resynced": True}
    assert len(logger.warnings) == 1
    # The warning has to name both ids, or it cannot be acted on.
    assert "1" in logger.warnings[0] and "9999" in logger.warnings[0]


def test_a_non_404_history_error_is_reported_not_swallowed(gmail):
    class _Failing(_FakeHistory):
        def list(self, **kwargs):
            return _Exec(_http_error(500, "backend error"))

    gmail.history = lambda: _Failing(gmail)

    assert "error" in gmail_read.list_history("100")


def test_list_history_needs_a_start_id():
    assert "error" in gmail_read.list_history(None)


# --------------------------------------------------------------------------- #
# Registering the watch
# --------------------------------------------------------------------------- #

def test_register_watch_sends_the_topic_and_label(gmail):
    result = gmail_read.register_watch("projects/p/topics/wren-mail", "Label_7")

    assert gmail.watch_calls == [{
        "topicName": "projects/p/topics/wren-mail",
        "labelFilterBehavior": "INCLUDE",
        "labelIds": ["Label_7"],
    }]
    assert result == {"history_id": "1234", "expiration": "1756000000000"}


def test_register_watch_reports_the_error_rather_than_raising(gmail):
    def _boom(userId=None, body=None):
        return _Exec(_http_error(403, "topic not accessible"))

    gmail.watch = _boom

    assert "error" in gmail_read.register_watch("projects/p/topics/t", "Label_7")
