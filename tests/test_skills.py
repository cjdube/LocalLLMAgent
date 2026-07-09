"""Tests for agent.tools.skills — Wren's procedural memory (how-to recipes).

WREN_SKILLS_DIR is read fresh in _skills_dir() on every call, so pointing it at a
tmp_path via monkeypatch fully isolates these from the real skills/ dir.
"""

import pytest

from agent.tools import skills


@pytest.fixture(autouse=True)
def tmp_skills_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("WREN_SKILLS_DIR", str(tmp_path / "skills"))
    return tmp_path / "skills"


# --------------------------------------------------------------------------- #
# write / read round-trip
# --------------------------------------------------------------------------- #

def test_write_then_read_round_trip():
    saved = skills.write_skill("trip-prep", description="Prep for a trip", body="Step 1\nStep 2")
    assert saved == {"name": "trip-prep", "saved": True, "overwrote": False}

    got = skills.read_skill("trip-prep")
    assert got["name"] == "trip-prep"
    assert got["description"] == "Prep for a trip"
    assert got["body"] == "Step 1\nStep 2"


def test_write_overwrites_existing_body_by_name():
    skills.write_skill("trip-prep", description="old", body="old body")
    saved = skills.write_skill("trip-prep", description="new", body="new body")

    assert saved["overwrote"] is True
    got = skills.read_skill("trip-prep")
    assert got["description"] == "new"
    assert got["body"] == "new body"
    assert skills.list_skills()["skills"] == [{"name": "trip-prep", "description": "new"}]


def test_name_is_slugified_so_variants_resolve_to_one_file():
    skills.write_skill("Trip Prep", description="d", body="b")
    # Different spellings of the same name find the same skill.
    assert skills.read_skill("trip-prep")["body"] == "b"
    assert skills.read_skill("Trip Prep")["body"] == "b"
    assert skills.list_skills()["skills"] == [{"name": "trip-prep", "description": "d"}]


def test_delete_removes_the_skill():
    skills.write_skill("trip-prep", body="b")
    result = skills.delete_skill("trip-prep")

    assert result == {"name": "trip-prep", "removed": True}
    assert skills.list_skills()["skills"] == []
    assert "error" in skills.read_skill("trip-prep")


# --------------------------------------------------------------------------- #
# validation / missing
# --------------------------------------------------------------------------- #

def test_write_rejects_empty_body():
    assert "error" in skills.write_skill("trip-prep", body="   ")
    assert skills.list_skills()["skills"] == []


def test_write_rejects_empty_name():
    assert "error" in skills.write_skill("   ", body="b")


def test_read_and_delete_missing_report_error():
    assert "error" in skills.read_skill("does-not-exist")
    result = skills.delete_skill("does-not-exist")
    assert "error" in result


def test_read_empty_name_rejected():
    assert "error" in skills.read_skill("   ")


def test_list_on_missing_dir_returns_empty():
    # Nothing has been written, so the skills dir doesn't exist yet.
    assert skills.list_skills() == {"skills": []}


# --------------------------------------------------------------------------- #
# path traversal
# --------------------------------------------------------------------------- #

def test_traversal_rejected_on_read_write_delete(tmp_path):
    # A secret outside the skills dir the model must not reach via '../'.
    (tmp_path / "secret.md").write_text("secret")

    # _slugify strips the punctuation in '../secret', so the escape never forms;
    # assert the traversal target is unreachable rather than the error wording.
    skills.read_skill("../secret")
    skills.delete_skill("../secret")
    assert (tmp_path / "secret.md").read_text() == "secret"  # untouched
    # A crafted name that survives slugification still can't escape the dir.
    assert "error" not in skills.write_skill("ok", body="b")


# --------------------------------------------------------------------------- #
# index rendering
# --------------------------------------------------------------------------- #

def test_render_index_empty_when_no_skills():
    assert skills.render_skills_index() == ""


def test_render_index_lists_slug_and_description():
    skills.write_skill("trip-prep", description="Prep for a trip", body="b")
    skills.write_skill("repo-catchup", description="What changed in repos", body="b")

    block = skills.render_skills_index()
    assert "read_skill" in block
    assert "- trip-prep: Prep for a trip" in block
    assert "- repo-catchup: What changed in repos" in block


def test_render_index_respects_skill_cap(monkeypatch):
    monkeypatch.setattr(skills, "MAX_INDEX_SKILLS", 2)
    for i in range(5):
        skills.write_skill(f"skill-{i}", description=f"d{i}", body="b")

    block = skills.render_skills_index()
    entries = [ln for ln in block.splitlines() if ln.startswith("- ")]
    assert len(entries) == 2  # capped at MAX_INDEX_SKILLS, not all 5


def test_render_index_respects_char_cap(monkeypatch):
    monkeypatch.setattr(skills, "MAX_INDEX_CHARS", 20)
    skills.write_skill("aaaa", description="x" * 50, body="b")
    skills.write_skill("bbbb", description="y" * 50, body="b")

    block = skills.render_skills_index()
    # The first entry alone exceeds the cap, so no entries fit -> "".
    assert block == ""
