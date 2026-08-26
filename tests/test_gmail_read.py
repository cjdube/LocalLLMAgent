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
                 history_pages=None, profile_history_id="9000",
                 profile_address="craig@example.com"):
        self.profile_address = profile_address
        self.profile_calls = 0
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
        # Exceptions for history().list() to raise, one per call, before it
        # starts returning pages. Lets a test reproduce the dead-connection
        # failure the mail_watcher daemon hits after an idle gap.
        self.history_errors = []
        self.history_calls = 0

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
        self.profile_calls += 1
        return _Exec({"historyId": self.profile_history_id,
                      "emailAddress": self.profile_address})

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
        self.gmail.history_calls += 1
        if self.gmail.history_errors:
            return _Exec(self.gmail.history_errors.pop(0))
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


@pytest.fixture(autouse=True)
def _clear_address_cache():
    """my_address() caches for the life of the process, so one test's fake
    address would otherwise answer every later test — and the module under test
    would look correct while the cache did the work."""
    gmail_read._MY_ADDRESS.clear()
    yield
    gmail_read._MY_ADDRESS.clear()


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
    """What reply_plan() puts on an outgoing reply as In-Reply-To/References,
    so the recipient's client shows an answer rather than a new conversation."""
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

def _watched(*message_labels):
    """A thread whose messages carry these label sets, oldest first. Ids and
    stamps are real because `newest` is now part of the answer."""
    return {"messages": [
        {"id": f"tm{n}", "internalDate": str(1000 + n), "labelIds": list(labels)}
        for n, labels in enumerate(message_labels)]}


def test_list_history_collects_added_message_ids(gmail):
    gmail.history_pages = [{
        "history": [
            {"messagesAdded": [{"message": {"id": "m1", "threadId": "t1"}}]},
            {"messagesAdded": [{"message": {"id": "m2", "threadId": "t1"}}]},
        ],
        "historyId": "500",
    }]
    gmail.thread_data = {"t1": _watched(["Label_7", "INBOX"])}
    result = gmail_read.list_history("100", "Label_7")

    assert result["message_ids"] == ["m1", "m2"]
    assert result["history_id"] == "500"
    assert result["resynced"] is False
    assert gmail.last_history_call["historyTypes"] == ["messageAdded", "labelAdded"]
    # The API call must NOT filter by label; see test_list_history_reports_a_...
    assert gmail.last_history_call["labelId"] is None


def test_list_history_reports_a_reply_that_never_got_the_label(gmail):
    """The live miss this design exists for. He labelled the first email on a
    thread by hand; Gmail put the label on that message only. The reply arriving
    later carries INBOX and nothing else, so a label filter — at the API or on
    the message — reports the first email and then goes silent forever."""
    gmail.history_pages = [{
        "history": [{"messagesAdded": [
            {"message": {"id": "reply", "threadId": "t1", "labelIds": ["INBOX"]}},
        ]}],
        "historyId": "500",
    }]
    gmail.thread_data = {"t1": _watched(["Label_7", "INBOX"], ["INBOX"])}

    assert gmail_read.list_history("100", "Label_7")["message_ids"] == ["reply"]


def test_list_history_ignores_mail_on_a_thread_he_never_labelled(gmail):
    gmail.history_pages = [{
        "history": [{"messagesAdded": [
            {"message": {"id": "noise", "threadId": "t9", "labelIds": ["INBOX"]}},
        ]}],
        "historyId": "500",
    }]
    gmail.thread_data = {"t9": _watched(["INBOX"])}

    assert gmail_read.list_history("100", "Label_7")["message_ids"] == []


def test_list_history_resolves_each_thread_once(gmail):
    """Several new messages usually share a thread, and the answer cannot change
    inside one call — so this must not cost one lookup per message."""
    gmail.history_pages = [{
        "history": [{"messagesAdded": [
            {"message": {"id": "a", "threadId": "t1"}},
            {"message": {"id": "b", "threadId": "t1"}},
            {"message": {"id": "c", "threadId": "t1"}},
        ]}],
        "historyId": "500",
    }]
    gmail.thread_data = {"t1": _watched(["Label_7"])}
    lookups = []
    real = gmail.threads

    def _counting():
        thread_api = real()
        get = thread_api.get

        def _get(userId=None, id=None, format=None):
            lookups.append(id)
            return get(userId=userId, id=id, format=format)

        thread_api.get = _get
        return thread_api

    gmail.threads = _counting

    assert gmail_read.list_history("100", "Label_7")["message_ids"] == ["a", "b", "c"]
    assert lookups == ["t1"]


def test_an_unreadable_thread_is_dropped_but_says_so(gmail):
    """Degrading is only allowed out loud: a thread we cannot classify is
    treated as unwatched, so real mail may go unreported."""
    gmail.history_pages = [{
        "history": [{"messagesAdded": [
            {"message": {"id": "m1", "threadId": "gone"}},
        ]}],
        "historyId": "500",
    }]
    gmail.thread_data = {}  # threads().get 404s
    logger = _Recorder()

    result = gmail_read.list_history("100", "Label_7", logger=logger)

    assert result["message_ids"] == []
    assert len(logger.warnings) == 1
    assert "gone" in logger.warnings[0]


def test_list_history_skips_mail_he_sent_himself(gmail):
    """A Gmail label covers the whole thread, so his own reply on a watched
    thread inherits the watch label. Without the SENT filter the watcher pushes
    an alert about an email he just wrote."""
    gmail.history_pages = [{
        "history": [
            {"messagesAdded": [
                {"message": {"id": "mine", "threadId": "t1",
                             "labelIds": ["SENT"]}},
                {"message": {"id": "theirs", "threadId": "t1",
                             "labelIds": ["INBOX"]}},
            ]},
        ],
        "historyId": "500",
    }]
    gmail.thread_data = {"t1": _watched(["Label_7", "INBOX"])}

    assert gmail_read.list_history("100", "Label_7")["message_ids"] == ["theirs"]


def test_list_history_skips_a_draft_he_is_still_writing(gmail):
    """The live false alert a SENT-only filter did not catch: Gmail autosaves a
    reply as a draft before it is sent, the draft is a real message on the
    thread and inherits the watch label, and it carries DRAFT and not SENT — so
    he got pushed his own half-written reply."""
    gmail.history_pages = [{
        "history": [{"messagesAdded": [
            {"message": {"id": "half-typed", "threadId": "t1",
                         "labelIds": ["DRAFT"]}},
        ]}],
        "historyId": "500",
    }]
    gmail.thread_data = {"t1": _watched(["Label_7", "INBOX"])}

    assert gmail_read.list_history("100", "Label_7")["message_ids"] == []


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

    assert result == {"message_ids": [], "message_threads": {}, "threads": {},
                      "history_id": "9999", "resynced": True}
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

def test_register_watch_covers_the_whole_mailbox_not_just_the_label(gmail):
    """A label-filtered watch never publishes for a reply that arrived after the
    label was applied by hand, because Gmail does not put the label on it. No
    downstream filtering can recover a notification that was never sent, so the
    watch has to be unfiltered and the decision made per thread."""
    result = gmail_read.register_watch("projects/p/topics/wren-mail")

    assert gmail.watch_calls == [{"topicName": "projects/p/topics/wren-mail"}]
    assert result == {"history_id": "1234", "expiration": "1756000000000"}


def test_register_watch_reports_the_error_rather_than_raising(gmail):
    def _boom(userId=None, body=None):
        return _Exec(_http_error(403, "topic not accessible"))

    gmail.watch = _boom

    assert "error" in gmail_read.register_watch("projects/p/topics/t")


# --------------------------------------------------------------------------- #
# my_address — identity, not delivery preference. A reply uses it to tell his
# own messages on a thread from everyone else's.
# --------------------------------------------------------------------------- #

def test_my_address_comes_from_the_gmail_profile(gmail):
    gmail.profile_address = "craig@example.com"
    assert gmail_read.my_address() == "craig@example.com"


def test_my_address_is_only_fetched_once(gmail):
    gmail_read.my_address()
    gmail_read.my_address()
    assert gmail.profile_calls == 1


def test_a_failed_profile_read_is_not_cached(monkeypatch, gmail):
    """Caching "" would pin the failure for the life of the daemon, and every
    later reply would quietly include him on his own thread."""
    monkeypatch.setattr(gmail_read, "_service",
                        lambda: (_ for _ in ()).throw(RuntimeError("gmail down")))
    assert gmail_read.my_address() == ""

    monkeypatch.setattr(gmail_read, "_service", lambda: gmail)
    assert gmail_read.my_address() == "craig@example.com"


# --------------------------------------------------------------------------- #
# Following more than one label. Wren/Watch means "tell me" and Wren/Do means
# "handle it", they live on different threads, and the caller has to be able to
# tell which arrived without a second read of the same thread.
# --------------------------------------------------------------------------- #

def test_list_history_follows_several_labels_at_once(gmail):
    gmail.history_pages = [{
        "history": [{"messagesAdded": [
            {"message": {"id": "watched", "threadId": "t1"}},
            {"message": {"id": "acted", "threadId": "t2"}},
        ]}],
        "historyId": "500",
    }]
    gmail.thread_data = {"t1": _watched(["Label_7"]), "t2": _watched(["Label_9"])}

    result = gmail_read.list_history("100", ["Label_7", "Label_9"])

    assert result["message_ids"] == ["watched", "acted"]
    assert result["threads"] == {"t1": {"labels": ["Label_7"], "newest": "tm0"},
                                 "t2": {"labels": ["Label_9"], "newest": "tm0"}}


def test_list_history_reports_a_thread_carrying_both_labels(gmail):
    """The caller decides what both-at-once means (act wins). This only has to
    hand it the facts rather than picking one."""
    gmail.history_pages = [{
        "history": [{"messagesAdded": [{"message": {"id": "m1", "threadId": "t1"}}]}],
        "historyId": "500",
    }]
    gmail.thread_data = {"t1": _watched(["Label_7", "Label_9"])}

    result = gmail_read.list_history("100", ["Label_7", "Label_9"])

    assert result["threads"]["t1"]["labels"] == ["Label_7", "Label_9"]


def test_list_history_names_no_thread_it_filtered_out(gmail):
    """A caller reading thread_labels must never find a thread whose messages it
    was not given — that is how a dropped message becomes an acted-on one."""
    gmail.history_pages = [{
        "history": [{"messagesAdded": [
            {"message": {"id": "kept", "threadId": "t1"}},
            {"message": {"id": "dropped", "threadId": "t2"}},
        ]}],
        "historyId": "500",
    }]
    gmail.thread_data = {"t1": _watched(["Label_7"]), "t2": _watched(["INBOX"])}

    result = gmail_read.list_history("100", ["Label_7", "Label_9"])

    assert result["message_ids"] == ["kept"]
    assert set(result["threads"]) == {"t1"}
    assert result["message_threads"] == {"kept": "t1"}


def test_list_history_still_takes_a_single_label_id(gmail):
    """The watch-only caller passes one id, and did so before Wren/Do existed."""
    gmail.history_pages = [{
        "history": [{"messagesAdded": [{"message": {"id": "m1", "threadId": "t1"}}]}],
        "historyId": "500",
    }]
    gmail.thread_data = {"t1": _watched(["Label_7"])}

    result = gmail_read.list_history("100", "Label_7")

    assert result["message_ids"] == ["m1"]
    assert result["threads"] == {"t1": {"labels": ["Label_7"], "newest": "tm0"}}


def test_thread_state_returns_nothing_for_a_thread_it_cannot_read(gmail):
    """Unreadable means no labels and no message to act on, so the mail is
    neither reported nor acted on, and the log says why."""
    class _Failing:
        def get(self, **kwargs):
            raise RuntimeError("gmail 500")

    gmail.threads = lambda: _Failing()
    logger = _Recorder()

    assert gmail_read._thread_state("t1", ["Label_7"], logger) == {
        "labels": set(), "newest": None}
    assert len(logger.warnings) == 1
    assert "NOT acted on" in logger.warnings[0]


def test_thread_state_reads_no_thread_when_there_is_nothing_to_look_for(gmail):
    """Watch-only with no act label configured must not pay a threads.get to
    look for a label that does not exist."""
    class _Exploding:
        def get(self, **kwargs):
            raise AssertionError("threads.get reached")

    gmail.threads = lambda: _Exploding()

    assert gmail_read._thread_state("t1", []) == {"labels": set(), "newest": None}
    assert gmail_read._thread_state("t1", [None]) == {"labels": set(), "newest": None}


# --------------------------------------------------------------------------- #
# Labelling mail that already arrived
#
# The live miss. Wren/Do deliberately has no Gmail filter, so dragging the label
# onto an email BY HAND is the only way it is ever used — and Gmail records that
# as labelsAdded, a different history type from messagesAdded. Asking for
# messageAdded alone made the whole feature a no-op that logged "nothing new".
# --------------------------------------------------------------------------- #

def test_hand_labelling_an_old_email_is_seen(gmail):
    """No new mail arrived at all: he labelled something already in the mailbox."""
    gmail.history_pages = [{
        "history": [{"labelsAdded": [
            {"labelIds": ["Label_9"],
             "message": {"id": "old", "threadId": "t1"}},
        ]}],
        "historyId": "500",
    }]
    gmail.thread_data = {"t1": _watched(["Label_9"])}

    result = gmail_read.list_history("100", ["Label_7", "Label_9"])

    assert result["threads"] == {"t1": {"labels": ["Label_9"], "newest": "tm0"}}
    # Nothing ARRIVED, so there is nothing to alert about. The thread entry is
    # the whole answer.
    assert result["message_ids"] == []


def test_labelling_a_long_thread_names_it_once(gmail):
    """Gmail applies a label to every message on the thread, so this arrives as
    five labelsAdded entries. Five thread entries would be five background jobs."""
    gmail.history_pages = [{
        "history": [{"labelsAdded": [
            {"labelIds": ["Label_9"], "message": {"id": f"m{n}", "threadId": "t1"}}
            for n in range(5)
        ]}],
        "historyId": "500",
    }]
    gmail.thread_data = {"t1": _watched(*[["Label_9"]] * 5)}

    result = gmail_read.list_history("100", ["Label_9"])

    assert list(result["threads"]) == ["t1"]
    # And it names the LATEST message, not whichever one Gmail listed first.
    assert result["threads"]["t1"]["newest"] == "tm4"


def test_an_everyday_label_change_costs_no_thread_read(gmail):
    """Reading, starring and archiving are labelsAdded too. Taking them
    seriously would mean a threads.get for every mailbox fidget."""
    class _Exploding:
        def get(self, **kwargs):
            raise AssertionError("threads.get reached")

    gmail.history_pages = [{
        "history": [{"labelsAdded": [
            {"labelIds": ["STARRED"], "message": {"id": "m1", "threadId": "t1"}},
        ]}],
        "historyId": "500",
    }]
    gmail.threads = lambda: _Exploding()

    result = gmail_read.list_history("100", ["Label_9"])

    assert result["threads"] == {} and result["message_ids"] == []


def test_the_newest_message_is_never_a_half_typed_draft(gmail):
    """Gmail autosaves a reply as a real message on the thread before it is
    sent. Handing Wren his own unfinished sentence is worse than useless."""
    gmail.history_pages = [{
        "history": [{"labelsAdded": [
            {"labelIds": ["Label_9"], "message": {"id": "m1", "threadId": "t1"}},
        ]}],
        "historyId": "500",
    }]
    gmail.thread_data = {"t1": _watched(["Label_9", "INBOX"],
                                        ["Label_9", "DRAFT"])}

    result = gmail_read.list_history("100", ["Label_9"])

    assert result["threads"]["t1"]["newest"] == "tm0"


# --------------------------------------------------------------------------- #
# The dead-connection retry (mail_watcher only)
# --------------------------------------------------------------------------- #

def test_a_broken_pipe_reconnects_and_the_retry_succeeds(gmail, monkeypatch):
    """"[Errno 32] Broken pipe", three times in two days on the mail_watcher
    daemon. Google closes an idle connection, and the cached Gmail client is
    still holding it — so the first call after every quiet gap dies.

    Assert the whole guarantee, not just "it didn't raise": a new client was
    asked for, the call was retried, the real history came back, and the
    recovery was logged rather than swallowed.
    """
    gmail.history_errors = [BrokenPipeError(32, "Broken pipe")]
    gmail.history_pages = [{
        "history": [{"messagesAdded": [{"message": {"id": "m1", "threadId": "t1"}}]}],
        "historyId": "500",
    }]
    reconnects = []
    monkeypatch.setattr(gmail_read, "_reconnect",
                        lambda: (reconnects.append(1), gmail)[1])
    logger = _Recorder()

    result = gmail_read.list_history("100", logger=logger)

    assert reconnects == [1]
    assert gmail.history_calls == 2
    assert result["message_ids"] == ["m1"]
    assert len(logger.warnings) == 1
    assert "Broken pipe" in logger.warnings[0]


def test_a_second_broken_pipe_is_an_error_not_another_retry(gmail, monkeypatch):
    """One retry only. A fresh connection that also dies is a real outage, and
    retrying forever would hang the watcher's callback instead of reporting it.
    """
    gmail.history_errors = [BrokenPipeError(32, "Broken pipe"),
                            BrokenPipeError(32, "Broken pipe")]
    monkeypatch.setattr(gmail_read, "_reconnect", lambda: gmail)

    result = gmail_read.list_history("100")

    assert "error" in result
    assert "Broken pipe" in result["error"]
    assert gmail.history_calls == 2


def test_a_broken_pipe_does_not_move_the_watermark(gmail, monkeypatch):
    """The caller acks a raise, so a failure that reported a history id would
    lose that mail for good. A give-up returns an error and nothing else —
    no history_id for mail_watcher to commit."""
    gmail.history_errors = [BrokenPipeError(32, "Broken pipe"),
                            BrokenPipeError(32, "Broken pipe")]
    monkeypatch.setattr(gmail_read, "_reconnect", lambda: gmail)

    result = gmail_read.list_history("100")

    assert "history_id" not in result
