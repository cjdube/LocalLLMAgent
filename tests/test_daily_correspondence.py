"""Tests for scribejay/daily_correspondence.py — that main() reads sent metadata
and persists one Correspondence page per day. Every collaborator is stubbed
("monkeypatched" — swapped for a fake for the length of one test); nothing here
touches Gmail, the vault, or the phone."""

import sys

import pytest

from chat.insights import _LINE_RE, _is_run_start, _is_run_success
from scribejay import daily_correspondence as dc

ME = "craigdubenh@gmail.com"


def _messages(capsys) -> list:
    """The message halves of the captured log lines, split the way
    chat/insights.py splits them — so the boundary assertions below and the
    dashboard's own parser cannot drift apart."""
    out = []
    for line in capsys.readouterr().out.splitlines():
        m = _LINE_RE.match(line)
        if m:
            out.append(m.group(3))
    return out


@pytest.fixture
def stubbed_run(monkeypatch):
    """A happy path: one real message sent yesterday, to one real person."""
    seen = {"persists": [], "windows": []}

    def _fetch(start, end, *a, **k):
        seen["windows"].append((start, end))
        return {"count": 1, "messages": [
            {"message_id": "m1", "thread_id": "T", "to": "Kat <kat@impact.com>", "cc": "",
             "subject": "Re: Catch Up", "date": "2026-08-21 10:00", "is_reply": True}]}

    monkeypatch.setattr(dc, "fetch_sent_metadata", _fetch)
    monkeypatch.setattr(dc, "my_address", lambda: ME)
    monkeypatch.setattr(dc, "persist_or_email",
                        lambda content, prefix, day, subject, task_name, logger, directory=None:
                        seen["persists"].append((prefix, day, content, directory)) or {"written": True})
    monkeypatch.setattr(dc, "notify_failure", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", ["daily_correspondence"])
    return seen


def test_happy_path_writes_one_page(stubbed_run):
    assert dc.main() == 0
    assert len(stubbed_run["persists"]) == 1
    prefix, _day, content, _dir = stubbed_run["persists"][0]
    assert prefix == "Correspondence"
    assert "Catch Up" in content
    assert "Kat" in content


def test_the_page_is_written_outside_the_vault_ingest_queue(stubbed_run, monkeypatch):
    # Option B, asserted at the call: these pages name people, so they must not
    # land in LEARNINGS_DIR where ObsidianWikiAgent would make wiki pages of them.
    monkeypatch.setenv("CORRESPONDENCE_DIR", "/tmp/corr-target")
    assert dc.main() == 0
    assert str(stubbed_run["persists"][0][3]) == "/tmp/corr-target"


def test_a_day_of_only_self_addressed_mail_writes_nothing(stubbed_run, monkeypatch):
    # Wren's own Morning Brief is the common case for a quiet day.
    monkeypatch.setattr(dc, "fetch_sent_metadata", lambda *a, **k: {"count": 1, "messages": [
        {"message_id": "m1", "thread_id": "T", "to": ME, "cc": "",
         "subject": "Morning Brief - 2026-08-21", "date": "2026-08-21 06:00", "is_reply": False}]})
    assert dc.main() == 0
    assert stubbed_run["persists"] == []


def test_a_quiet_day_still_logs_a_finished_run(stubbed_run, monkeypatch, capsys):
    # The dashboard reads run status from the log lines, not the exit code: a run
    # that starts and never completes is reported as still running, forever.
    monkeypatch.setattr(dc, "fetch_sent_metadata", lambda *a, **k: {"count": 0, "messages": []})
    assert dc.main() == 0
    msgs = _messages(capsys)
    assert any(_is_run_start(m) for m in msgs)
    assert any(_is_run_success(m) for m in msgs)


def test_gmail_failing_is_a_warning_not_a_failed_run(stubbed_run, monkeypatch, capsys):
    # CLAUDE.md: a dead source reads as an empty day — but never silently, or a
    # missing page looks like a quiet day instead of a broken one.
    monkeypatch.setattr(dc, "fetch_sent_metadata", lambda *a, **k: {"error": "gmail 503"})
    assert dc.main() == 0
    assert stubbed_run["persists"] == []
    msgs = _messages(capsys)
    assert any("gmail 503" in m for m in msgs)
    assert any(_is_run_success(m) for m in msgs)


def test_not_knowing_his_own_address_is_a_hard_failure(stubbed_run, monkeypatch):
    # Every rule in the page is "everyone who is not him". With no address, every
    # message looks like it went to a stranger — worse than writing nothing.
    alerts = []
    monkeypatch.setattr(dc, "my_address", lambda: "")
    monkeypatch.setattr(dc, "notify_failure", lambda name, detail, logger=None: alerts.append(str(detail)))
    assert dc.main() == 1
    assert stubbed_run["persists"] == []
    assert alerts


def test_gmail_is_never_asked_before_we_know_who_he_is(stubbed_run, monkeypatch):
    monkeypatch.setattr(dc, "my_address", lambda: "")
    assert dc.main() == 1
    assert stubbed_run["windows"] == []


def test_date_writes_the_day_asked_for(stubbed_run, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["daily_correspondence", "--date", "2026-08-21"])
    assert dc.main() == 0
    _prefix, day, content, _dir = stubbed_run["persists"][0]
    assert str(day) == "2026-08-21"
    assert "August 21, 2026" in content


def test_the_day_window_covers_the_whole_local_day(stubbed_run, monkeypatch):
    # Gmail's own after:/before: take whole days in the ACCOUNT's timezone, which
    # is why the fetch takes real datetimes and turns them into epoch seconds.
    monkeypatch.setattr(sys, "argv", ["daily_correspondence", "--date", "2026-08-21"])
    dc.main()
    start, end = stubbed_run["windows"][0]
    assert (start.hour, start.minute) == (0, 0)
    assert end.hour == 23
    assert start.tzinfo is not None and end.tzinfo is not None


def test_backfill_writes_a_page_per_day(stubbed_run, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["daily_correspondence", "--backfill", "3"])
    assert dc.main() == 0
    days = [p[1] for p in stubbed_run["persists"]]
    assert len(days) == 3
    assert days == sorted(days)  # oldest first, so the log reads chronologically


def test_backfill_is_one_run_in_the_dashboard_not_three(stubbed_run, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["daily_correspondence", "--backfill", "3"])
    dc.main()
    msgs = _messages(capsys)
    assert sum(1 for m in msgs if _is_run_start(m)) == 1
    assert sum(1 for m in msgs if _is_run_success(m)) == 1


def test_one_bad_day_does_not_stop_the_backfill(stubbed_run, monkeypatch):
    calls = {"n": 0}

    def _fetch(start, end, *a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            return {"error": "gmail 503"}
        return {"count": 1, "messages": [
            {"message_id": "m", "thread_id": f"T{calls['n']}", "to": "kat@impact.com", "cc": "",
             "subject": "hi", "date": "2026-08-21 10:00", "is_reply": False}]}

    monkeypatch.setattr(dc, "fetch_sent_metadata", _fetch)
    monkeypatch.setattr(sys, "argv", ["daily_correspondence", "--backfill", "3"])
    assert dc.main() == 0
    assert len(stubbed_run["persists"]) == 2


def test_a_write_failure_is_a_failed_run(stubbed_run, monkeypatch):
    alerts = []
    monkeypatch.setattr(dc, "persist_or_email",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("vault gone")))
    monkeypatch.setattr(dc, "notify_failure", lambda name, detail, logger=None: alerts.append(str(detail)))
    assert dc.main() == 1
    assert any("vault gone" in a for a in alerts)
