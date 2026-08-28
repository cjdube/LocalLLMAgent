"""Guard the project instructions and the architecture they make binding."""

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def test_compatibility_file_is_import_only():
    assert (ROOT / "CLAUDE.md").read_text(encoding="utf-8") == "@AGENTS.md\n"


def test_canonical_file_directs_instruction_edits_to_itself():
    guidance = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "This file is the sole source of project guidance" in guidance
    assert "including one that names `CLAUDE.md` — edit `AGENTS.md`" in guidance


_SCRIBEJAY_ALLOWED_IMPORTS = {
    "agent.prefs",
    "agent.store",
    "agent.activity_log",
    "agent.loop",
    "agent.tools.calendar",
    "agent.tools.email",
    "agent.tools.chrome_history",
    "agent.tools.youtube",
    "agent.tools.strava",
    "agent.tools.gmail_read",
    "agent.tools.clickup",
    "tasks._common",
    "tasks._urls",
}
_SCRIBEJAY_NARROW_IMPORTS = {
    "agent.loop": {"complete_text", "warm_model"},
    "agent.tools.gmail_read": {"fetch_sent_metadata", "my_address"},
    "agent.tools.clickup": {"closed_tasks"},
}


def _external_imports(path: Path):
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "agent":
                for name in node.names:
                    yield f"agent.{name.name}", None
            elif node.module.split(".")[0] in {"agent", "chat", "tasks", "scribejay"}:
                yield node.module, {name.name for name in node.names}
        elif isinstance(node, ast.Import):
            for name in node.names:
                if name.name.split(".")[0] in {"agent", "chat", "tasks", "scribejay"}:
                    yield name.name, None


def test_scribejay_imports_stay_on_the_documented_porch():
    violations = []
    for path in sorted((ROOT / "scribejay").glob("*.py")):
        for module, names in _external_imports(path):
            if module.startswith("scribejay"):
                continue
            if module not in _SCRIBEJAY_ALLOWED_IMPORTS:
                violations.append(f"{path.name}: {module} is outside the porch")
                continue
            allowed_names = _SCRIBEJAY_NARROW_IMPORTS.get(module)
            if allowed_names is not None and (names is None or not names <= allowed_names):
                violations.append(
                    f"{path.name}: {module} imports {sorted(names or [])}; "
                    f"allowed {sorted(allowed_names)}"
                )
    assert violations == []


def test_wren_never_imports_scribejay():
    violations = []
    for directory in ("agent", "chat", "tasks"):
        for path in sorted((ROOT / directory).rglob("*.py")):
            for module, _ in _external_imports(path):
                if module == "scribejay" or module.startswith("scribejay."):
                    violations.append(f"{path.relative_to(ROOT)}: {module}")
    assert violations == []
