"""Shared JSON-file store: resilient load, atomic write, cross-process lock.

The chat server (Flask, threaded), the launchd workers (tasks/bg_worker.py,
tasks/reminder_sweep.py), and ad hoc CLI runs all mutate the same
config/*.json files from separate processes. Each store module used to guard
its read-modify-write with a threading.Lock, which serializes threads within
one process but not the processes themselves — two processes could interleave
load -> mutate -> save and silently drop the loser's update (a done background
job reverting to pending and re-running its side effects, or a fired reminder
resurrecting and re-pushing on every sweep).

locked() closes that gap with fcntl.flock on a sidecar <name>.lock file. A
flock is held per open file description and every locked() call opens its own
fd, so it excludes concurrent threads and concurrent processes alike. The lock
lives on a sidecar rather than the store file itself because
atomic_write_json() replaces the store's inode via os.replace() — a lock taken
on the old inode would no longer exclude anyone after a write.

load_json() also absorbs a corrupt store file. One bad byte in
wren_memory.json used to crash every chat turn and every scheduled run
(render_memory_block() seeds each conversation's system prompt). A file that
exists but won't parse is moved aside to <name>.corrupt-<timestamp> for
inspection, logged as an error, and treated as the empty default.

Usage — hold the lock across the whole read-modify-write:

    with locked(STORE_PATH):
        data = load_json(STORE_PATH, {"jobs": []})
        data["jobs"].append(job)
        atomic_write_json(STORE_PATH, data)

Lock-free reads of an atomically-written file are safe (a reader sees the old
or the new complete file, never a torn one), so read-only paths may call
load_json() without locked() when a momentarily stale answer is acceptable.
"""

import fcntl
import json
import logging
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


@contextmanager
def locked(store_path):
    """Exclusive lock scoped to `store_path`, valid across threads AND
    processes. Blocks until acquired; released when the with-block exits
    (closing the fd drops the flock, even if the process dies)."""
    fd = os.open(f"{store_path}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def load_json(store_path, default):
    """Parse the JSON file at `store_path`, or return `default` when the file
    doesn't exist yet. A file that exists but can't be read or parsed is moved
    aside to <name>.corrupt-<timestamp> and logged, and `default` is returned —
    a damaged store degrades to empty instead of crashing every caller.
    Callers should pass a fresh `default` (e.g. a dict literal) since it may be
    returned and then mutated."""
    store_path = Path(store_path)
    try:
        return json.loads(store_path.read_text())
    except FileNotFoundError:
        return default
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
        quarantine = store_path.with_name(
            f"{store_path.name}.corrupt-{datetime.now():%Y%m%dT%H%M%S}"
        )
        try:
            os.replace(store_path, quarantine)
            disposition = f"moved aside to {quarantine.name}"
        except OSError:
            disposition = "and it could not be moved aside"
        logger.error(
            "store %s is unreadable (%s) — %s; continuing with an empty store",
            store_path, e, disposition,
        )
        return default


def atomic_write_json(store_path, data) -> None:
    """Serialize `data` to a temp file in the store's directory, then
    os.replace() it into place (atomic on the same filesystem) — any concurrent
    reader sees a complete file, never a half-written one."""
    store_path = Path(store_path)
    fd, tmp = tempfile.mkstemp(
        dir=store_path.parent, prefix=f".{store_path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, store_path)
    except BaseException:
        os.unlink(tmp)
        raise
