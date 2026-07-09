"""Write the weekly Strategic Weekly Review to a Markdown file in Craig's
Obsidian vault, and read the most recent one back for carry-forward context.

weekly_learnings.py drops each review as a standalone .md file (one per week).
Returns dicts (not print/exit); the target dir is an external drive, so a
missing dir is treated as "not mounted" and surfaced as an error so the
caller's email fallback fires.

Usage:
    python -m agent.tools.learnings_file read-previous
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent.parent
load_dotenv(_ROOT / "config" / ".env")

DEFAULT_LEARNINGS_DIR = "/Volumes/T7/Obsidian/learnings/raw"
FILENAME_PREFIX = "Strategic-Weekly-Review"


def _learnings_dir() -> Path:
    return Path(os.getenv("LEARNINGS_DIR", DEFAULT_LEARNINGS_DIR))


def write_weekly_entry(content: str, sunday) -> dict:
    """Write `content` to Strategic-Weekly-Review-<sunday>.md in the learnings
    dir. Does NOT create parent dirs — the target lives on an external drive, so
    if it's unmounted we return an error (email fallback) rather than shadow the
    mount point on the boot disk."""
    directory = _learnings_dir()
    if not directory.exists():
        return {"error": f"learnings dir not found (drive not mounted?): {directory}"}
    try:
        path = directory / f"{FILENAME_PREFIX}-{sunday:%Y-%m-%d}.md"
        path.write_text(content)
    except Exception as e:
        return {"error": str(e)}
    return {"written": True, "path": str(path)}


def get_previous_entry_text(max_chars: int = 4000) -> str:
    """Return the text of the most recent weekly review file, for carry-forward
    context. Returns '' if the dir/files can't be read. Skips macOS AppleDouble
    sidecar files ('._'-prefixed) present in the folder."""
    try:
        files = [
            p for p in _learnings_dir().glob(f"{FILENAME_PREFIX}-*.md")
            if not p.name.startswith("._")
        ]
        if not files:
            return ""
        # ISO dates in the filename sort lexicographically, so the last is newest.
        latest = sorted(files, key=lambda p: p.name)[-1]
        return latest.read_text()[:max_chars]
    except Exception:
        return ""


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == "read-previous":
        print(get_previous_entry_text())
        return 0
    print("usage: python -m agent.tools.learnings_file read-previous", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
