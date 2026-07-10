"""Tests for agent/store.py — the shared JSON-store helpers the memory,
reminders, background-jobs, and starred-repo stores are built on. Everything
runs against tmp_path; the cross-process lock test probes the flock from a
real child process with LOCK_NB so it's deterministic (no sleeps or timing)."""

import fcntl
import json
import os
import subprocess
import sys
import threading

import pytest

from agent.store import atomic_write_json, load_json, locked


# --------------------------------------------------------------------------- #
# load_json
# --------------------------------------------------------------------------- #

def test_missing_file_returns_default(tmp_path):
    assert load_json(tmp_path / "nope.json", {"jobs": []}) == {"jobs": []}


def test_round_trip(tmp_path):
    path = tmp_path / "store.json"
    atomic_write_json(path, {"reminders": [{"id": "ab12"}]})
    assert load_json(path, {}) == {"reminders": [{"id": "ab12"}]}


def test_corrupt_file_is_quarantined_and_default_returned(tmp_path, caplog):
    path = tmp_path / "store.json"
    path.write_text('{"memories": [truncated')

    with caplog.at_level("ERROR"):
        assert load_json(path, {"memories": []}) == {"memories": []}

    assert "unreadable" in caplog.text
    # The damaged file is moved aside for inspection, not deleted...
    quarantined = list(tmp_path.glob("store.json.corrupt-*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text() == '{"memories": [truncated'
    # ...and the store path is clear, so the next write/read cycle works.
    assert not path.exists()
    atomic_write_json(path, {"memories": [{"id": "x"}]})
    assert load_json(path, {})["memories"] == [{"id": "x"}]


# --------------------------------------------------------------------------- #
# atomic_write_json
# --------------------------------------------------------------------------- #

def test_failed_write_keeps_original_and_leaves_no_temp(tmp_path):
    path = tmp_path / "store.json"
    atomic_write_json(path, {"ok": True})

    with pytest.raises(TypeError):
        atomic_write_json(path, {"bad": object()})  # not JSON-serializable

    assert load_json(path, {}) == {"ok": True}
    assert list(tmp_path.glob("*.tmp")) == []


# --------------------------------------------------------------------------- #
# locked — must exclude other processes (the whole point) and other threads
# --------------------------------------------------------------------------- #

_PROBE = """
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
try:
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    sys.exit(42)
sys.exit(0)
"""


def _probe_lock_from_child(store_path) -> int:
    """Try to take the store's flock from a separate process, non-blocking.
    Returns 42 if the lock was held (child excluded), 0 if it was free."""
    return subprocess.run(
        [sys.executable, "-c", _PROBE, f"{store_path}.lock"], timeout=30
    ).returncode


def test_lock_excludes_other_processes_and_releases(tmp_path):
    store = tmp_path / "store.json"
    with locked(store):
        assert _probe_lock_from_child(store) == 42
    assert _probe_lock_from_child(store) == 0


def test_lock_serializes_read_modify_write_across_threads(tmp_path):
    # Each locked() call opens its own fd, so flock excludes sibling threads
    # too — no separate threading.Lock needed. An unlocked version of this
    # reliably loses updates (the load->save window is widened by the barrier
    # of many threads starting together).
    store = tmp_path / "store.json"
    n = 20

    def add(i):
        with locked(store):
            data = load_json(store, {"items": []})
            data["items"].append(i)
            atomic_write_json(store, data)

    threads = [threading.Thread(target=add, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(load_json(store, {})["items"]) == list(range(n))


def test_lock_file_is_a_sidecar_not_the_store(tmp_path):
    # os.replace() in atomic_write_json swaps the store's inode; a lock on the
    # store file itself would silently stop excluding anyone after a write.
    store = tmp_path / "store.json"
    with locked(store):
        atomic_write_json(store, {"v": 1})
        assert (tmp_path / "store.json.lock").exists()
        assert _probe_lock_from_child(store) == 42
