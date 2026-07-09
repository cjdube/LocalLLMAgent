"""Tests for agent.tools.learnings_file — the local Markdown read/write that
replaced the Google Doc for weekly reviews.

LEARNINGS_DIR is read fresh in _learnings_dir() on every call, so pointing it
at a tmp_path via monkeypatch fully isolates these from the real vault.
"""

from datetime import date

from agent.tools import learnings_file as lf

SUNDAY = date(2026, 7, 5)


def test_write_weekly_entry_creates_named_file(tmp_path, monkeypatch):
    monkeypatch.setenv("LEARNINGS_DIR", str(tmp_path))
    result = lf.write_weekly_entry("## Strategic Weekly Review\n\ncontent", SUNDAY)

    expected = tmp_path / "Strategic-Weekly-Review-2026-07-05.md"
    assert result == {"written": True, "path": str(expected)}
    assert expected.read_text() == "## Strategic Weekly Review\n\ncontent"


def test_write_weekly_entry_missing_dir_errors_without_creating(tmp_path, monkeypatch):
    missing = tmp_path / "not-mounted"
    monkeypatch.setenv("LEARNINGS_DIR", str(missing))
    result = lf.write_weekly_entry("anything", SUNDAY)

    assert "error" in result and "not found" in result["error"]
    assert not missing.exists()  # did NOT shadow the mount point


def test_get_previous_entry_returns_newest(tmp_path, monkeypatch):
    monkeypatch.setenv("LEARNINGS_DIR", str(tmp_path))
    (tmp_path / "Strategic-Weekly-Review-2026-06-28.md").write_text("older")
    (tmp_path / "Strategic-Weekly-Review-2026-07-05.md").write_text("newest")

    assert lf.get_previous_entry_text() == "newest"


def test_get_previous_entry_skips_appledouble_sidecars(tmp_path, monkeypatch):
    monkeypatch.setenv("LEARNINGS_DIR", str(tmp_path))
    (tmp_path / "Strategic-Weekly-Review-2026-07-05.md").write_text("real entry")
    # A macOS '._'-prefixed sidecar with a *later* date must not win.
    (tmp_path / "._Strategic-Weekly-Review-2026-07-12.md").write_text("sidecar junk")

    assert lf.get_previous_entry_text() == "real entry"


def test_get_previous_entry_empty_when_no_files(tmp_path, monkeypatch):
    monkeypatch.setenv("LEARNINGS_DIR", str(tmp_path))
    assert lf.get_previous_entry_text() == ""
