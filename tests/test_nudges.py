"""Tests for agent/tools/nudges.py — reading back the daily-synthesis archive.

SYNTHESIS_DIR is redirected to tmp_path suite-wide by
tests/conftest.py::_isolate_learnings_dir, so these tests write their fixture
archive there and never touch the real vault. TIMEZONE is pinned because the
window's cutoff is a local calendar day (CLAUDE.md: UTC→local day windows) and
the host's zone must not decide whether a file is in range.
"""

from datetime import date, timedelta

import pytest

from agent.tools import nudges as nudges_mod


@pytest.fixture(autouse=True)
def _pin_timezone(monkeypatch):
    monkeypatch.setenv("TIMEZONE", "America/New_York")


@pytest.fixture
def archive(tmp_path):
    """Write a Daily-Synthesis file `days_ago` days back. Returns the writer."""
    def write(days_ago: int, *lines: str, heading: bool = True) -> date:
        day = nudges_mod._today() - timedelta(days=days_ago)
        body = f"## Synthesis Suggestions: {day:%B %-d, %Y}\n\n" if heading else ""
        body += "".join(f"- {line}\n" for line in lines)
        (tmp_path / f"Daily-Synthesis-{day:%Y-%m-%d}.md").write_text(body)
        return day
    return write


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

def test_reads_the_bullets_and_dates_them_from_the_filename(archive):
    day = archive(1, "You dug into DuckDB — it fits your 'duckdb-analytics' note.")

    result = nudges_mod.list_nudges()

    assert result["nudges"] == [
        {"date": f"{day:%Y-%m-%d}",
         "text": "You dug into DuckDB — it fits your 'duckdb-analytics' note."},
    ]


def test_heading_is_not_a_nudge(archive):
    # The archive's "## Synthesis Suggestions" line must not come back as a
    # suggestion — it isn't one, and a model told it was would repeat it.
    archive(1, "One real nudge.")

    texts = [row["text"] for row in nudges_mod.list_nudges()["nudges"]]

    assert texts == ["One real nudge."]


def test_a_file_with_no_bullets_contributes_nothing(archive, tmp_path):
    archive(1, heading=True)

    assert nudges_mod.list_nudges()["nudges"] == []


def test_newest_first(archive):
    archive(5, "older")
    archive(1, "newer")

    texts = [row["text"] for row in nudges_mod.list_nudges()["nudges"]]

    assert texts == ["newer", "older"]


# --------------------------------------------------------------------------- #
# The window
# --------------------------------------------------------------------------- #

def test_window_excludes_files_older_than_days(archive):
    archive(2, "inside")
    archive(40, "outside")

    texts = [row["text"] for row in nudges_mod.list_nudges(days=7)["nudges"]]

    assert texts == ["inside"]


def test_days_is_clamped(archive):
    archive(1, "recent")
    archive(5, "older")

    # A zero or negative window would otherwise mean "nothing", which reads as a
    # broken archive rather than a bad argument: clamped to MIN_DAYS, it still
    # returns yesterday's file (the only day the task can have written).
    assert nudges_mod.list_nudges(days=0)["days"] == nudges_mod.MIN_DAYS
    assert nudges_mod.list_nudges(days=9999)["days"] == nudges_mod.MAX_DAYS
    assert [r["text"] for r in nudges_mod.list_nudges(days=-3)["nudges"]] == ["recent"]


def test_non_integer_days_falls_back_to_the_default(archive):
    archive(1, "recent")

    assert nudges_mod.list_nudges(days="a week")["days"] == nudges_mod.DEFAULT_DAYS


def test_unrelated_files_in_the_directory_are_ignored(archive, tmp_path):
    archive(1, "real")
    (tmp_path / "notes.md").write_text("- not a nudge\n")
    (tmp_path / "Daily-Synthesis-not-a-date.md").write_text("- not a nudge\n")

    texts = [row["text"] for row in nudges_mod.list_nudges()["nudges"]]

    assert texts == ["real"]


# --------------------------------------------------------------------------- #
# Degrading
# --------------------------------------------------------------------------- #

def test_missing_directory_is_an_error_not_an_exception(monkeypatch, tmp_path):
    # A wrong SYNTHESIS_DIR must degrade to "no nudges" in the chat turn that
    # asked, the way wiki.py does for a missing vault.
    monkeypatch.setenv("SYNTHESIS_DIR", str(tmp_path / "gone"))

    result = nudges_mod.list_nudges()

    assert "error" in result and "SYNTHESIS_DIR" in result["error"]


def test_empty_archive_returns_no_nudges_not_an_error():
    # Most days produce nothing at all, so an empty dir is the healthy case.
    result = nudges_mod.list_nudges()
    assert result["nudges"] == [] and result["days"] == nudges_mod.DEFAULT_DAYS
    assert "normal" in result["summary"]


# --------------------------------------------------------------------------- #
# The rendered summary — what the model actually replies with
# --------------------------------------------------------------------------- #

def test_summary_carries_every_nudge_verbatim(archive):
    day = archive(1, "First one — with an em dash.", "Second one.")

    summary = nudges_mod.list_nudges()["summary"]

    assert "First one — with an em dash." in summary
    assert "Second one." in summary
    assert f"{day:%Y-%m-%d}" in summary


def test_summary_is_what_the_model_relays_not_the_rows(archive):
    # The rows are still there for daily_synthesis, but the model is told to send
    # `summary` — it paraphrased a nudge that was never sent when asked to write
    # the list out itself. See _render.
    archive(1, "A nudge.")

    result = nudges_mod.list_nudges()

    assert result["nudges"] == [{"date": result["nudges"][0]["date"], "text": "A nudge."}]
    assert result["summary"].endswith("A nudge.")


def test_tool_description_tells_the_model_to_relay_the_summary():
    description = nudges_mod.TOOL_SCHEMA["function"]["description"]
    assert "`summary`" in description
    assert "Do not retype the list yourself" in description


# --------------------------------------------------------------------------- #
# The tool schema
# --------------------------------------------------------------------------- #

def test_tool_description_forbids_inventing_a_suggestion():
    # A registry-style "what did you suggest?" tool is the shape where the model
    # answers plausibly from its own head instead of calling anything, and a
    # fabricated suggestion is indistinguishable from a real one (CLAUDE.md, and
    # the measured list_games case). Pinned so a future trim can't drop it.
    description = nudges_mod.TOOL_SCHEMA["function"]["description"]
    assert "NOT something you know" in description
    assert "Never invent" in description


def test_tool_description_says_silence_is_normal():
    # Without this the model reads an empty result as a fault and says something
    # is broken; no nudges on a given day is the common case.
    assert "normal" in nudges_mod.TOOL_SCHEMA["function"]["description"]
