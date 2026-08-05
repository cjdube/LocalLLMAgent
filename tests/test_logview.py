"""Tests for chat/logview.py — the /logs viewer's reader.

The tests that matter here are the ones about *bounds*: this module exists
because logs/<name>.launchd.log is append-only and rotated by nothing, so a read
that quietly loads the whole file would work fine today and degrade silently as
the file grows. Those assertions (window size, per-entry caps) are pinned against
the constants rather than against a fixture's contents.

conftest redirects insights.LOGS_DIR to tmp_path suite-wide, so every log written
here lands in a throwaway dir; discover_tasks() is fed fixture plists the same way.
"""

import plistlib

import pytest

from chat import insights, logview


@pytest.fixture(autouse=True)
def _fixture_dirs(tmp_path, monkeypatch):
    """Point both halves of task discovery at tmp dirs.

    LOGS_DIR is already redirected by conftest; LAUNCHD_DIR is not, and
    discover_tasks() would otherwise parse the repo's real plists and hand back
    paths into the production logs/.
    """
    launchd = tmp_path / "launchd"
    launchd.mkdir()
    monkeypatch.setattr(insights, "LAUNCHD_DIR", launchd)
    monkeypatch.setattr(insights, "LOGS_DIR", tmp_path)
    insights._TASKS_CACHE.clear()
    yield launchd
    insights._TASKS_CACHE.clear()


def write_plist(launchd_dir, key, *, daemon=False):
    data = {
        "Label": f"local.wren.{key}",
        "ProgramArguments": ["python", "-m", f"tasks.{key}"],
        "StandardOutPath": f"/anywhere/logs/{key}.launchd.log",
    }
    if daemon:
        data["KeepAlive"] = True
    else:
        data["StartCalendarInterval"] = {"Hour": 6, "Minute": 0}
    (launchd_dir / f"{key}.plist").write_bytes(plistlib.dumps(data))


def line(ts, level, msg):
    return f"2026-08-05 {ts},000 [{level}] {msg}"


# --------------------------------------------------------------------------- #
# Catalogue and path safety
# --------------------------------------------------------------------------- #

def test_lists_both_streams_for_a_task(tmp_path, _fixture_dirs):
    write_plist(_fixture_dirs, "morning_brief")
    (tmp_path / "morning_brief.log").write_text(line("06:00:00", "INFO", "hi") + "\n")
    (tmp_path / "morning_brief.launchd.log").write_text("crash\n")

    entry = logview.list_logs()[0]
    assert entry["key"] == "morning_brief"
    assert set(entry["streams"]) == {"log", "stdout"}


def test_a_task_with_no_log_file_is_omitted(tmp_path, _fixture_dirs):
    write_plist(_fixture_dirs, "never_ran")
    assert logview.list_logs() == []


def test_orphan_log_is_listed_after_tasks(tmp_path, _fixture_dirs):
    write_plist(_fixture_dirs, "morning_brief")
    (tmp_path / "morning_brief.log").write_text(line("06:00:00", "INFO", "hi") + "\n")
    (tmp_path / "retired_task.log").write_text(line("06:00:00", "INFO", "old") + "\n")

    keys = [e["key"] for e in logview.list_logs()]
    assert keys == ["morning_brief", "file:retired_task.log"]


def test_bak_files_are_not_listed(tmp_path, _fixture_dirs):
    (tmp_path / "weekly_learnings.log.bak").write_text("frozen\n")
    assert logview.list_logs() == []


def test_rotated_siblings_are_counted_not_read(tmp_path, _fixture_dirs):
    """The viewer reads the live file only; `rotated` is how the page says so."""
    write_plist(_fixture_dirs, "morning_brief")
    (tmp_path / "morning_brief.log").write_text(line("06:00:00", "INFO", "live") + "\n")
    (tmp_path / "morning_brief.log.1").write_text(line("05:00:00", "INFO", "older") + "\n")

    assert logview.list_logs()[0]["streams"]["log"]["rotated"] == 1
    msgs = [e["msg"] for e in logview.read_log("morning_brief")["entries"]]
    assert msgs == ["live"]


@pytest.mark.parametrize("key", [
    "../../etc/passwd",
    "file:../../etc/passwd",
    "/etc/passwd",
    "file:/etc/passwd",
    "morning_brief/../../../etc/passwd",
])
def test_path_traversal_keys_resolve_to_nothing(tmp_path, _fixture_dirs, key):
    write_plist(_fixture_dirs, "morning_brief")
    (tmp_path / "morning_brief.log").write_text(line("06:00:00", "INFO", "hi") + "\n")

    assert logview.resolve(key) is None
    assert logview.read_log(key) is None


def test_unknown_stream_resolves_to_nothing(tmp_path, _fixture_dirs):
    write_plist(_fixture_dirs, "morning_brief")
    (tmp_path / "morning_brief.log").write_text(line("06:00:00", "INFO", "hi") + "\n")
    assert logview.resolve("morning_brief", stream="stdout") is None


# --------------------------------------------------------------------------- #
# Entry folding
# --------------------------------------------------------------------------- #

def test_continuation_lines_fold_into_the_entry_above(tmp_path, _fixture_dirs):
    """~31% of real lines are continuations; a line-per-row read shreds them."""
    (tmp_path / "t.log").write_text("\n".join([
        line("06:00:00", "INFO", "Drafted entry:"),
        "## Daily Log",
        "- a bullet",
        line("06:00:01", "INFO", "done"),
    ]) + "\n")

    entries = logview.read_log("file:t.log")["entries"]
    assert len(entries) == 2
    assert entries[0]["extra"] == ["## Daily Log", "- a bullet"]
    assert entries[1]["extra"] == []


def test_leading_partial_entry_is_dropped_not_shown_whole(tmp_path, _fixture_dirs, monkeypatch):
    """A window that starts mid-line must not present the fragment as an entry."""
    monkeypatch.setattr(logview, "WINDOW_BYTES", 120)
    (tmp_path / "t.log").write_text("\n".join([
        line("06:00:00", "INFO", "x" * 100),
        line("06:00:01", "INFO", "second"),
        line("06:00:02", "INFO", "third"),
    ]) + "\n")

    data = logview.read_log("file:t.log")
    assert [e["msg"] for e in data["entries"]] == ["second", "third"]
    assert data["scanned"]["complete"] is False


def test_offsets_point_at_the_start_of_each_entry(tmp_path, _fixture_dirs):
    body = "\n".join([
        line("06:00:00", "INFO", "first"),
        line("06:00:01", "INFO", "second"),
    ]) + "\n"
    (tmp_path / "t.log").write_text(body)

    entries = logview.read_log("file:t.log")["entries"]
    assert entries[0]["offset"] == 0
    assert entries[1]["offset"] == len(body.split("\n")[0]) + 1


# --------------------------------------------------------------------------- #
# Bounds — why this module exists
# --------------------------------------------------------------------------- #

def test_read_is_bounded_by_the_window_not_the_file(tmp_path, _fixture_dirs):
    """The launchd logs grow without limit, so a full read would degrade silently."""
    filler = line("06:00:00", "INFO", "x" * 200) + "\n"
    big = tmp_path / "t.log"
    big.write_text(filler * ((logview.WINDOW_BYTES * 3) // len(filler)))
    assert big.stat().st_size > logview.WINDOW_BYTES * 2

    data = logview.read_log("file:t.log", limit=logview.MAX_LIMIT)
    scanned = data["scanned"]["to"] - data["scanned"]["from"]
    assert scanned <= logview.WINDOW_BYTES
    assert data["scanned"]["complete"] is False
    assert data["size"] == big.stat().st_size


def test_long_message_is_capped_and_the_drop_reported(tmp_path, _fixture_dirs):
    """The 46,683-char calendar result of 2026-07-10 is the case this bounds."""
    (tmp_path / "t.log").write_text(line("06:00:00", "INFO", "j" * 46_683) + "\n")

    entry = logview.read_log("file:t.log")["entries"][0]
    assert len(entry["msg"]) == logview.MAX_MSG_CHARS
    assert entry["dropped_chars"] == 46_683 - logview.MAX_MSG_CHARS


def test_long_continuation_block_is_capped_and_the_drop_reported(tmp_path, _fixture_dirs):
    extra = ["line %d" % i for i in range(logview.MAX_EXTRA_LINES + 25)]
    (tmp_path / "t.log").write_text(
        "\n".join([line("06:00:00", "INFO", "Drafted entry:")] + extra) + "\n")

    entry = logview.read_log("file:t.log")["entries"][0]
    assert len(entry["extra"]) == logview.MAX_EXTRA_LINES
    assert entry["dropped_lines"] == 25


def test_limit_is_clamped(tmp_path, _fixture_dirs):
    (tmp_path / "t.log").write_text(
        "".join(line("06:00:00", "INFO", f"m{i}") + "\n" for i in range(50)))
    assert len(logview.read_log("file:t.log", limit=10_000)["entries"]) == 50
    assert len(logview.read_log("file:t.log", limit=-5)["entries"]) == 1


# --------------------------------------------------------------------------- #
# Paging
# --------------------------------------------------------------------------- #

def test_paging_backwards_covers_every_entry_exactly_once(tmp_path, _fixture_dirs):
    (tmp_path / "t.log").write_text(
        "".join(line("06:00:00", "INFO", f"m{i}") + "\n" for i in range(25)))

    seen, before, hops = [], None, 0
    while hops < 20:
        page = logview.read_log("file:t.log", limit=7, before=before)
        seen = [e["msg"] for e in page["entries"]] + seen
        before, hops = page["next_before"], hops + 1
        if before is None:
            break
    assert seen == [f"m{i}" for i in range(25)]


def test_last_page_reports_no_more(tmp_path, _fixture_dirs):
    (tmp_path / "t.log").write_text(line("06:00:00", "INFO", "only") + "\n")
    assert logview.read_log("file:t.log")["next_before"] is None


def test_entries_are_returned_oldest_first(tmp_path, _fixture_dirs):
    """The page renders newest-first, but the reversal is the client's job — runs
    pair start-to-end forwards and continuation lines attach downwards."""
    (tmp_path / "t.log").write_text("\n".join([
        line("06:00:00", "INFO", "first"),
        line("06:00:01", "INFO", "second"),
    ]) + "\n")
    assert [e["msg"] for e in logview.read_log("file:t.log")["entries"]] \
        == ["first", "second"]


# --------------------------------------------------------------------------- #
# The live tail
# --------------------------------------------------------------------------- #

def test_after_returns_only_what_was_appended(tmp_path, _fixture_dirs):
    log = tmp_path / "t.log"
    log.write_text(line("06:00:00", "INFO", "before the poll") + "\n")
    cursor = logview.read_log("file:t.log")["next_after"]

    with log.open("a") as fh:
        fh.write(line("06:00:05", "INFO", "brand new") + "\n")

    page = logview.read_log("file:t.log", after=cursor)
    assert [e["msg"] for e in page["entries"]] == ["brand new"]
    assert page["next_after"] > cursor


def test_a_poll_with_nothing_new_returns_nothing(tmp_path, _fixture_dirs):
    (tmp_path / "t.log").write_text(line("06:00:00", "INFO", "quiet") + "\n")
    cursor = logview.read_log("file:t.log")["next_after"]
    assert logview.read_log("file:t.log", after=cursor)["entries"] == []


def test_a_half_written_line_is_not_read_until_it_is_complete(tmp_path, _fixture_dirs):
    """Without this the tail shows a fragment, and the next poll re-reads the
    same line from its start and renders it a second time."""
    log = tmp_path / "t.log"
    log.write_text(line("06:00:00", "INFO", "complete") + "\n")
    cursor = logview.read_log("file:t.log")["next_after"]

    with log.open("a") as fh:
        fh.write(line("06:00:01", "INFO", "still being writt"))   # no newline yet
    mid = logview.read_log("file:t.log", after=cursor)
    assert mid["entries"] == []
    assert mid["next_after"] == cursor

    with log.open("a") as fh:
        fh.write("en\n")
    done = logview.read_log("file:t.log", after=cursor)
    assert [e["msg"] for e in done["entries"]] == ["still being written"]


def test_falling_far_behind_jumps_to_the_tail_and_says_so(tmp_path, _fixture_dirs):
    """A burst, or a Mac that slept: the catch-up read stays bounded like the
    rest, rather than growing to match the gap."""
    log = tmp_path / "t.log"
    log.write_text(line("06:00:00", "INFO", "start") + "\n")
    cursor = logview.read_log("file:t.log")["next_after"]

    filler = line("06:00:01", "INFO", "x" * 200) + "\n"
    with log.open("a") as fh:
        fh.write(filler * ((logview.WINDOW_BYTES * 2) // len(filler)))

    page = logview.read_log("file:t.log", after=cursor, limit=logview.MAX_LIMIT)
    assert page["scanned"]["skipped"] is True
    assert page["scanned"]["to"] - page["scanned"]["from"] <= logview.WINDOW_BYTES


def test_a_truncated_file_restarts_from_its_end(tmp_path, _fixture_dirs):
    """Rotation, or someone emptying the file, leaves the cursor past EOF."""
    log = tmp_path / "t.log"
    log.write_text(line("06:00:00", "INFO", "old and long" * 50) + "\n")
    cursor = logview.read_log("file:t.log")["next_after"]

    log.write_text(line("07:00:00", "INFO", "fresh") + "\n")
    page = logview.read_log("file:t.log", after=cursor)
    assert page["entries"] == []
    assert page["next_after"] <= log.stat().st_size


def test_after_wins_over_before_when_both_are_given(tmp_path, _fixture_dirs):
    (tmp_path / "t.log").write_text("\n".join([
        line("06:00:00", "INFO", "a"),
        line("06:00:01", "INFO", "b"),
    ]) + "\n")
    page = logview.read_log("file:t.log", after=0, before=10)
    assert [e["msg"] for e in page["entries"]] == ["a", "b"]


# --------------------------------------------------------------------------- #
# Filtering
# --------------------------------------------------------------------------- #

def test_level_filter_keeps_surrounding_context(tmp_path, _fixture_dirs):
    """The cause of a warning sits above it: the 46KB tool result that overflowed
    num_ctx on 2026-07-10 is three lines above the WARNING that reported it. A
    filter showing bare matches reports symptoms and hides causes."""
    (tmp_path / "t.log").write_text("\n".join([
        line("06:00:00", "INFO", "out of range"),
        line("06:00:01", "INFO", "cause"),
        line("06:00:02", "INFO", "noise"),
        line("06:00:03", "WARNING", "symptom"),
        line("06:00:04", "INFO", "after"),
        line("06:00:05", "INFO", "well after"),
        line("06:00:06", "INFO", "out of range"),
    ]) + "\n")

    entries = logview.read_log("file:t.log", level="warning")["entries"]
    assert [e["msg"] for e in entries] == \
        ["cause", "noise", "symptom", "after", "well after"]
    assert [e["context"] for e in entries] == [True, True, False, True, True]


def test_level_filter_is_a_minimum_not_an_equality(tmp_path, _fixture_dirs):
    (tmp_path / "t.log").write_text("\n".join([
        line("06:00:00", "WARNING", "warn"),
        line("06:00:01", "ERROR", "err"),
    ]) + "\n")

    hits = [e["msg"] for e in logview.read_log("file:t.log", level="warning")["entries"]
            if not e["context"]]
    assert hits == ["warn", "err"]


def test_text_filter_searches_continuation_lines_too(tmp_path, _fixture_dirs):
    (tmp_path / "t.log").write_text("\n".join([
        line("06:00:00", "ERROR", "chat turn failed"),
        "Traceback (most recent call last):",
        "  ReadTimeout",
    ]) + "\n")

    hits = [e for e in logview.read_log("file:t.log", query="readtimeout")["entries"]
            if not e["context"]]
    assert len(hits) == 1


def test_counts_describe_the_scanned_window(tmp_path, _fixture_dirs):
    (tmp_path / "t.log").write_text("\n".join([
        line("06:00:00", "INFO", "a"),
        line("06:00:01", "WARNING", "b"),
        line("06:00:02", "ERROR", "c"),
    ]) + "\n")

    data = logview.read_log("file:t.log", level="error")
    # Counts are of everything read, not of what survived the filter — the page
    # says "1 error in the last N KB", which needs the unfiltered tally.
    assert data["counts"] == {"info": 1, "warning": 1, "error": 1}
    assert data["scanned"]["entries"] == 3


# --------------------------------------------------------------------------- #
# Run boundaries
# --------------------------------------------------------------------------- #

def test_run_boundaries_are_marked_for_grouping(tmp_path, _fixture_dirs):
    (tmp_path / "t.log").write_text("\n".join([
        line("06:00:00", "INFO", "Starting morning brief run"),
        line("06:00:01", "INFO", "fetch_weather -> {}"),
        line("06:00:02", "INFO", "Morning brief run complete"),
    ]) + "\n")

    assert [e["boundary"] for e in logview.read_log("file:t.log")["entries"]] \
        == ["start", None, "end"]


def test_daemon_log_has_no_run_boundaries(tmp_path, _fixture_dirs):
    """The chat server's log is the one with no runs — and the one the dashboard
    drawer refuses outright, which is why the viewer must handle it."""
    (tmp_path / "t.log").write_text("\n".join([
        line("06:00:00", "INFO", "Starting Wren chat server on 127.0.0.1:8420"),
        line("06:00:01", "INFO", "chat turn start: 2 messages, 24 tools"),
    ]) + "\n")

    assert [e["boundary"] for e in logview.read_log("file:t.log")["entries"]] == [None, None]


def test_undecodable_bytes_do_not_raise(tmp_path, _fixture_dirs):
    (tmp_path / "t.log").write_bytes(
        line("06:00:00", "INFO", "caf").encode() + b"\xff\xfe\n")
    assert len(logview.read_log("file:t.log")["entries"]) == 1
