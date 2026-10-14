"""Tests for launchd/reload-after-upgrade.sh, run for real against a fake
launchd.

The script is copied into a temp root, $HOME points at a temp LaunchAgents
directory, and `launchctl` on PATH is a stub that keeps loaded/running state in
files. Nothing here touches the real launchd domain.
"""

import os
import plistlib
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "darwin",
                                reason="the script calls macOS plutil and stat")

REPO = Path(__file__).resolve().parent.parent

LAUNCHCTL = """#!/bin/bash
# print <domain>/<label> | bootout <domain>/<label> | bootstrap <domain> <plist>
state="$FAKE_LAUNCHD"
echo "$*" >> "$state/calls"
case "$1" in
    print)
        label="${2##*/}"
        [ -e "$state/loaded/$label" ] || exit 113
        [ -e "$state/running/$label" ] && echo "    state = running"
        exit 0 ;;
    bootout)
        rm -f "$state/loaded/${2##*/}" "$state/running/${2##*/}" ;;
    bootstrap)
        touch "$state/loaded/$(basename "$3" .plist)" ;;
esac
"""


def _executable(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@pytest.fixture
def world(tmp_path):
    root = tmp_path / "repo"
    (root / "launchd").mkdir(parents=True)
    (root / "config").mkdir()
    shutil.copy(REPO / "launchd" / "reload-after-upgrade.sh", root / "launchd")
    python = root / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    # Stands in for the interpreter (fingerprinted) and for push() (records).
    _executable(python, f'#!/bin/bash\necho "$*" >> "{tmp_path}/pushes"\n')

    home = tmp_path / "home"
    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    state = tmp_path / "launchd"
    (state / "loaded").mkdir(parents=True)
    (state / "running").mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _executable(bin_dir / "launchctl", LAUNCHCTL)

    def agent(label, running=False, **keys):
        plist = {"Label": label, "ProgramArguments": [str(python)], **keys}
        (agents / f"{label}.plist").write_bytes(plistlib.dumps(plist))
        (state / "loaded" / label).touch()
        if running:
            (state / "running" / label).touch()

    def run():
        env = {**os.environ, "HOME": str(home), "FAKE_LAUNCHD": str(state),
               "PATH": f"{bin_dir}:{os.environ['PATH']}"}
        return subprocess.run(["/bin/bash", str(root / "launchd" / "reload-after-upgrade.sh"),
                               "--quiet"], env=env, capture_output=True, text=True)

    def pushes():
        p = tmp_path / "pushes"
        return p.read_text().splitlines() if p.exists() else []

    def calls():
        return (state / "calls").read_text().splitlines()

    agent("local.wren.wren", KeepAlive=True)
    # A recorded fingerprint that no longer matches: the upgrade has happened.
    (root / "config" / ".interpreter_id").write_text("/old/python3.12 1 1\n")
    return {"agent": agent, "run": run, "pushes": pushes, "calls": calls,
            "fingerprint": root / "config" / ".interpreter_id"}


def test_an_always_on_daemon_is_reloaded_and_the_upgrade_is_recorded(world):
    # The mail watcher is KeepAlive, so it reads as "running" on every pass.
    # Skipping it as mid-run left the upgrade unrecorded forever, and the healer
    # reloaded every agent and pushed once an hour (2026-10-14).
    world["agent"]("local.wren.mailwatcher", running=True, KeepAlive=True)

    first = world["run"]()

    assert first.returncode == 0, first.stdout + first.stderr
    assert any(c.endswith("/local.wren.mailwatcher") and c.startswith("bootout")
               for c in world["calls"]())
    assert "/old/python3.12" not in world["fingerprint"].read_text()
    assert len(world["pushes"]()) == 1

    second = world["run"]()

    assert second.returncode == 0, second.stdout + second.stderr
    assert len(world["pushes"]()) == 1  # healed once, quiet after


def test_a_scheduled_job_caught_mid_run_is_still_left_alone(world):
    world["agent"]("local.wren.morningbrief", running=True,
                   StartCalendarInterval={"Hour": 6, "Minute": 30})

    result = world["run"]()

    assert result.returncode == 1
    assert "skipped local.wren.morningbrief" in result.stdout
    assert not any(c.startswith("bootout") and c.endswith("/local.wren.morningbrief")
                   for c in world["calls"]())
    assert "/old/python3.12" in world["fingerprint"].read_text()
