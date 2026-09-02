"""Mechanical drift guards for claims that must match code or the file tree."""

import ast
import re
from pathlib import Path
from urllib.parse import unquote, urlparse


ROOT = Path(__file__).resolve().parent.parent


def _literal_call_keys(paths, function_name: str) -> set[str]:
    keys = set()
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id != function_name or not node.args:
                continue
            value = node.args[0]
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                keys.add(value.value.upper())
    return keys


def _documented_keys(text: str, marker: str) -> set[str]:
    paragraph = text.split(marker, 1)[1].split("\n\n", 1)[0]
    return set(re.findall(r"`([A-Z][A-Z_]*)`", paragraph))


def test_backend_key_inventory_matches_literal_call_sites():
    # ScribeJay's half of this guard went with ScribeJay: its keys are checked
    # against its own call sites in its own repo. Asserting them from here would
    # rglob a directory that no longer exists and compare two empty sets.
    docs = (ROOT / "docs" / "llm-backend.md").read_text(encoding="utf-8")
    wren_paths = [
        *sorted((ROOT / "agent").rglob("*.py")),
        *sorted((ROOT / "tasks").rglob("*.py")),
    ]
    assert _documented_keys(docs, "Wired task keys:") == _literal_call_keys(
        wren_paths, "resolve_backend"
    )


def test_local_markdown_links_resolve():
    files = [
        ROOT / "README.md",
        ROOT / "AGENTS.md",
        ROOT / "ANALYSIS.md",
        *sorted((ROOT / "docs").glob("*.md")),
    ]
    missing = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        for match in re.finditer(r"!?\[[^\]]*\]\(([^)]+)\)", text):
            target = match.group(1).strip().strip("<>")
            if (not target or target.startswith(("#", "/"))
                    or urlparse(target).scheme):
                continue
            relative = unquote(target.split("#", 1)[0])
            if relative and not (path.parent / relative).exists():
                missing.append(f"{path.relative_to(ROOT)} -> {target}")
    assert missing == []


def test_canonical_rule_links_point_to_agents_md():
    stale = []
    for path in sorted((ROOT / "docs").glob("*.md")):
        text = path.read_text(encoding="utf-8")
        if re.search(r"\]\([^)]*CLAUDE\.md", text):
            stale.append(str(path.relative_to(ROOT)))
    assert stale == []


def _external_root_names(line: str) -> set[str]:
    """The root NAMEs out of one WREN_EXTERNAL_TASK_ROOTS value.

    Entries are `name=path`, comma-separated, and a path may carry a
    `#local.<prefix>.` suffix — only the name before `=` matters here.
    """
    value = line.split("=", 1)[1].strip()
    return {entry.split("=", 1)[0].strip() for entry in value.split(",") if "=" in entry}


def test_env_example_federates_every_root_the_docs_describe():
    """A root documented but missing from the example default is invisible: a
    fresh checkout gets zero rows for it on /dashboard, /map and /logs, with no
    error anywhere. That is what happened to ScribeJay when it split out on
    2026-08-30 — four documents named it and the example did not.
    """
    pattern = re.compile(r"^WREN_EXTERNAL_TASK_ROOTS=.+$", re.MULTILINE)

    documented: set[str] = set()
    for path in sorted((ROOT / "docs").glob("*.md")):
        for line in pattern.findall(path.read_text(encoding="utf-8")):
            documented |= _external_root_names(line)
    assert documented, "no documented roots found — this guard would pass vacuously"

    example = pattern.findall((ROOT / "config" / ".env.example").read_text(encoding="utf-8"))
    assert len(example) == 1, f"expected one active default, found {example}"

    assert documented <= _external_root_names(example[0])
