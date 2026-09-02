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


# --------------------------------------------------------------------------- #
# "Every HTTP call has an explicit timeout." — AGENTS.md, Data sourcing policy
# --------------------------------------------------------------------------- #

_HTTP_METHODS = frozenset(
    {"get", "post", "put", "patch", "delete", "head", "options", "request"}
)


def _unbounded_requests_calls(path: Path) -> list[str]:
    """`requests.<method>(...)` call sites in one file with no `timeout=`."""
    found = []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr not in _HTTP_METHODS:
            continue
        # Only the module itself. Matching any receiver named `session` caught
        # Flask's `session.get("authenticated")` in chat/auth.py, which is a
        # dict read and not a request at all.
        if not (isinstance(node.func.value, ast.Name) and node.func.value.id == "requests"):
            continue
        if not any(kw.arg == "timeout" for kw in node.keywords):
            found.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    return found


def test_every_http_call_carries_an_explicit_timeout():
    """A request with no timeout waits forever, and every caller here is either
    a scheduled task or a chat turn: one dead endpoint hangs the whole run, and
    a poller that hangs stops silently rather than failing. The rule was written
    down and followed by hand — `tasks/startup_recovery.py` still lost both of
    its launchctl calls to it — so it is checked mechanically now.
    """
    paths = [
        *sorted((ROOT / "agent").rglob("*.py")),
        *sorted((ROOT / "chat").rglob("*.py")),
        *sorted((ROOT / "tasks").rglob("*.py")),
    ]
    checked = sum(
        1
        for path in paths
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _HTTP_METHODS
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "requests"
    )
    assert checked > 20, f"only {checked} call sites found — the scan missed them"

    unbounded = [site for path in paths for site in _unbounded_requests_calls(path)]
    assert not unbounded, f"HTTP calls with no timeout: {unbounded}"


def _unbounded_subprocess_calls(path: Path) -> list[str]:
    """Blocking `subprocess.*` call sites in one file with no `timeout=`."""
    found = []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        # Popen is deliberately absent: it does not block and takes no timeout
        # at construction. chat/insights.py uses it to launch a task and return.
        if node.func.attr not in {"run", "check_output", "call", "check_call"}:
            continue
        if not (isinstance(node.func.value, ast.Name) and node.func.value.id == "subprocess"):
            continue
        if not any(kw.arg == "timeout" for kw in node.keywords):
            found.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    return found


def test_every_blocking_subprocess_call_carries_an_explicit_timeout():
    """Same failure as an unbounded HTTP call, one process further out. The two
    `launchctl` calls in `tasks/startup_recovery.py` had no bound, and that
    poller fires every 60 seconds: one wedged launchctl would have stopped
    every catch-up run after a reboot, silently and forever.
    """
    paths = [
        *sorted((ROOT / "agent").rglob("*.py")),
        *sorted((ROOT / "chat").rglob("*.py")),
        *sorted((ROOT / "tasks").rglob("*.py")),
    ]
    checked = sum(
        1
        for path in paths
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"run", "check_output", "call", "check_call"}
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subprocess"
    )
    assert checked >= 8, f"only {checked} call sites found — the scan missed them"

    unbounded = [site for path in paths for site in _unbounded_subprocess_calls(path)]
    assert not unbounded, f"blocking subprocess calls with no timeout: {unbounded}"
