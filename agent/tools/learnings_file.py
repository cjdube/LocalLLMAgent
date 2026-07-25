"""Write a learnings review to a Markdown file in Craig's Obsidian vault.

The daily learnings tasks each drop a standalone .md file (one per day) into the
vault's raw/ dir. Callers whose output isn't a *source* for the vault (e.g.
daily_synthesis, which archives nudges addressed to Craig) pass an explicit
`directory` instead, to stay out of the ingest queue. Returns dicts (not
print/exit); a missing target dir is surfaced as an error rather than created, so
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


def write_entry(content: str, prefix: str, day, directory: str | Path | None = None) -> dict:
    """Write `content` to <prefix>-<day:%Y-%m-%d>.md in `directory`, defaulting to
    the learnings dir. Does NOT create parent dirs: a missing dir means the
    configured path is wrong or the vault moved, and mkdir-ing it would file
    reviews into a stray tree nobody reads. Return an error instead and let the
    caller email the draft."""
    directory = Path(directory).expanduser() if directory else _learnings_dir()
    if not directory.exists():
        return {"error": f"target dir not found (check the caller's configured path): {directory}"}
    try:
        path = directory / f"{prefix}-{day:%Y-%m-%d}.md"
        path.write_text(content)
    except Exception as e:
        return {"error": str(e)}
    return {"written": True, "path": str(path)}


def read_entry(prefix: str, day, directory: str | Path | None = None) -> dict:
    """Read back the entry `write_entry` would have written for `day`, as
    {"content": ...}. A missing file is an error dict, not an exception: a task
    reading another task's output has to treat "it wrote nothing that day" as an
    ordinary outcome (tasks/daily_synthesis.py reads the AI-chat log this way).

    The flat path write_entry used is only where the file *starts*. ObsidianWikiAgent
    files everything it ingests out of raw/ into raw/<type>/ subdirs — in the real
    vault no .md remains at the root — so fall back to one level down. Without it a
    reader would look flat, find nothing, and degrade to silence forever."""
    directory = Path(directory).expanduser() if directory else _learnings_dir()
    filename = f"{prefix}-{day:%Y-%m-%d}.md"
    path = directory / filename
    if not path.is_file():
        filed = sorted(directory.glob(f"*/{filename}"))
        if not filed:
            return {"error": f"no entry for {filename} in {directory} or its subdirs"}
        path = filed[0]
    try:
        return {"content": path.read_text(encoding="utf-8")}
    except Exception as e:
        return {"error": str(e)}
