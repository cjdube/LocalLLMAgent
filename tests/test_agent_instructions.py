"""Guard the single source of project instructions."""

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_compatibility_file_is_import_only():
    assert (ROOT / "CLAUDE.md").read_text(encoding="utf-8") == "@AGENTS.md\n"


def test_canonical_file_directs_instruction_edits_to_itself():
    guidance = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "This file is the sole source of project guidance" in guidance
    assert "including one that names `CLAUDE.md` — edit `AGENTS.md`" in guidance
