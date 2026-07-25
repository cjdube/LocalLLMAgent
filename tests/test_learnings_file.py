"""Tests for agent.tools.learnings_file — the local Markdown write that backs the
daily learnings reviews, and the read-back one task uses on another's output.

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
    missing = tmp_path / "missing-learnings-dir"
    monkeypatch.setenv("LEARNINGS_DIR", str(missing))
    result = lf.write_entry("anything", "Daily-YouTube", DAY)

    assert "error" in result and "not found" in result["error"]
    assert not missing.exists()  # errored instead of creating a stray tree


def test_write_entry_honors_an_explicit_directory(tmp_path, monkeypatch):
    # daily_synthesis writes its nudge archive outside LEARNINGS_DIR this way.
    monkeypatch.setenv("LEARNINGS_DIR", str(tmp_path))
    other = tmp_path / "nudges"
    other.mkdir()
    result = lf.write_entry("nudge", "Daily-Synthesis", DAY, directory=other)

    assert result["path"] == str(other / "Daily-Synthesis-2026-07-12.md")
    assert not (tmp_path / "Daily-Synthesis-2026-07-12.md").exists()


def test_read_entry_round_trips_a_written_file(tmp_path, monkeypatch):
    monkeypatch.setenv("LEARNINGS_DIR", str(tmp_path))
    lf.write_entry("## AI Chat Learnings\n\n- a topic", "AI-Chat-Learnings", DAY)

    assert lf.read_entry("AI-Chat-Learnings", DAY) == {
        "content": "## AI Chat Learnings\n\n- a topic"}


def test_read_entry_finds_a_file_filed_into_a_subdir(tmp_path, monkeypatch):
    # ObsidianWikiAgent moves ingested files from raw/ into raw/<type>/, so the flat
    # path is only where a file starts. Reading flat-only would degrade to silence.
    monkeypatch.setenv("LEARNINGS_DIR", str(tmp_path))
    filed = tmp_path / "daily-ai"
    filed.mkdir()
    (filed / "AI-Chat-Learnings-2026-07-12.md").write_text("- filed away")

    assert lf.read_entry("AI-Chat-Learnings", DAY) == {"content": "- filed away"}


def test_read_entry_missing_file_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("LEARNINGS_DIR", str(tmp_path))
    result = lf.read_entry("AI-Chat-Learnings", DAY)

    assert "error" in result and "no entry" in result["error"]
