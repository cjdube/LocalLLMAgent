"""Tests for agent.tools.learnings_file — the local Markdown write that backs the
daily learnings reviews.

LEARNINGS_DIR is read fresh in _learnings_dir() on every call, so pointing it at a
tmp_path via monkeypatch fully isolates these from the real vault.
"""

from datetime import date

from agent.tools import learnings_file as lf

DAY = date(2026, 7, 12)


def test_write_entry_names_by_prefix_and_date(tmp_path, monkeypatch):
    monkeypatch.setenv("LEARNINGS_DIR", str(tmp_path))
    result = lf.write_entry("## Daily Log\n\ncontent", "Daily-Chrome", DAY)

    expected = tmp_path / "Daily-Chrome-2026-07-12.md"
    assert result == {"written": True, "path": str(expected)}
    assert expected.read_text() == "## Daily Log\n\ncontent"


def test_write_entry_missing_dir_errors_without_creating(tmp_path, monkeypatch):
    missing = tmp_path / "not-mounted"
    monkeypatch.setenv("LEARNINGS_DIR", str(missing))
    result = lf.write_entry("anything", "Daily-YouTube", DAY)

    assert "error" in result and "not found" in result["error"]
    assert not missing.exists()  # did NOT shadow the mount point
