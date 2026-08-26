"""Tests for scribejay/correspondence.py — the noise filter, the thread grouping
and the page rendering. All three are pure functions over metadata rows; nothing
here reaches Gmail.

The fixtures are built from real captured headers (a fortnight of the user's own
SENT mail, August 2026), because every one of these rules exists to handle
something that mailbox actually did: Wren's own Morning Briefs, a customer.io
unsubscribe, and one person arriving both named and bare on the same thread."""

import pytest

from scribejay import correspondence as co

ME = "craigdubenh@gmail.com"


def _row(to, subject, cc="", is_reply=False, thread="t1", date="2026-08-21 10:00"):
    return {"message_id": f"m{subject}", "thread_id": thread, "to": to, "cc": cc,
            "subject": subject, "date": date, "is_reply": is_reply}


class _Recorder:
    def __init__(self):
        self.infos = []

    def info(self, msg):
        self.infos.append(msg)


# --------------------------------------------------------------------------- #
# filter_noise
# --------------------------------------------------------------------------- #

def test_mail_to_himself_is_not_correspondence():
    # Wren's Morning Brief, and every other digest she sends him. 4 of 10 sent
    # messages in the sample fortnight, which is why this rule comes first.
    rows = [_row(ME, "Morning Brief - 2026-08-23"),
            _row("Diego M. Oppenheimer <diego@hq.aitinkerers.org>", "local agent chat - thanks!")]
    kept = co.filter_noise(rows, ME)
    assert [r["subject"] for r in kept] == ["local agent chat - thanks!"]


def test_a_thread_he_is_cc_d_on_still_counts():
    # Self-addressed means EVERY recipient is him. Being cc'd alongside a real
    # person is an ordinary message, and dropping it would lose the conversation.
    rows = [_row("gavin.lawton@crowd-now.com", "Re: Catch Up", cc=ME, is_reply=True)]
    assert len(co.filter_noise(rows, ME)) == 1


def test_machine_subjects_are_dropped():
    # The unsubscribe that made the "no real address" shortcut unusable: its
    # recipient IS a valid address (...@unsubscribe2.customer.io), so only the
    # subject separates it from real mail.
    rows = [_row("32.MRTVIMLSO53UYQ=@unsubscribe2.customer.io", "unsubscribe")]
    assert co.filter_noise(rows, ME) == []


def test_the_subject_match_is_exact_not_a_substring():
    # A real message about unsubscribing from something must survive.
    rows = [_row("kat@impact.com", "How do I unsubscribe from your newsletter?")]
    assert len(co.filter_noise(rows, ME)) == 1


def test_a_message_with_no_recipient_is_dropped():
    assert co.filter_noise([_row("", "(no subject)")], ME) == []


def test_the_filter_is_case_insensitive_about_his_own_address():
    assert co.filter_noise([_row("CraigDubeNH@Gmail.com", "Morning Brief")], ME) == []


def test_filtering_says_how_much_it_dropped():
    logger = _Recorder()
    co.filter_noise([_row(ME, "Morning Brief"), _row("a@b.com", "hi")], ME, logger=logger)
    assert any("filtered 1 of 2" in m for m in logger.infos), logger.infos


def test_nothing_dropped_is_silent():
    logger = _Recorder()
    co.filter_noise([_row("a@b.com", "hi")], ME, logger=logger)
    assert logger.infos == []


# --------------------------------------------------------------------------- #
# group_threads
# --------------------------------------------------------------------------- #

def test_a_conversation_is_one_row_not_four():
    rows = [_row("joe@hq.aitinkerers.org", "Re: Chat about local-first agents?",
                 cc="diego@hq.aitinkerers.org", is_reply=True, thread="T"),
            _row("howie@howie.com", "Re: Chat about local-first agents?",
                 cc="diego@hq.aitinkerers.org", is_reply=True, thread="T")]
    threads = co.group_threads(rows, ME)
    assert len(threads) == 1
    assert threads[0]["messages"] == 2


def test_one_person_named_on_one_message_and_bare_on_another_is_listed_once():
    # Real bug from the August 21 thread: Diego arrived as a display name on one
    # message and as a bare address on another, and was listed twice.
    rows = [_row("joe@hq.aitinkerers.org", "Re: Chat", cc="diego@hq.aitinkerers.org",
                 is_reply=True, thread="T"),
            _row("howie@howie.com", "Re: Chat",
                 cc='"Diego M. Oppenheimer" <diego@hq.aitinkerers.org>',
                 is_reply=True, thread="T")]
    people = list(co.group_threads(rows, ME)[0]["people"].values())
    assert people.count("Diego M. Oppenheimer") == 1
    assert "diego@hq.aitinkerers.org" not in people   # the name won, not the address


@pytest.mark.parametrize("order", ["named_first", "bare_first"])
def test_the_display_name_wins_whichever_message_carried_it(order):
    named = _row("a@b.com", "Re: X", cc='"Real Name" <c@d.com>', is_reply=True, thread="T")
    bare = _row("a@b.com", "Re: X", cc="c@d.com", is_reply=True, thread="T")
    rows = [named, bare] if order == "named_first" else [bare, named]
    assert co.group_threads(rows, ME)[0]["people"]["c@d.com"] == "Real Name"


def test_he_is_never_listed_among_the_people():
    rows = [_row("gavin.lawton@crowd-now.com", "Re: Catch Up", cc=ME, is_reply=True)]
    assert ME not in co.group_threads(rows, ME)[0]["people"]


def test_reached_out_comes_from_the_first_message_of_the_day():
    # He starts a thread and then follows up on it: the day's story is that he
    # reached out, not that he replied to himself.
    rows = [_row("a@b.com", "New idea", thread="T", is_reply=False),
            _row("a@b.com", "Re: New idea", thread="T", is_reply=True)]
    assert co.group_threads(rows, ME)[0]["reached_out"] is True


def test_a_reply_thread_is_not_reaching_out():
    rows = [_row("a@b.com", "Re: Old thread", thread="T", is_reply=True)]
    assert co.group_threads(rows, ME)[0]["reached_out"] is False


# --------------------------------------------------------------------------- #
# render_page
# --------------------------------------------------------------------------- #

from datetime import date

DAY = date(2026, 8, 21)


def test_the_page_separates_reaching_out_from_replying():
    threads = co.group_threads(
        [_row("kat@impact.com", "checking in", thread="A"),
         _row("gavin.lawton@crowd-now.com", "Re: Catch Up", is_reply=True, thread="B")], ME)
    page = co.render_page(threads, DAY)
    reached, replied = page.split("### Replied")
    assert "checking in" in reached
    assert "Catch Up" in replied


def test_the_reply_prefix_is_dropped_from_the_heading():
    # Which section it is in already says it was a reply.
    threads = co.group_threads([_row("a@b.com", "RE: Re: Catch Up", is_reply=True)], ME)
    assert "**Catch Up**" in co.render_page(threads, DAY)


def test_a_multi_message_thread_says_how_many():
    threads = co.group_threads(
        [_row("a@b.com", "Re: X", is_reply=True, thread="T"),
         _row("a@b.com", "Re: X", is_reply=True, thread="T")], ME)
    assert "(2 messages)" in co.render_page(threads, DAY)


def test_a_single_message_thread_says_nothing_about_count():
    threads = co.group_threads([_row("a@b.com", "Re: X", is_reply=True)], ME)
    assert "messages)" not in co.render_page(threads, DAY)


def test_an_empty_section_gets_the_none_marker():
    threads = co.group_threads([_row("a@b.com", "Re: X", is_reply=True)], ME)
    page = co.render_page(threads, DAY)
    assert page.split("### Replied")[0].count("**None:**") == 1


def test_the_date_is_written_by_python():
    assert co.render_page([], DAY).startswith("## Correspondence: August 21, 2026")


def test_the_page_never_carries_a_body_or_a_snippet():
    # The promise of the whole feature: metadata only. A row that somehow arrived
    # carrying a body must not render one.
    row = _row("a@b.com", "Re: X", is_reply=True)
    row["body"] = "SECRET BODY TEXT"
    row["snippet"] = "SECRET SNIPPET"
    page = co.render_page(co.group_threads([row], ME), DAY)
    assert "SECRET" not in page


# --------------------------------------------------------------------------- #
# Where it writes
# --------------------------------------------------------------------------- #

def test_the_directory_is_not_the_vault_ingest_queue(monkeypatch):
    # The whole point of option B: anything under LEARNINGS_DIR becomes an
    # asserted wiki page, and these pages name people.
    monkeypatch.delenv("CORRESPONDENCE_DIR", raising=False)
    monkeypatch.setenv("LEARNINGS_DIR", "/tmp/vault/raw")
    assert co._correspondence_dir() != __import__("pathlib").Path("/tmp/vault/raw")
    assert co._correspondence_dir().name == "correspondence"


def test_the_directory_is_configurable(monkeypatch):
    monkeypatch.setenv("CORRESPONDENCE_DIR", "/tmp/elsewhere")
    assert str(co._correspondence_dir()) == "/tmp/elsewhere"
