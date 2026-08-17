"""Run the sibling repo's wiki lint and hand back its findings, for /wiki/lint.

The checks themselves live in ObsidianWikiAgent's wiki_lint.py — 500 lines of
heuristics, each one tuned against a real false positive. Reimplementing them
here would fork that tuning on day one, so this module runs the real thing and
parses its `--json` output. That flag exists for this caller and logs nothing:
the sibling's own log file is what Wren's dashboard parses for that job's run
history, and a button press must not fabricate a run in it.

It is a **subprocess, never an import**. Both repos have a top-level `agent`
package, so importing across the seam would shadow one with the other. The
sibling's own interpreter runs its own code with its own dependencies.

Every failure mode returns {"error": ...} rather than raising, matching
agent/tools/wiki.py: the sibling repo lives outside this one and may be moved,
unmounted, or mid-upgrade, and that means "no lint today", not a 500 on the view.

Standalone-runnable and Flask-free like chat/insights.py; the blueprint that
serves it is chat/routes_wiki.py.

Usage:
    python -m chat.wikilint
"""

import json
import os
import subprocess
import threading
from pathlib import Path

from agent.tools.wiki import _vault

DEFAULT_LINT_ROOT = "~/Projects/ObsidianWikiAgent"

# The structural pass is pure Python over ~390 local files and takes about a
# second. This ceiling is here for the case where the interpreter itself is
# wedged (a Homebrew python bump mid-flight has done it before), not because the
# work is slow. A --fix run gets the same budget: it writes the same files.
LINT_TIMEOUT = 60

# wiki_lint.py exits 1 when it finds problems and 0 when it doesn't — findings
# are the normal outcome of an audit, not a failure. Treating a nonzero exit as
# an error would report every dirty vault as a broken lint, so the exit code is
# only consulted after stdout fails to parse.
_EXIT_OK = (0, 1)

# Findings are a pure function of the vault's wiki/ directory, so cache them on
# that directory's (name, mtime_ns) signature — the same shape as _TASKS_CACHE
# in chat/insights.py. An edit in Obsidian invalidates it for free, so the view
# can poll without re-running the subprocess. Locked because Flask runs threaded.
_LINT_CACHE: dict[str, tuple] = {}
_LINT_CACHE_LOCK = threading.Lock()


def _lint_root() -> Path:
    return Path(os.getenv("WREN_WIKI_LINT_ROOT", DEFAULT_LINT_ROOT)).expanduser()


def _wiki_signature() -> tuple:
    """(name, mtime_ns) for every page in the vault's wiki/, or () if there is
    no vault. () never matches a real signature, so a missing vault simply
    doesn't cache."""
    wiki_dir = _vault() / "wiki"
    if not wiki_dir.is_dir():
        return ()
    sig = []
    for path in sorted(wiki_dir.glob("*.md")):
        try:
            sig.append((path.name, path.stat().st_mtime_ns))
        except OSError:
            continue
    return tuple(sig)


def _run(fix: bool) -> dict:
    """One subprocess run of the sibling lint. Never raises."""
    root = _lint_root()
    python = root / ".venv" / "bin" / "python"
    script = root / "wiki_lint.py"
    if not script.is_file():
        return {"error": f"wiki_lint.py not found (check WREN_WIKI_LINT_ROOT): {script}"}
    if not python.is_file():
        return {"error": f"the lint repo has no virtualenv at {python}"}

    cmd = [str(python), str(script), "--vault", str(_vault()), "--json"]
    if fix:
        cmd.append("--fix")
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=LINT_TIMEOUT, cwd=str(root)
        )
    except subprocess.TimeoutExpired:
        return {"error": f"the lint did not finish within {LINT_TIMEOUT}s"}
    except OSError as e:
        return {"error": f"could not run the lint: {e}"}

    # stdout first, exit code second — see _EXIT_OK.
    try:
        payload = json.loads(proc.stdout)
    except ValueError:
        detail = (proc.stderr or proc.stdout or "").strip()[:400]
        return {"error": f"the lint returned no usable output (exit {proc.returncode})"
                         + (f": {detail}" if detail else "")}
    if not isinstance(payload, dict):
        return {"error": "the lint returned no usable output"}
    if "error" in payload:
        return {"error": str(payload["error"])}
    if proc.returncode not in _EXIT_OK:
        return {"error": f"the lint exited {proc.returncode}"}
    payload.setdefault("sections", {})
    payload.setdefault("fixes", [])
    payload["findings"] = sum(len(v) for v in payload["sections"].values())
    return payload


def run_lint(fix: bool = False) -> dict:
    """The structural findings, as
    {vault, pages, findings, sections: {section: [finding, ...]}, fixes: [...]}
    — or {"error": ...}.

    Cached on the vault's wiki/ signature (see _LINT_CACHE). `fix=True` writes to
    the vault, so it always runs and always replaces the cached entry: its own
    writes change the signature, and serving the pre-fix findings afterwards
    would show the user problems they just fixed.
    """
    if fix:
        result = _run(fix=True)
        with _LINT_CACHE_LOCK:
            if "error" in result:
                _LINT_CACHE.pop("entry", None)
            else:
                _LINT_CACHE["entry"] = (_wiki_signature(), result)
        return result

    signature = _wiki_signature()
    with _LINT_CACHE_LOCK:
        cached = _LINT_CACHE.get("entry")
        if cached is not None and cached[0] == signature:
            return cached[1]

    result = _run(fix=False)
    if "error" not in result:
        with _LINT_CACHE_LOCK:
            _LINT_CACHE["entry"] = (signature, result)
    return result


def main() -> int:
    result = run_lint()
    if "error" in result:
        print(f"error: {result['error']}")
        return 1
    print(f"{result['pages']} pages checked, {result['findings']} findings")
    for section, items in result["sections"].items():
        print(f"{len(items):5d}  {section}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
