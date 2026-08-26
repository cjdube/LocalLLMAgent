"""Tests for agent.tools.memory — the persistent fact store.

Each test points the store at a fresh tmp file via monkeypatch so nothing
touches the real config/wren_memory.json.
"""

import logging

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
    saved = memory.remember("I prefer metric units", category="preference")
    assert "id" in saved and saved["text"] == "I prefer metric units"

    got = memory.recall()
    assert got["count"] == 1
    m = got["memories"][0]
    assert m["text"] == "I prefer metric units"
    assert m["category"] == "preference"
    assert m["id"] == saved["id"]
    assert m["created"]  # a timestamp was stamped in


def test_remember_dedupes_identical_text_case_insensitively():
    first = memory.remember("I prefer metric units")
    dup = memory.remember("  i PREFER metric units ")

    assert dup["id"] == first["id"]
    assert dup.get("already_known") is True
    assert memory.recall()["count"] == 1


def test_remember_rejects_empty_text():
    assert "error" in memory.remember("   ")
    assert memory.recall()["count"] == 0


def test_recall_filters_by_query_on_text_and_category():
    memory.remember("I prefer metric units", category="preference")
    memory.remember("My sister's birthday is March 3", category="person")

    assert memory.recall(query="metric")["count"] == 1
    assert memory.recall(query="person")["count"] == 1  # matches category
    assert memory.recall(query="nonesuch")["count"] == 0


# --------------------------------------------------------------------------- #
# forget
# --------------------------------------------------------------------------- #

def test_forget_removes_by_id():
    saved = memory.remember("I prefer metric units")
    result = memory.forget(saved["id"])

    assert result["removed"] is True
    assert memory.recall()["count"] == 0


def test_forget_unknown_id_reports_error():
    memory.remember("I prefer metric units")
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
    memory.pin("I prefer metric units")
    memory.pin("My sister's birthday is March 3")
    memory.remember("Crows can recognize human faces")  # archival — not rendered

    block = memory.render_memory_block()
    assert "reference facts" in block
    assert "- I prefer metric units" in block
    assert "- My sister's birthday is March 3" in block
    assert "Crows" not in block


# --------------------------------------------------------------------------- #
# The always-on block is bounded, and says so when it drops a fact.
#
# render_memory_block was the one prompt-injected index with no cap, which made
# chat/server.py's startup budget check unprovable — you cannot price a prompt
# head that has an unbounded term in it. Caps are sized well above the live
# store (13 memories, 3 active, 203 chars on 2026-08-26), so in practice these
# never fire; the warning is the point, because a pinned fact silently missing
# from the prompt looks exactly like Wren ignoring an instruction.
# --------------------------------------------------------------------------- #

def test_shipped_caps_are_above_real_usage():
    # A cap set at or below actual usage silently drops a fact on day one. The
    # live store on 2026-08-26 was 3 active facts / 203 chars.
    assert memory.MAX_ACTIVE_MEMORIES >= 20
    assert memory.MAX_MEMORY_BLOCK_CHARS >= 1500


def test_block_is_capped_by_count_and_warns(caplog):
    for i in range(memory.MAX_ACTIVE_MEMORIES + 5):
        memory.pin(f"fact number {i}")
    log = logging.getLogger("cap-count")
    with caplog.at_level(logging.WARNING, logger="cap-count"):
        block = memory.render_memory_block(log)
    assert block.count("\n- ") == memory.MAX_ACTIVE_MEMORIES
    # Both halves: it truncated AND it said so, with the counts CLAUDE.md asks
    # for. A cap that drops quietly is the bug, not the fix.
    assert any("truncated" in r.message for r in caplog.records)
    assert any(f"of {memory.MAX_ACTIVE_MEMORIES + 5} active" in r.message
               for r in caplog.records)


def test_block_is_capped_by_chars_and_warns(caplog):
    # Few enough facts to clear the count cap, long enough to blow the budget —
    # so only the char term can be what truncates here.
    long_fact = "x" * (memory.MAX_MEMORY_BLOCK_CHARS // 3)
    for i in range(5):
        memory.pin(f"{i} {long_fact}")
    log = logging.getLogger("cap-chars")
    with caplog.at_level(logging.WARNING, logger="cap-chars"):
        block = memory.render_memory_block(log)
    assert len(block) < memory.MAX_MEMORY_BLOCK_CHARS + 200
    assert any(str(memory.MAX_MEMORY_BLOCK_CHARS) in r.message for r in caplog.records)


def test_uncapped_block_does_not_warn(caplog):
    # The ordinary case, which is every real day: nothing dropped, nothing said.
    memory.pin("I prefer metric units")
    log = logging.getLogger("cap-quiet")
    with caplog.at_level(logging.WARNING, logger="cap-quiet"):
        assert "- I prefer metric units" in memory.render_memory_block(log)
    assert caplog.records == []


def test_truncation_without_a_logger_still_truncates():
    # The bound is not conditional on someone passing a logger.
    for i in range(memory.MAX_ACTIVE_MEMORIES + 5):
        memory.pin(f"fact number {i}")
    assert memory.render_memory_block().count("\n- ") == memory.MAX_ACTIVE_MEMORIES


# --------------------------------------------------------------------------- #
# scope: remember (archival) vs pin (active)
# --------------------------------------------------------------------------- #

def test_remember_defaults_to_archival_with_zero_access_count():
    memory.remember("Crows can recognize human faces", category="trivia")
    m = memory.recall()["memories"][0]
    assert m["scope"] == "archival"
    assert m["access_count"] == 0


def test_pin_saves_as_active():
    memory.pin("I prefer metric units", category="preference")
    m = memory.recall()["memories"][0]
    assert m["scope"] == "active"


def test_recall_normalizes_missing_scope_to_active():
    # A legacy record saved before scope existed has no scope field. recall()
    # must hand it back with an explicit scope so callers (notably the small
    # chat model asked to list memories) don't have to guess — matching how
    # render_memory_block / the /memories page already default it.
    memory._save({"memories": [{"id": "legacy1", "text": "an old fact"}]})
    m = memory.recall()["memories"][0]
    assert m["scope"] == "active"


def test_recall_returns_both_active_and_archival():
    memory.pin("I prefer metric units")
    memory.remember("Crows can recognize human faces")
    assert memory.recall()["count"] == 2


def test_pin_promotes_existing_archival_fact():
    saved = memory.remember("I prefer metric units")
    promoted = memory.pin("i prefer metric units")  # same text, different case

    assert promoted["id"] == saved["id"]
    assert promoted.get("promoted") is True
    got = memory.recall()
    assert got["count"] == 1  # no duplicate
    assert got["memories"][0]["scope"] == "active"


# --------------------------------------------------------------------------- #
# archive (demote active -> archival)
# --------------------------------------------------------------------------- #

def test_archive_demotes_active_fact():
    saved = memory.pin("I prefer metric units")
    result = memory.archive(saved["id"])

    assert result["archived"] is True
    assert memory.recall()["memories"][0]["scope"] == "archival"
    assert memory.render_memory_block() == ""  # no longer injected


def test_archive_unknown_id_reports_error():
    memory.pin("I prefer metric units")
    result = memory.archive("deadbeef")

    assert result["archived"] is False
    assert "error" in result


# --------------------------------------------------------------------------- #
# recategorize (relabel in place, preserving id / created / access_count)
# --------------------------------------------------------------------------- #

def test_recategorize_changes_tag_without_losing_history():
    saved = memory.remember("Crows can recognize human faces", category="trivia")
    memory.recall(query="crows")  # bump access_count to 1
    created = memory.recall()["memories"][0]["created"]

    result = memory.recategorize(saved["id"], "person")
    assert result == {"recategorized": True, "id": saved["id"],
                      "from": "trivia", "to": "person"}

    m = memory.recall(category="person")["memories"][0]
    assert m["id"] == saved["id"]          # same fact, not a new one
    assert m["category"] == "person"
    assert m["created"] == created         # timestamp kept
    assert m["access_count"] == 1          # preserved from the earlier recall


def test_recategorize_preserves_active_scope():
    saved = memory.pin("I prefer metric units", category="preference")
    memory.recategorize(saved["id"], "other")
    assert memory.recall()["memories"][0]["scope"] == "active"


def test_recategorize_empty_category_clears_tag():
    saved = memory.remember("Crows can recognize human faces", category="trivia")
    result = memory.recategorize(saved["id"], "  ")
    assert result["to"] is None
    assert memory.recall()["memories"][0]["category"] is None


def test_recategorize_unknown_id_reports_error():
    memory.remember("I prefer metric units", category="preference")
    result = memory.recategorize("deadbeef", "other")

    assert result["recategorized"] is False
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
    memory.pin("I prefer metric units")
    memory.recall(query="metric")
    assert memory.recall()["memories"][0]["access_count"] == 0


# --------------------------------------------------------------------------- #
# category filter
# --------------------------------------------------------------------------- #

def test_recall_filters_by_category():
    memory.remember("Crows can recognize human faces", category="trivia")
    memory.pin("I prefer metric units", category="preference")

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
        {"id": "legacy01", "text": "I do yoga on Mondays", "category": "schedule",
         "created": "2026-07-08T16:56:28"},
    ]})

    assert "- I do yoga on Mondays" in memory.render_memory_block()
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
    assert "id" in memory.remember("I prefer metric units")
    assert memory.recall()["count"] == 1


# --------------------------------------------------------------------------- #
# concurrency: atomic writes + lock (Flask runs threaded=True)
# --------------------------------------------------------------------------- #

def test_save_leaves_no_temp_files():
    memory.remember("I prefer metric units", category="preference")
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
