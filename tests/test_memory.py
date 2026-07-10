"""Tests for agent.tools.memory — the persistent fact store.

Each test points the store at a fresh tmp file via monkeypatch so nothing
touches the real config/wren_memory.json.
"""

import threading
import time

import pytest

from agent.tools import memory


@pytest.fixture(autouse=True)
def tmp_store(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "_STORE_PATH", tmp_path / "wren_memory.json")


# --------------------------------------------------------------------------- #
# remember / recall round-trip
# --------------------------------------------------------------------------- #

def test_remember_then_recall_round_trip():
    saved = memory.remember("Craig prefers metric units", category="preference")
    assert "id" in saved and saved["text"] == "Craig prefers metric units"

    got = memory.recall()
    assert got["count"] == 1
    m = got["memories"][0]
    assert m["text"] == "Craig prefers metric units"
    assert m["category"] == "preference"
    assert m["id"] == saved["id"]
    assert m["created"]  # a timestamp was stamped in


def test_remember_dedupes_identical_text_case_insensitively():
    first = memory.remember("Craig prefers metric units")
    dup = memory.remember("  craig PREFERS metric units ")

    assert dup["id"] == first["id"]
    assert dup.get("already_known") is True
    assert memory.recall()["count"] == 1


def test_remember_rejects_empty_text():
    assert "error" in memory.remember("   ")
    assert memory.recall()["count"] == 0


def test_recall_filters_by_query_on_text_and_category():
    memory.remember("Craig prefers metric units", category="preference")
    memory.remember("Craig's sister's birthday is March 3", category="person")

    assert memory.recall(query="metric")["count"] == 1
    assert memory.recall(query="person")["count"] == 1  # matches category
    assert memory.recall(query="nonesuch")["count"] == 0


# --------------------------------------------------------------------------- #
# forget
# --------------------------------------------------------------------------- #

def test_forget_removes_by_id():
    saved = memory.remember("Craig prefers metric units")
    result = memory.forget(saved["id"])

    assert result["removed"] is True
    assert memory.recall()["count"] == 0


def test_forget_unknown_id_reports_error():
    memory.remember("Craig prefers metric units")
    result = memory.forget("deadbeef")

    assert result["removed"] is False
    assert "error" in result
    assert memory.recall()["count"] == 1  # nothing removed


# --------------------------------------------------------------------------- #
# missing file / render
# --------------------------------------------------------------------------- #

def test_recall_on_missing_file_returns_empty():
    assert memory.recall() == {"count": 0, "memories": []}


def test_render_memory_block_empty_when_no_memories():
    assert memory.render_memory_block() == ""


def test_render_memory_block_lists_only_active_facts():
    memory.pin("Craig prefers metric units")
    memory.pin("Craig's sister's birthday is March 3")
    memory.remember("Crows can recognize human faces")  # archival — not rendered

    block = memory.render_memory_block()
    assert "reference facts" in block
    assert "- Craig prefers metric units" in block
    assert "- Craig's sister's birthday is March 3" in block
    assert "Crows" not in block


# --------------------------------------------------------------------------- #
# scope: remember (archival) vs pin (active)
# --------------------------------------------------------------------------- #

def test_remember_defaults_to_archival_with_zero_access_count():
    memory.remember("Crows can recognize human faces", category="trivia")
    m = memory.recall()["memories"][0]
    assert m["scope"] == "archival"
    assert m["access_count"] == 0


def test_pin_saves_as_active():
    memory.pin("Craig prefers metric units", category="preference")
    m = memory.recall()["memories"][0]
    assert m["scope"] == "active"


def test_recall_returns_both_active_and_archival():
    memory.pin("Craig prefers metric units")
    memory.remember("Crows can recognize human faces")
    assert memory.recall()["count"] == 2


def test_pin_promotes_existing_archival_fact():
    saved = memory.remember("Craig prefers metric units")
    promoted = memory.pin("craig prefers metric units")  # same text, different case

    assert promoted["id"] == saved["id"]
    assert promoted.get("promoted") is True
    got = memory.recall()
    assert got["count"] == 1  # no duplicate
    assert got["memories"][0]["scope"] == "active"


# --------------------------------------------------------------------------- #
# archive (demote active -> archival)
# --------------------------------------------------------------------------- #

def test_archive_demotes_active_fact():
    saved = memory.pin("Craig prefers metric units")
    result = memory.archive(saved["id"])

    assert result["archived"] is True
    assert memory.recall()["memories"][0]["scope"] == "archival"
    assert memory.render_memory_block() == ""  # no longer injected


def test_archive_unknown_id_reports_error():
    memory.pin("Craig prefers metric units")
    result = memory.archive("deadbeef")

    assert result["archived"] is False
    assert "error" in result


# --------------------------------------------------------------------------- #
# access_count
# --------------------------------------------------------------------------- #

def test_targeted_recall_increments_archival_access_count():
    memory.remember("Crows can recognize human faces", category="trivia")

    memory.recall(query="crows")
    memory.recall(query="crows")
    m = memory.recall(query="crows")["memories"][0]
    assert m["access_count"] == 3


def test_listing_recall_does_not_increment_access_count():
    memory.remember("Crows can recognize human faces")

    memory.recall()  # no query — browsing, not retrieval
    assert memory.recall()["memories"][0]["access_count"] == 0


def test_active_facts_are_not_access_counted():
    memory.pin("Craig prefers metric units")
    memory.recall(query="metric")
    assert memory.recall()["memories"][0]["access_count"] == 0


# --------------------------------------------------------------------------- #
# category filter
# --------------------------------------------------------------------------- #

def test_recall_filters_by_category():
    memory.remember("Crows can recognize human faces", category="trivia")
    memory.pin("Craig prefers metric units", category="preference")

    trivia = memory.recall(category="trivia")
    assert trivia["count"] == 1
    assert trivia["memories"][0]["text"] == "Crows can recognize human faces"


def test_recall_intersects_query_and_category():
    memory.remember("Crows can recognize human faces", category="trivia")
    memory.remember("Owls can rotate their heads 270 degrees", category="trivia")

    got = memory.recall(query="crows", category="trivia")
    assert got["count"] == 1
    assert memory.recall(query="crows", category="preference")["count"] == 0


# --------------------------------------------------------------------------- #
# migration: legacy entries without scope / access_count
# --------------------------------------------------------------------------- #

def test_legacy_entry_without_scope_is_treated_as_active():
    # Simulate a pre-upgrade file: entries have neither scope nor access_count.
    memory._save({"memories": [
        {"id": "legacy01", "text": "Craig does yoga on Mondays", "category": "schedule",
         "created": "2026-07-08T16:56:28"},
    ]})

    assert "- Craig does yoga on Mondays" in memory.render_memory_block()
    # recall with a query must not crash on the missing access_count.
    got = memory.recall(query="yoga")
    assert got["count"] == 1


# --------------------------------------------------------------------------- #
# corrupt store: degrades to empty instead of crashing every conversation
# (render_memory_block() seeds each system prompt via with_identity())
# --------------------------------------------------------------------------- #

def test_corrupt_store_degrades_to_empty_and_recovers():
    memory._STORE_PATH.write_text('{"memories": [truncated garbage')

    assert memory.render_memory_block() == ""
    assert memory.recall()["count"] == 0
    # The damaged file was quarantined, so a fresh save works.
    assert "id" in memory.remember("Craig prefers metric units")
    assert memory.recall()["count"] == 1


# --------------------------------------------------------------------------- #
# concurrency: atomic writes + lock (Flask runs threaded=True)
# --------------------------------------------------------------------------- #

def test_save_leaves_no_temp_files():
    memory.remember("Craig prefers metric units", category="preference")
    memory.remember("Crows can recognize human faces", category="trivia")
    memory.recall(query="crows")  # a write on the read path too

    leftovers = list(memory._STORE_PATH.parent.glob("*.tmp"))
    assert leftovers == []


def test_concurrent_remembers_do_not_lose_writes(monkeypatch):
    # Widen the read-modify-write window so an unlocked store would reliably
    # clobber: sleep briefly after each _load(). With _LOCK held across
    # load->save, every write must still land.
    real_load = memory._load

    def slow_load():
        data = real_load()
        time.sleep(0.005)
        return data

    monkeypatch.setattr(memory, "_load", slow_load)

    n = 20
    threads = [
        threading.Thread(target=memory.remember, args=(f"fact number {i}",))
        for i in range(n)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert memory.recall()["count"] == n
