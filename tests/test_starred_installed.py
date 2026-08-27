"""Tests for tasks/starred_installed.py — main() resolves each tracked repo's
installed version from config/starred_installed.json (running a version_cmd,
reading Claude Code's plugin record, or taking a static version), caches the
result keyed by full_name, degrades a broken command to an error string rather
than failing the run, and prunes the cache to whatever the source currently
lists. subprocess is monkeypatched; no real commands run, and PLUGINS_PATH is
pointed at a fixture so the user's real ~/.claude is never read. Both store
paths are redirected to tmp_path by conftest."""

import subprocess

import pytest

from agent.store import atomic_write_json, load_json
from tasks import starred_installed as si


def _write_source(mapping):
    atomic_write_json(si.SOURCE_PATH, mapping)


class _Proc:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


@pytest.fixture(autouse=True)
def _isolate_plugin_record(tmp_path, monkeypatch):
    """PLUGINS_PATH points into the developer's real ~/.claude. Redirect it for
    every test in this file so no test result depends on which plugins this
    machine happens to have installed."""
    monkeypatch.setattr(si, "PLUGINS_PATH", tmp_path / "installed_plugins.json")


def _write_plugin_record(entries):
    """Claude Code's installed_plugins.json, shaped as the real file is."""
    atomic_write_json(si.PLUGINS_PATH, {"version": 2, "plugins": entries})


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
# _plugin_version — Claude Code's own installer record, no command run
# --------------------------------------------------------------------------- #

def test_plugin_version_reads_the_installer_record():
    # Shaped exactly as ~/.claude/plugins/installed_plugins.json is on disk.
    _write_plugin_record({
        "mattpocock-skills@claude-plugins-official": [{
            "scope": "user",
            "installPath": "/Users/x/.claude/plugins/cache/.../1.2.3",
            "version": "1.2.3",
            "installedAt": "2026-08-26T12:07:54.007Z",
            "gitCommitSha": "0ab1b63a410a03d3627979a109c8695de27af954",
        }],
    })
    assert si._plugin_version("mattpocock-skills@claude-plugins-official") == ("1.2.3", None)


def test_plugin_version_runs_no_subprocess(monkeypatch):
    # The whole point of a plugin entry: deterministic, so no PATH and no
    # command to go missing. Any subprocess use here fails the test.
    def _forbidden(*a, **k):
        raise AssertionError("a plugin lookup must not run a command")
    monkeypatch.setattr(si.subprocess, "run", _forbidden)
    _write_plugin_record({"p@m": [{"version": "9.9.9"}]})
    assert si._plugin_version("p@m") == ("9.9.9", None)


def test_plugin_version_uninstalled_is_error():
    _write_plugin_record({"other@m": [{"version": "1.0.0"}]})
    version, error = si._plugin_version("p@m")
    assert version is None
    assert "not installed" in error


def test_plugin_version_missing_record_file_is_error():
    # No installed_plugins.json at all (Claude Code never ran, or moved).
    version, error = si._plugin_version("p@m")
    assert version is None
    assert "no plugin record" in error


@pytest.mark.parametrize("data", [
    {"version": 3},                                  # schema bumped, plugins gone
    {"plugins": []},                                 # plugins is not an object
    {"plugins": {"p@m": []}},                        # installed at no scope
    {"plugins": {"p@m": [{"scope": "user"}]}},       # entry without a version
    {"plugins": {"p@m": "1.2.3"}},                   # entry list is not a list
])
def test_plugin_version_degrades_on_unexpected_shapes(data):
    # This file belongs to another program; a schema change must degrade to an
    # error string (and a blank cell), never a traceback that fails the run.
    atomic_write_json(si.PLUGINS_PATH, data)
    version, error = si._plugin_version("p@m")
    assert version is None
    assert error


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


def test_main_resolves_a_plugin_entry(monkeypatch):
    _write_source({"mattpocock/skills": {"plugin": "mattpocock-skills@claude-plugins-official"}})
    _write_plugin_record({
        "mattpocock-skills@claude-plugins-official": [{"scope": "user", "version": "1.2.3"}],
    })

    assert si.main() == 0
    entry = load_json(si.INSTALLED_PATH, {})["mattpocock/skills"]
    assert entry["version"] == "1.2.3"
    assert entry["source"] == "plugin"   # not "manual" — it was measured, not typed
    assert entry["error"] is None


def test_main_carries_over_a_plugin_version_that_stops_resolving(monkeypatch):
    # An uninstalled/renamed plugin degrades like any other failed check.
    atomic_write_json(si.INSTALLED_PATH, {
        "mattpocock/skills": {"version": "1.2.3", "source": "plugin", "error": None,
                              "checked_at": "2026-08-26T20:10:00+00:00"},
    })
    _write_source({"mattpocock/skills": {"plugin": "gone@m"}})
    _write_plugin_record({})

    assert si.main() == 0
    entry = load_json(si.INSTALLED_PATH, {})["mattpocock/skills"]
    assert entry["version"] == "1.2.3"
    assert entry["stale"] is True
    assert "not installed" in entry["error"]


def test_main_keeps_last_good_version_when_the_check_fails(monkeypatch):
    # A run where the command is simply missing from PATH must not wipe a
    # version an earlier run measured — the cache is rewritten whole each run.
    atomic_write_json(si.INSTALLED_PATH, {
        "a/cli": {"version": "2.5.0", "source": "cmd", "error": None,
                  "checked_at": "2026-08-26T20:10:00+00:00"},
    })
    _write_source({"a/cli": {"version_cmd": "acli --version"}})

    def _boom(*a, **k):
        raise FileNotFoundError("no such file: acli")
    monkeypatch.setattr(si.subprocess, "run", _boom)

    assert si.main() == 0
    entry = load_json(si.INSTALLED_PATH, {})["a/cli"]
    assert entry["version"] == "2.5.0"      # carried over
    assert entry["stale"] is True           # and not passed off as fresh
    assert "FileNotFoundError" in entry["error"]


def test_main_marks_a_recovered_check_fresh_again(monkeypatch):
    # The stale flag must clear once the command works, or the page warns forever.
    atomic_write_json(si.INSTALLED_PATH, {
        "a/cli": {"version": "2.5.0", "source": "cmd", "stale": True,
                  "error": "FileNotFoundError: nope", "checked_at": "2026-08-26T20:10:00+00:00"},
    })
    _write_source({"a/cli": {"version_cmd": "acli --version"}})
    monkeypatch.setattr(si.subprocess, "run", lambda *a, **k: _Proc(stdout="acli 2.6.0"))

    assert si.main() == 0
    entry = load_json(si.INSTALLED_PATH, {})["a/cli"]
    assert entry["version"] == "2.6.0"
    assert entry["error"] is None
    assert entry.get("stale") is not True


def test_main_records_error_when_there_is_no_prior_version(monkeypatch):
    # Nothing to carry over -> still a blank cell plus the error, as before.
    atomic_write_json(si.INSTALLED_PATH, {"a/cli": {"version": None, "source": "cmd"}})
    _write_source({"a/cli": {"version_cmd": "acli --version"}})

    def _boom(*a, **k):
        raise FileNotFoundError("nope")
    monkeypatch.setattr(si.subprocess, "run", _boom)

    assert si.main() == 0
    entry = load_json(si.INSTALLED_PATH, {})["a/cli"]
    assert entry["version"] is None
    assert entry.get("stale") is not True


def test_main_flags_entry_missing_every_field(monkeypatch):
    _write_source({"a/x": {"note": "oops"}})
    assert si.main() == 0
    store = load_json(si.INSTALLED_PATH, {})
    assert store["a/x"]["version"] is None
    # The error names all three ways of writing an entry, so a typo is fixable
    # from the tooltip alone.
    assert "no 'version_cmd', 'plugin' or 'version'" in store["a/x"]["error"]


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
