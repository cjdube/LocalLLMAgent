"""Tests for tasks/starred_installed.py — main() resolves each tracked repo's
installed version from config/starred_installed.json (running a version_cmd or
taking a static version), caches the result keyed by full_name, degrades a
broken command to an error string rather than failing the run, and prunes the
cache to whatever the source currently lists. subprocess is monkeypatched; no
real commands run. Both store paths are redirected to tmp_path by conftest."""

import subprocess

import pytest

from agent.store import atomic_write_json, load_json
from tasks import starred_installed as si


def _write_source(mapping):
    atomic_write_json(si.SOURCE_PATH, mapping)


class _Proc:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


# --------------------------------------------------------------------------- #
# _run_version_cmd (pure-ish, subprocess stubbed)
# --------------------------------------------------------------------------- #

def test_run_version_cmd_extracts_token_from_stdout(monkeypatch):
    monkeypatch.setattr(si.subprocess, "run", lambda *a, **k: _Proc(stdout="rtk 0.43.0\n"))
    assert si._run_version_cmd("rtk --version") == ("0.43.0", None)


def test_run_version_cmd_reads_stderr_too(monkeypatch):
    # Some tools print their version banner to stderr.
    monkeypatch.setattr(si.subprocess, "run", lambda *a, **k: _Proc(stderr="tool v1.2.3"))
    assert si._run_version_cmd("tool --version") == ("1.2.3", None)


def test_run_version_cmd_missing_binary_is_error(monkeypatch):
    def _boom(*a, **k):
        raise FileNotFoundError("no such file: nope")
    monkeypatch.setattr(si.subprocess, "run", _boom)
    version, error = si._run_version_cmd("nope --version")
    assert version is None
    assert "FileNotFoundError" in error


def test_run_version_cmd_timeout_is_error(monkeypatch):
    def _slow(*a, **k):
        raise subprocess.TimeoutExpired(cmd="x", timeout=si.CMD_TIMEOUT)
    monkeypatch.setattr(si.subprocess, "run", _slow)
    version, error = si._run_version_cmd("hang --version")
    assert version is None
    assert "TimeoutExpired" in error


def test_run_version_cmd_no_version_in_output_is_error(monkeypatch):
    monkeypatch.setattr(si.subprocess, "run", lambda *a, **k: _Proc(stdout="usage: ...", returncode=2))
    version, error = si._run_version_cmd("tool --version")
    assert version is None
    assert "no version in output" in error


# --------------------------------------------------------------------------- #
# main()
# --------------------------------------------------------------------------- #

def test_main_resolves_cmd_and_manual_entries(monkeypatch):
    _write_source({
        "a/cli": {"version_cmd": "acli --version"},
        "b/skill": {"version": "v1.1.0"},
    })
    monkeypatch.setattr(si.subprocess, "run", lambda *a, **k: _Proc(stdout="acli 2.5.0"))

    assert si.main() == 0
    store = load_json(si.INSTALLED_PATH, {})
    assert store["a/cli"]["version"] == "2.5.0"
    assert store["a/cli"]["source"] == "cmd"
    assert store["a/cli"]["error"] is None
    assert store["b/skill"] == {"version": "v1.1.0", "source": "manual",
                                "error": None, "checked_at": store["b/skill"]["checked_at"]}


def test_main_records_error_for_broken_cmd_without_failing(monkeypatch):
    _write_source({"a/cli": {"version_cmd": "broken --version"}})

    def _boom(*a, **k):
        raise FileNotFoundError("nope")
    monkeypatch.setattr(si.subprocess, "run", _boom)

    assert si.main() == 0  # one broken command does not fail the run
    store = load_json(si.INSTALLED_PATH, {})
    assert store["a/cli"]["version"] is None
    assert "FileNotFoundError" in store["a/cli"]["error"]


def test_main_flags_entry_missing_both_fields(monkeypatch):
    _write_source({"a/x": {"note": "oops"}})
    assert si.main() == 0
    store = load_json(si.INSTALLED_PATH, {})
    assert store["a/x"]["version"] is None
    assert "no 'version_cmd' or 'version'" in store["a/x"]["error"]


def test_main_prunes_entries_dropped_from_source(monkeypatch):
    atomic_write_json(si.INSTALLED_PATH, {"gone/repo": {"version": "1.0", "source": "manual"}})
    _write_source({"a/keep": {"version": "v2.0"}})

    assert si.main() == 0
    store = load_json(si.INSTALLED_PATH, {})
    assert "gone/repo" not in store  # cache rewritten from the current source
    assert store["a/keep"]["version"] == "v2.0"


def test_main_empty_when_no_source(monkeypatch):
    # No source file -> empty cache, clean exit (nothing tracked).
    assert si.main() == 0
    assert load_json(si.INSTALLED_PATH, {}) == {}
