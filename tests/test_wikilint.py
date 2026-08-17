"""Tests for chat/wikilint.py — running the sibling repo's lint.

subprocess.run is stubbed in every test here. Nothing spawns a real interpreter,
and nothing reaches the real ObsidianWikiAgent checkout or the real vault:
tests/conftest.py redirects WIKI_VAULT_PATH suite-wide and blocks run_lint
outright, so this file has to reach past its own guard (see _unblocked) to
exercise the code at all.

The case worth naming is exit code 1. wiki_lint.py exits 1 when it FINDS
problems, which is the ordinary outcome of an audit — treating that as failure
would report every dirty vault as a broken lint.
"""

import json
import os
import subprocess

os.environ.setdefault("WREN_CHAT_TOKEN", "test-token")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")

import pytest

from chat import wikilint


CLEAN = {"vault": "/v", "pages": 3, "sections": {"Orphan pages": []}, "fixes": []}
DIRTY = {"vault": "/v", "pages": 3,
         "sections": {"Orphan pages": ["a.md is an orphan — no other page links to it."],
                      "Page format": []},
         "fixes": []}


class _Proc:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


@pytest.fixture
def lint_repo(tmp_path, monkeypatch):
    """A stand-in ObsidianWikiAgent checkout: the two files _run looks for."""
    root = tmp_path / "ObsidianWikiAgent"
    (root / ".venv" / "bin").mkdir(parents=True)
    (root / ".venv" / "bin" / "python").write_text("#!/bin/sh\n")
    (root / "wiki_lint.py").write_text("# stub\n")
    monkeypatch.setenv("WREN_WIKI_LINT_ROOT", str(root))
    return root


# conftest's autouse guard replaces wikilint.run_lint per test, so tests here
# call this module-level reference instead — captured at import time, which is
# before any fixture runs, so it is the real function. Reaching past the guard
# is safe only because every test below stubs subprocess.run first: nothing in
# this file spawns an interpreter or touches a vault.
_real_run_lint = wikilint.run_lint


@pytest.fixture
def unblocked():
    """Clear the module-level findings cache between tests."""
    wikilint._LINT_CACHE.clear()
    yield
    wikilint._LINT_CACHE.clear()


def _stub_run(monkeypatch, proc, calls=None):
    def _fake(cmd, **kwargs):
        if calls is not None:
            calls.append((cmd, kwargs))
        return proc if not callable(proc) else proc(cmd)
    monkeypatch.setattr(subprocess, "run", _fake)


# --------------------------------------------------------------------------- #
# parsing the sibling's output
# --------------------------------------------------------------------------- #

def test_findings_with_exit_one_are_a_success(lint_repo, unblocked, monkeypatch):
    """The whole point: exit 1 means "found problems", not "the lint broke"."""
    _stub_run(monkeypatch, _Proc(json.dumps(DIRTY), returncode=1))
    result = _real_run_lint()
    assert "error" not in result
    assert result["findings"] == 1
    assert result["sections"]["Orphan pages"]


def test_clean_vault_exit_zero(lint_repo, unblocked, monkeypatch):
    _stub_run(monkeypatch, _Proc(json.dumps(CLEAN), returncode=0))
    result = _real_run_lint()
    assert result["findings"] == 0
    assert result["pages"] == 3


def test_command_names_the_vault_and_asks_for_json(lint_repo, unblocked, monkeypatch):
    calls = []
    _stub_run(monkeypatch, _Proc(json.dumps(CLEAN)), calls)
    _real_run_lint()
    cmd = calls[0][0]
    assert cmd[0].endswith(".venv/bin/python")
    assert cmd[1].endswith("wiki_lint.py")
    assert "--json" in cmd and "--fix" not in cmd
    assert "--vault" in cmd


def test_fix_passes_the_flag_and_returns_the_change_log(lint_repo, unblocked, monkeypatch):
    calls = []
    payload = dict(CLEAN, fixes=["a.md: removed 1 self-link"])
    _stub_run(monkeypatch, _Proc(json.dumps(payload)), calls)
    result = _real_run_lint(fix=True)
    assert "--fix" in calls[0][0]
    assert result["fixes"] == ["a.md: removed 1 self-link"]


# --------------------------------------------------------------------------- #
# degrading rather than raising
# --------------------------------------------------------------------------- #

def test_missing_lint_repo_is_an_error_not_a_crash(tmp_path, unblocked, monkeypatch):
    monkeypatch.setenv("WREN_WIKI_LINT_ROOT", str(tmp_path / "gone"))
    assert "wiki_lint.py not found" in _real_run_lint()["error"]


def test_missing_virtualenv_is_reported_separately(tmp_path, unblocked, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "wiki_lint.py").write_text("# stub\n")
    monkeypatch.setenv("WREN_WIKI_LINT_ROOT", str(root))
    assert "virtualenv" in _real_run_lint()["error"]


def test_timeout_is_an_error(lint_repo, unblocked, monkeypatch):
    def _slow(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, wikilint.LINT_TIMEOUT)
    monkeypatch.setattr(subprocess, "run", _slow)
    assert "did not finish" in _real_run_lint()["error"]


def test_unparseable_output_reports_the_exit_code_and_stderr(lint_repo, unblocked, monkeypatch):
    _stub_run(monkeypatch, _Proc("Traceback...", returncode=2, stderr="ImportError: agent"))
    err = _real_run_lint()["error"]
    assert "no usable output" in err and "exit 2" in err and "ImportError" in err


def test_error_object_from_the_lint_is_passed_through(lint_repo, unblocked, monkeypatch):
    _stub_run(monkeypatch, _Proc(json.dumps({"error": "no wiki/ directory in /v"}), returncode=2))
    assert _real_run_lint()["error"] == "no wiki/ directory in /v"


def test_unexpected_exit_code_with_valid_json_is_still_an_error(lint_repo, unblocked, monkeypatch):
    # Exit 2 is the sibling's "bad usage" code. Valid JSON body, but the run
    # didn't do what was asked, so it isn't served as findings.
    _stub_run(monkeypatch, _Proc(json.dumps(CLEAN), returncode=2))
    assert "exited 2" in _real_run_lint()["error"]


# --------------------------------------------------------------------------- #
# caching
# --------------------------------------------------------------------------- #

def _seed_vault(tmp_path, monkeypatch):
    wiki = tmp_path / "vault" / "wiki"
    wiki.mkdir(parents=True)
    (wiki / "a.md").write_text("# A\n")
    monkeypatch.setenv("WIKI_VAULT_PATH", str(tmp_path / "vault"))
    return wiki


def test_repeat_reads_do_not_re_run_the_subprocess(lint_repo, unblocked, tmp_path, monkeypatch):
    _seed_vault(tmp_path, monkeypatch)
    calls = []
    _stub_run(monkeypatch, _Proc(json.dumps(CLEAN)), calls)
    _real_run_lint()
    _real_run_lint()
    assert len(calls) == 1


def test_editing_a_page_invalidates_the_cache(lint_repo, unblocked, tmp_path, monkeypatch):
    wiki = _seed_vault(tmp_path, monkeypatch)
    calls = []
    _stub_run(monkeypatch, _Proc(json.dumps(CLEAN)), calls)
    _real_run_lint()
    os.utime(wiki / "a.md", (0, 0))     # an edit in Obsidian
    _real_run_lint()
    assert len(calls) == 2


def test_fix_always_runs_even_when_a_read_is_cached(lint_repo, unblocked, tmp_path, monkeypatch):
    _seed_vault(tmp_path, monkeypatch)
    calls = []
    _stub_run(monkeypatch, _Proc(json.dumps(CLEAN)), calls)
    _real_run_lint()
    _real_run_lint(fix=True)
    assert len(calls) == 2


def test_a_failed_run_is_not_cached(lint_repo, unblocked, tmp_path, monkeypatch):
    _seed_vault(tmp_path, monkeypatch)
    calls = []
    _stub_run(monkeypatch, _Proc("boom", returncode=2), calls)
    _real_run_lint()
    _real_run_lint()
    assert len(calls) == 2      # a transient failure must not stick
