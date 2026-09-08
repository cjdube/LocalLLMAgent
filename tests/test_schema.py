"""Drift guards on agent/schema.py, in both directions.

The settings page is only as true as this table. Two ways it can lie:

  Forward — a call site resolves a key through config.getenv that has no row.
  The resolver silently skips the schema layer, and the page never shows the
  field at all.

  Reverse — a key HAS a row but the code still reads it with a raw os.getenv.
  This is the worse one: the page shows the field, accepts an edit, says
  "Saved", and the code goes on reading somewhere else. Nothing errors.

Both run off one AST walk over agent/, chat/, tasks/ and evals/, resolving the
four shapes a key is actually spelled in this repo — config.getenv("X"),
os.getenv("X"), os.environ.get("X") and os.environ["X"] — plus module-level
NAME = "LITERAL" bindings, without which the walk is blind at
chat/insights.py's EXTERNAL_ROOTS_ENV.

The walker is unit-tested against a fixture below, because a walker that
quietly matched nothing would make both guards pass forever.
"""

import ast
from pathlib import Path

import pytest

from agent import schema

ROOT = Path(__file__).resolve().parent.parent

# Directories walked by the guards: everything that runs in production. tests/
# is out on purpose — it sets keys it never reads back, and a fixture is
# allowed to poke os.environ directly.
_WALKED_DIRS = ("agent", "chat", "tasks", "evals")

# Files whose raw os.getenv is legitimate. agent/config.py IS the resolver: it
# reads the environment for every key by name, which is the layer the reverse
# guard exists to funnel everyone else into.
_RAW_ENV_EXEMPT_FILES = frozenset({"agent/config.py"})

# Keys that have a row but are not migrated to config.getenv yet. What is left
# is exactly the set bound at import — the ones whose row already says
# applies="restart". The list goes empty in the next commit; a key may leave
# it, never rejoin it.
_RAW_ENV_EXEMPT_KEYS = frozenset({
    "BRIEF_TO_EMAIL", "DEFAULT_LOCATION", "FLASK_SECRET_KEY",
    "GOOGLE_HTTP_TIMEOUT_S", "MAIL_ACT_LABEL", "MAIL_BODY_CHAR_BUDGET",
    "MAIL_SEARCH_CHAR_BUDGET", "MAIL_THREAD_CHAR_BUDGET",
    "MAIL_WATCH_LABEL", "OLLAMA_MAX_TOOL_RESULT_CHARS",
    "WEB_FETCH_MAX_CHARS", "WREN_CHAT_BUSY_PROBE", "WREN_CHAT_HOST",
    "WREN_CHAT_MAX_HISTORY_CHARS", "WREN_CHAT_MODEL_TIMEOUT",
    "WREN_CHAT_PORT", "WREN_CHAT_SUMMARY_CHARS", "WREN_CHAT_TOKEN",
    "WREN_LOGS_DIR", "WREN_RESEARCH_MODEL_TIMEOUT",
})


# --------------------------------------------------------------------------- #
# The walker
# --------------------------------------------------------------------------- #

class _Reads(ast.NodeVisitor):
    """Collect (key, lineno, how) for every settings read in one module.

    `import_time` restricts the walk to code that runs on import: it does not
    descend into function bodies, but it DOES descend into class bodies,
    decorators and default arguments, all of which are evaluated at import.
    """

    def __init__(self, consts: dict, import_time: bool):
        self.consts = consts
        self.import_time = import_time
        self.reads: list[tuple[str, int, str]] = []

    def visit_FunctionDef(self, node):
        if not self.import_time:
            self.generic_visit(node)
            return
        for decorator in node.decorator_list:
            self.visit(decorator)
        for default in node.args.defaults:
            self.visit(default)
        for default in node.args.kw_defaults:
            if default is not None:
                self.visit(default)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node):
        if not self.import_time:
            self.generic_visit(node)
            return
        for default in node.args.defaults:
            self.visit(default)

    def visit_Call(self, node):
        how = None
        func = node.func
        if isinstance(func, ast.Attribute):
            if func.attr == "getenv" and isinstance(func.value, ast.Name):
                if func.value.id == "os":
                    how = "os.getenv"
                elif func.value.id == "config":
                    how = "config.getenv"
            elif (func.attr == "get" and isinstance(func.value, ast.Attribute)
                    and func.value.attr == "environ"):
                how = "os.environ.get"
        if how and node.args:
            key = self._literal(node.args[0])
            if key is not None:
                self.reads.append((key, node.lineno, how))
        self.generic_visit(node)

    def visit_Subscript(self, node):
        value = node.value
        if (isinstance(value, ast.Attribute) and value.attr == "environ"
                and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)):
            self.reads.append((node.slice.value, node.lineno, "os.environ[]"))
        self.generic_visit(node)

    def _literal(self, node) -> str | None:
        """The key name, or None when it cannot be resolved statically — an
        f-string built per call (WREN_<TASK>_BACKEND) must be skipped, never
        guessed at."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            return self.consts.get(node.id)
        return None


def _module_constants(tree: ast.Module) -> dict:
    """Module-level NAME = "LITERAL" bindings, so a key spelled through one is
    still visible. chat/insights.py is the one place in the repo that does it."""
    consts = {}
    for node in tree.body:
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            consts[node.targets[0].id] = node.value.value
    return consts


def env_reads(source: str, import_time: bool = False) -> list[tuple[str, int, str]]:
    """(key, lineno, how) for every settings read in `source`."""
    tree = ast.parse(source)
    visitor = _Reads(_module_constants(tree), import_time)
    if import_time:
        for node in tree.body:
            visitor.visit(node)
    else:
        visitor.visit(tree)
    return visitor.reads


def _production_files() -> list[Path]:
    files = []
    for directory in _WALKED_DIRS:
        files.extend(sorted((ROOT / directory).rglob("*.py")))
    return files


def _tree_reads(import_time: bool = False) -> list[tuple[str, str, int, str]]:
    """(relative path, key, lineno, how) across every production file."""
    out = []
    for path in _production_files():
        rel = str(path.relative_to(ROOT))
        for key, lineno, how in env_reads(path.read_text(encoding="utf-8"),
                                          import_time=import_time):
            out.append((rel, key, lineno, how))
    return out


# --------------------------------------------------------------------------- #
# Guard on the guard
# --------------------------------------------------------------------------- #

_FIXTURE = '''
import os
from agent import config

NAME_CONST = "FROM_CONST"

A = config.getenv("FROM_CONFIG")
B = os.getenv("FROM_GETENV")
C = os.environ.get("FROM_ENVIRON_GET")
D = os.environ["FROM_SUBSCRIPT"]
E = os.getenv(NAME_CONST)
F = os.getenv(f"WREN_{name.upper()}_BACKEND")


def later(x=os.getenv("FROM_DEFAULT_ARG")):
    return os.getenv("FROM_FUNCTION_BODY")
'''


def test_the_walker_resolves_all_four_shapes_and_skips_what_it_cannot():
    found = {key for key, _, _ in env_reads(_FIXTURE)}
    assert found == {
        "FROM_CONFIG", "FROM_GETENV", "FROM_ENVIRON_GET", "FROM_SUBSCRIPT",
        "FROM_CONST", "FROM_DEFAULT_ARG", "FROM_FUNCTION_BODY",
    }, "the walker missed a shape the repo actually uses"
    # The f-string key is unresolvable and must be skipped, not guessed at.
    assert not any("BACKEND" in key for key, _, _ in env_reads(_FIXTURE))


def test_the_walker_labels_how_each_key_was_read():
    how_by_key = {key: how for key, _, how in env_reads(_FIXTURE)}
    assert how_by_key["FROM_CONFIG"] == "config.getenv"
    assert how_by_key["FROM_GETENV"] == "os.getenv"
    assert how_by_key["FROM_ENVIRON_GET"] == "os.environ.get"
    assert how_by_key["FROM_SUBSCRIPT"] == "os.environ[]"


def test_the_import_time_walk_stops_at_function_bodies_but_not_defaults():
    found = {key for key, _, _ in env_reads(_FIXTURE, import_time=True)}
    assert "FROM_DEFAULT_ARG" in found, "a default argument runs at import"
    assert "FROM_FUNCTION_BODY" not in found, "a function body does not"


def test_the_tree_walk_finds_the_reads_that_are_actually_there():
    # A walker that silently matched nothing would make both drift guards pass
    # forever. The repo has ~100 settings reads; 40 is a floor, not a target.
    assert len(_tree_reads()) > 40


# --------------------------------------------------------------------------- #
# The two drift guards
# --------------------------------------------------------------------------- #

def test_every_key_resolved_through_config_has_a_schema_row():
    missing = sorted({
        (rel, key) for rel, key, _, how in _tree_reads()
        if how == "config.getenv" and schema.by_key(key) is None
    })
    assert not missing, (
        "config.getenv resolves these keys but agent/schema.py has no row for "
        f"them, so the schema layer is skipped and /settings never shows the "
        f"field: {missing}"
    )


def test_no_key_with_a_schema_row_is_still_read_raw():
    leaked = sorted({
        (rel, key, lineno) for rel, key, lineno, how in _tree_reads()
        if how != "config.getenv"
        and schema.by_key(key) is not None
        and key not in _RAW_ENV_EXEMPT_KEYS
        and rel not in _RAW_ENV_EXEMPT_FILES
    })
    assert not leaked, (
        "these keys have a /settings field but are read straight from the "
        "environment, so a save would appear to work and change nothing: "
        f"{leaked}"
    )


def test_the_exempt_list_only_names_keys_that_are_really_still_raw():
    # Otherwise the list outlives the migration and silently re-permits a
    # regression on a key that was already moved.
    still_raw = {key for _, key, _, how in _tree_reads() if how != "config.getenv"}
    stale = sorted(_RAW_ENV_EXEMPT_KEYS - still_raw)
    assert not stale, (
        f"these keys are already migrated — drop them from "
        f"_RAW_ENV_EXEMPT_KEYS: {stale}"
    )


# --------------------------------------------------------------------------- #
# Table invariants
# --------------------------------------------------------------------------- #

def test_keys_are_unique():
    keys = [s.key for s in schema.SETTINGS]
    assert len(keys) == len(set(keys))


def test_every_row_is_well_formed():
    for row in schema.SETTINGS:
        assert row.group in schema.GROUPS, row.key
        assert row.applies in schema.APPLIES, row.key
        assert row.type in ("str", "int", "float", "bool", "path", "choice"), row.key
        assert row.label and row.help, row.key
        if row.type == "choice":
            assert row.choices, row.key
            assert row.default in row.choices, row.key
        else:
            assert not row.choices, row.key
        if row.minimum is not None or row.maximum is not None:
            assert row.type in ("int", "float"), row.key


def test_a_locked_row_says_why():
    # A read-only field with no reason reads as a bug in the page.
    for row in schema.SETTINGS:
        if not row.editable:
            assert row.reason, f"{row.key} is locked but gives no reason"


def test_a_secret_row_carries_no_default():
    # A default would be rendered, and the whole point of the secret flag is
    # that nothing about the value reaches the page.
    for row in schema.SETTINGS:
        if row.secret:
            assert row.default == "", row.key


def test_every_shipped_preference_section_has_defaults():
    for name in schema.PREFERENCE_SECTIONS:
        assert name in schema.STRUCTURED_DEFAULTS, name
    assert schema.STRUCTURED_DEFAULTS["persona"]["user_name"]


def test_structured_defaults_drop_the_comment_keys():
    # Their text moves into each row's help, where the page shows it, instead
    # of living where only a JSON reader would find it.
    def comment_keys(value):
        if isinstance(value, dict):
            return ([k for k in value if k.startswith("_comment")]
                    + [c for v in value.values() for c in comment_keys(v)])
        if isinstance(value, list):
            return [c for v in value for c in comment_keys(v)]
        return []

    assert comment_keys(schema.STRUCTURED_DEFAULTS) == []


# --------------------------------------------------------------------------- #
# config/.env.example coverage
# --------------------------------------------------------------------------- #

def test_every_row_is_documented_in_env_example():
    text = (ROOT / "config" / ".env.example").read_text(encoding="utf-8")
    documented = set()
    for line in text.splitlines():
        line = line.lstrip("# ").strip()
        if "=" in line:
            documented.add(line.split("=", 1)[0].strip())
    undocumented = sorted(set(schema.keys()) - documented)
    assert not undocumented, (
        "every setting the page offers must also be documented in "
        f"config/.env.example: {undocumented}"
    )


# --------------------------------------------------------------------------- #
# `applies` against the chat server's real import closure
# --------------------------------------------------------------------------- #

def _chat_server_closure() -> set[str]:
    """Files reachable at import from chat/server.py — read off sys.modules
    after actually importing it, not approximated statically."""
    import sys

    import chat.server  # noqa: F401  (imported for the side effect)

    closure = set()
    for module in list(sys.modules.values()):
        filename = getattr(module, "__file__", None)
        if not filename:
            continue
        path = Path(filename).resolve()
        try:
            rel = path.relative_to(ROOT)
        except ValueError:
            continue
        if path.suffix == ".py" and ".venv" not in path.parts:
            closure.add(str(rel))
    return closure


@pytest.fixture(scope="module")
def import_time_reads_in_the_server() -> dict:
    """{key: [where]} for every key bound at import inside that closure."""
    closure = _chat_server_closure()
    found: dict[str, list[str]] = {}
    for rel, key, lineno, _ in _tree_reads(import_time=True):
        if rel in closure:
            found.setdefault(key, []).append(f"{rel}:{lineno}")
    return found


def test_every_import_time_read_in_the_server_is_marked_restart(
        import_time_reads_in_the_server):
    wrong = sorted(
        (key, where) for key, where in import_time_reads_in_the_server.items()
        if (row := schema.by_key(key)) is not None and row.applies != "restart"
    )
    assert not wrong, (
        "these keys are bound at import inside the chat server, so a save "
        "cannot reach the running process, but their row does not say "
        f"restart — the page would claim Saved and nothing would happen: {wrong}"
    )


def test_no_row_claims_restart_without_an_import_time_read(
        import_time_reads_in_the_server):
    stale = sorted(
        row.key for row in schema.SETTINGS
        if row.applies == "restart" and row.key not in import_time_reads_in_the_server
    )
    assert not stale, (
        "these rows show the amber restart banner but nothing binds them at "
        "import any more — a banner nobody needs is a banner that trains you "
        f"to ignore the ones you do: {stale}"
    )
