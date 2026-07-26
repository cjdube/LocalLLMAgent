"""Wren's procedural memory: reusable how-to procedures she's written down.

Where memory.py stores *facts* about Craig and wiki.py reads *externally-authored*
notes, this stores *procedure* — the multi-step recipe for a recurring task that
Wren (or Craig) figured out once and shouldn't have to re-derive. A skill composes
existing tools; it does not replace them (e.g. "trip prep -> get_upcoming_events for
the range, cross-check fetch_weather at the destination, summarize").

Storage is one Markdown file per skill under skills/ at the repo root (override with
WREN_SKILLS_DIR). The filename stem is the skill's slug — the name the model passes
to read_skill. Each file carries a one-line `description:` in a small frontmatter
block, then the procedure body:

    ---
    description: What this procedure is for, in one line.
    ---

    Step-by-step body...

render_skills_index() renders a capped "slug: description" list injected into the
chat system prompt (chat-only, like the wiki tools) so Wren knows what procedures
exist without reading every body — the bodies stay out of the prompt to protect the
tight num_ctx budget (see agent/loop.py). Capture is deliberate (Craig-initiated),
mirroring memory.py; write_skill and delete_skill are confirmation-gated in chat.

Usage:
    python -m agent.tools.skills list
    python -m agent.tools.skills read trip-prep
    python -m agent.tools.skills write trip-prep --description "..." --body "..."
    python -m agent.tools.skills delete trip-prep
"""

import argparse
import os
import re
import sys
from pathlib import Path

from agent.tools._http import load_env, print_result

load_env()

_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_SKILLS_DIR = _ROOT / "skills"

# Keep the injected index cheap: the chat prompt already crowds num_ctx=8192 and
# loop.py front-truncates the system prompt when a prompt hits that ceiling, so
# cap both how many skills and how many characters the index can add.
MAX_INDEX_SKILLS = 12
MAX_INDEX_CHARS = 800

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


def _skills_dir() -> Path:
    return Path(os.getenv("WREN_SKILLS_DIR", str(DEFAULT_SKILLS_DIR)))


def _slugify(name: str) -> str:
    """Normalise a skill name to a filesystem slug so 'Trip Prep' and 'trip-prep'
    resolve to the same file. write_skill and read_skill both slugify, so a skill
    saved under one spelling is found under any equivalent one."""
    s = (name or "").strip().lower()
    s = re.sub(r"[^\w\s-]", "", s)      # drop punctuation the model might add
    s = re.sub(r"[\s_]+", "-", s)       # spaces/underscores -> hyphen
    return s.strip("-")


def _safe_child(base: Path, name: str) -> Path:
    """Resolve `name` to a path inside `base`, rejecting any '../' escape. The
    name comes from the model, so it's untrusted (mirrors wiki._safe_child)."""
    candidate = (base / name).resolve()
    base = base.resolve()
    if base not in candidate.parents and candidate != base:
        raise ValueError(f"'{name}' resolves outside {base}")
    return candidate


def _parse_skill(text: str) -> dict:
    """Split a skill file into its one-line description and body. A file without
    the frontmatter block is treated as all-body with no description."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {"description": "", "body": text.strip()}
    frontmatter, body = m.group(1), m.group(2)
    description = ""
    for line in frontmatter.splitlines():
        key, sep, val = line.partition(":")
        if sep and key.strip() == "description":
            description = val.strip()
    return {"description": description, "body": body.strip()}


def _serialize_skill(description: str, body: str) -> str:
    return f"---\ndescription: {description}\n---\n\n{body}\n"


def _skill_path(name: str) -> tuple[str | None, Path | None, dict | None]:
    """Resolve a model-supplied name to (slug, path, None) or (None, None, error)."""
    if not name or not name.strip():
        return None, None, {"error": "name must not be empty"}
    slug = _slugify(name)
    if not slug:
        return None, None, {"error": f"'{name}' is not a valid skill name"}
    try:
        path = _safe_child(_skills_dir(), f"{slug}.md")
    except ValueError as e:
        return None, None, {"error": str(e)}
    return slug, path, None


def list_skills() -> dict:
    """Every saved skill's slug + one-line description (no bodies)."""
    d = _skills_dir()
    if not d.exists():
        return {"skills": []}
    out = []
    for p in sorted(d.iterdir()):
        if p.is_file() and p.suffix == ".md" and not p.name.startswith("."):
            parsed = _parse_skill(p.read_text(encoding="utf-8"))
            out.append({"name": p.stem, "description": parsed["description"]})
    return {"skills": out}


def read_skill(name: str) -> dict:
    slug, path, err = _skill_path(name)
    if err:
        return err
    if not path.is_file():
        return {"error": f"skill '{slug}' not found"}
    parsed = _parse_skill(path.read_text(encoding="utf-8"))
    return {"name": slug, "description": parsed["description"], "body": parsed["body"]}


def write_skill(name: str, description: str = "", body: str = "") -> dict:
    """Create or overwrite a skill by name. Writes the whole body — this replaces
    any existing procedure with the same slug."""
    slug, path, err = _skill_path(name)
    if err:
        return err
    body = (body or "").strip()
    if not body:
        return {"error": "a skill needs a body describing the procedure"}
    existed = path.is_file()
    _skills_dir().mkdir(parents=True, exist_ok=True)
    path.write_text(_serialize_skill((description or "").strip(), body), encoding="utf-8")
    return {"name": slug, "saved": True, "overwrote": existed}


def delete_skill(name: str) -> dict:
    slug, path, err = _skill_path(name)
    if err:
        return err
    if not path.is_file():
        return {"error": f"skill '{slug}' not found"}
    path.unlink()
    return {"name": slug, "removed": True}


def render_skills_index(logger=None) -> str:
    """The capped 'slug: description' block injected into the chat system prompt,
    or "" when there are no skills. Bodies are never included — Wren reads a
    skill's body on demand with read_skill.

    A skill dropped by either cap is invisible to Wren, who then never follows
    it — a silent degrade, so it's logged at WARNING with the counts and the
    names (CLAUDE.md). Mirrors wiki.render_lenses_index; unlike the lens index,
    skills are written by Wren rather than hand-authored, so the count cap is as
    likely to bite as the character budget. Rendered per turn, so a standing drop
    repeats every turn; log_inspector collapses identical warnings into a count
    rather than pushing each one."""
    skills = list_skills()["skills"]
    if not skills:
        return ""
    lines, total = [], 0
    for s in skills[:MAX_INDEX_SKILLS]:
        line = f"- {s['name']}: {s['description']}" if s["description"] else f"- {s['name']}"
        if total + len(line) > MAX_INDEX_CHARS:
            break
        lines.append(line)
        total += len(line)
    if len(lines) < len(skills) and logger:
        # Only ever a truncated prefix, so the dropped ones are the tail.
        dropped = [s["name"] for s in skills[len(lines):]]
        cause = (f"the {MAX_INDEX_CHARS}-char budget"
                 if len(lines) < min(len(skills), MAX_INDEX_SKILLS)
                 else f"the {MAX_INDEX_SKILLS}-skill cap")
        logger.warning(
            f"skills index truncated by {cause}: {len(lines)} of {len(skills)} skills "
            f"in the prompt ({total} chars), dropped {', '.join(dropped)} — dropped "
            f"skills are invisible in chat; shorten the frontmatter descriptions"
        )
    if not lines:
        return ""
    return (
        "Saved skills (procedures you've written down — read the relevant one with "
        "read_skill before following it, don't rely on the title alone):\n"
        + "\n".join(lines)
    )


LIST_SKILLS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "list_skills",
        "description": "List saved skills with their one-line descriptions. The system prompt "
        "already shows this index, so you rarely need this — use read_skill to open one.",
        "parameters": {"type": "object", "properties": {}},
    },
}

READ_SKILL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "read_skill",
        "description": "Read the full step-by-step body of one saved skill by its slug. Do this "
        "before following a procedure — the index only shows the one-line summary, not the steps.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The skill's name/slug, e.g. 'trip-prep'.",
                },
            },
            "required": ["name"],
        },
    },
}

WRITE_SKILL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "write_skill",
        "description": "Save a reusable procedure as a skill — for remembering HOW to do a "
        "multi-step task (the sequence of tools and steps), not a plain fact (use remember/pin for "
        "facts). Saving under an existing name overwrites it, so pass the complete, updated body.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "A short slug for the skill, e.g. 'trip-prep'.",
                },
                "description": {
                    "type": "string",
                    "description": "A one-line summary of what the procedure is for — shown in the "
                    "skills index so you know when to reach for it.",
                },
                "body": {
                    "type": "string",
                    "description": "The step-by-step procedure, referencing the tools to use at "
                    "each step.",
                },
            },
            "required": ["name", "body"],
        },
    },
}

DELETE_SKILL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "delete_skill",
        "description": "Delete a saved skill by its name/slug when a procedure is stale or no longer "
        "wanted. This removes it for good.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "The skill's name/slug to delete, e.g. 'trip-prep'.",
                },
            },
            "required": ["name"],
        },
    },
}

SKILL_TOOL_SCHEMAS = [
    LIST_SKILLS_SCHEMA,
    READ_SKILL_SCHEMA,
    WRITE_SKILL_SCHEMA,
    DELETE_SKILL_SCHEMA,
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    p_read = sub.add_parser("read")
    p_read.add_argument("name")
    p_write = sub.add_parser("write")
    p_write.add_argument("name")
    p_write.add_argument("--description", default="")
    p_write.add_argument("--body", required=True)
    p_delete = sub.add_parser("delete")
    p_delete.add_argument("name")
    args = parser.parse_args()

    if args.cmd == "list":
        result = list_skills()
    elif args.cmd == "read":
        result = read_skill(args.name)
    elif args.cmd == "write":
        result = write_skill(args.name, args.description, args.body)
    else:
        result = delete_skill(args.name)

    return print_result(result)


if __name__ == "__main__":
    sys.exit(main())
