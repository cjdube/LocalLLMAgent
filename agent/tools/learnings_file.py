"""Write a learnings review to a Markdown file in Craig's Obsidian vault.

The daily learnings tasks each drop a standalone .md file (one per day) into the
vault's raw/ dir. Returns dicts (not print/exit); the target dir is an external
drive, so a missing dir is treated as "not mounted" and surfaced as an error so
the caller's email fallback fires.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(_ROOT / "config" / ".env")

DEFAULT_LEARNINGS_DIR = str(Path.home() / "Documents" / "llm-wiki-learnings" / "raw")


def _learnings_dir() -> Path:
    return Path(os.getenv("LEARNINGS_DIR", DEFAULT_LEARNINGS_DIR)).expanduser()


def write_entry(content: str, prefix: str, day) -> dict:
    """Write `content` to <prefix>-<day:%Y-%m-%d>.md in the learnings dir. Does
    NOT create parent dirs — the target lives on an external drive, so if it's
    unmounted we return an error (email fallback) rather than shadow the mount
    point on the boot disk."""
    directory = _learnings_dir()
    if not directory.exists():
        return {"error": f"learnings dir not found (drive not mounted?): {directory}"}
    try:
        path = directory / f"{prefix}-{day:%Y-%m-%d}.md"
        path.write_text(content)
    except Exception as e:
        return {"error": str(e)}
    return {"written": True, "path": str(path)}
