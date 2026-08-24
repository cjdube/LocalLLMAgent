"""Tests for the Gmail watcher's state store.

The store path is redirected to tmp by the suite-wide fixture in conftest, and
again here per-test — the convention this repo keeps, with the conftest entry as
the backstop.
"""

from datetime import datetime, timedelta, timezone

import pytest

from agent.tools import mail_state


@pytest.fixture(autouse=True)
def _isolate_store(tmp_path, monkeypatch):
    monkeypatch.setattr(mail_state, "_STORE_PATH", tmp_path / "mail_state.json")


def test_empty_store_reads_as_defaults():
    assert mail_state.history_id() is None
    assert mail_state.watch_expires_in_hours() is None
    assert mail_state.unseen(["a", "b"]) == ["a", "b"]


def test_commit_marks_messages_seen():
    mail_state.commit(seen_ids=["m1", "m2"], new_history_id="100")

    assert mail_state.unseen(["m1", "m2", "m3"]) == ["m3"]
    assert mail_state.history_id() == "100"


def test_duplicate_delivery_is_a_no_op():
    """Pub/Sub is at-least-once, so the same notification arrives again. The
    second pass must find nothing new — otherwise one email pushes twice."""
    mail_state.commit(seen_ids=["m1"], new_history_id="100")
    mail_state.commit(seen_ids=["m1"], new_history_id="100")

    assert mail_state.unseen(["m1"]) == []
    assert mail_state.load_state()["seen"].count("m1") == 1


def test_unseen_dedupes_within_one_batch():
    # Gmail names the same message in several history entries, so a single
    # history.list response can carry it twice.
    assert mail_state.unseen(["m1", "m1", "m2"]) == ["m1", "m2"]


def test_watermark_never_moves_backward():
    """Pub/Sub does not guarantee order. An older historyId arriving late must
    not walk the watermark back and re-report handled mail."""
    mail_state.commit(new_history_id="500")
    mail_state.commit(new_history_id="200")

    assert mail_state.history_id() == "500"


def test_watermark_compares_numerically_not_as_strings():
    # A string compare gets "9" > "10" wrong, and Gmail returns these as strings.
    mail_state.commit(new_history_id="9")
    mail_state.commit(new_history_id="10")

    assert mail_state.history_id() == "10"


def test_unparseable_incoming_history_id_leaves_the_watermark_alone():
    mail_state.commit(new_history_id="100")
    mail_state.commit(new_history_id="not-a-number")

    assert mail_state.history_id() == "100"


def test_seen_list_is_pruned_on_write(monkeypatch):
    monkeypatch.setattr(mail_state, "MAX_SEEN", 5)
    mail_state.commit(seen_ids=[f"m{i}" for i in range(20)])

    seen = mail_state.load_state()["seen"]
    assert len(seen) == 5
    # The newest survive — an id older than Gmail's ~week of history can never
    # come back through list_history anyway.
    assert seen == ["m15", "m16", "m17", "m18", "m19"]


def test_record_watch_stores_expiry_and_seeds_the_watermark():
    mail_state.record_watch("1756000000000", "42")

    state = mail_state.load_state()
    assert state["watch_expiration"] == "1756000000000"
    assert state["history_id"] == "42"


def test_watch_expiry_is_read_as_epoch_milliseconds():
    """Gmail reports expiration in MILLISECONDS. Read as seconds it would land
    in the year 57000 and the near-expiry alert would never fire."""
    in_three_days = datetime.now(timezone.utc) + timedelta(days=3)
    mail_state.record_watch(str(int(in_three_days.timestamp() * 1000)))

    hours = mail_state.watch_expires_in_hours()
    assert 71 < hours < 73


def test_lapsed_watch_reports_negative_hours():
    """The silent failure this store exists to make loud."""
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    mail_state.record_watch(str(int(yesterday.timestamp() * 1000)))

    assert mail_state.watch_expires_in_hours() < 0


def test_garbage_expiry_reads_as_absent_rather_than_raising():
    mail_state.record_watch("whenever")

    assert mail_state.watch_expires_in_hours() is None


def test_commit_preserves_the_watch_expiry():
    """The watcher and the renewal job write the same file. A commit from one
    must not drop the other's field."""
    mail_state.record_watch("1756000000000", "10")
    mail_state.commit(seen_ids=["m1"], new_history_id="20")

    state = mail_state.load_state()
    assert state["watch_expiration"] == "1756000000000"
    assert state["history_id"] == "20"
    assert state["seen"] == ["m1"]
