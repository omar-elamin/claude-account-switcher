"""Per-user login agent behavior without touching launchd."""

import importlib
import os
import plistlib
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import MagicMock, call

import pytest


@pytest.fixture
def login_item():
    return importlib.import_module("claude_switcher.login_item")


def test_plist_path_and_enabled_state(login_item, tmp_path):
    path = tmp_path / "Library/LaunchAgents/com.emilejouannet.claude-switcher.plist"
    assert login_item.LABEL == "com.emilejouannet.claude-switcher"
    assert login_item.plist_path(tmp_path) == path
    assert not login_item.is_enabled(tmp_path)
    path.parent.mkdir(parents=True)
    path.touch()
    assert login_item.is_enabled(tmp_path)
    path.unlink()
    assert not login_item.is_enabled(tmp_path)


def test_enable_writes_exact_plist_before_bootstrap(login_item, tmp_path):
    bundle = Path("/Applications/Claude Switcher.app")
    path = login_item.plist_path(tmp_path)

    def bootstrap(argv, **kwargs):
        with path.open("rb") as file:
            assert plistlib.load(file) == {
                "Label": "com.emilejouannet.claude-switcher",
                "ProgramArguments": ["/Applications/Claude Switcher.app/Contents/MacOS/Claude Switcher"],
                "RunAtLoad": True,
                "KeepAlive": False,
                "ProcessType": "Interactive",
            }
        return CompletedProcess(argv, 0, stderr="")

    run = MagicMock(side_effect=bootstrap)
    login_item.enable(bundle, home=tmp_path, run=run)
    run.assert_called_once_with(
        ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(path)],
        capture_output=True, text=True,
    )
    assert login_item.is_enabled(tmp_path)


@pytest.mark.parametrize("returncode,stderr", [(5, "Input/output error"), (1, "Service Already loaded")])
def test_already_loaded_falls_back_to_enable(login_item, tmp_path, returncode, stderr):
    run = MagicMock(side_effect=[
        CompletedProcess([], returncode, stderr=stderr),
        CompletedProcess([], 0, stderr=""),
    ])
    login_item.enable(Path("/Applications/Claude Switcher.app"), home=tmp_path, run=run)
    assert run.call_args_list == [
        call(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(login_item.plist_path(tmp_path))],
             capture_output=True, text=True),
        call(["launchctl", "enable", f"gui/{os.getuid()}/com.emilejouannet.claude-switcher"],
             capture_output=True, text=True),
    ]


@pytest.mark.parametrize("already_loaded", [False, True])
def test_launchctl_failure_raises(login_item, tmp_path, already_loaded):
    results = [CompletedProcess([], 1, stderr="Permission denied")]
    if already_loaded:
        results.insert(0, CompletedProcess([], 5, stderr=""))
    run = MagicMock(side_effect=results)
    with pytest.raises(RuntimeError, match="Permission denied"):
        login_item.enable(Path("/Applications/Claude Switcher.app"), home=tmp_path, run=run)
    assert run.call_count == len(results)


def test_launchctl_cannot_run_raises(login_item, tmp_path):
    run = MagicMock(side_effect=OSError("launchctl unavailable"))
    with pytest.raises(RuntimeError, match="launchctl unavailable"):
        login_item.enable(Path("/Applications/Claude Switcher.app"), home=tmp_path, run=run)


@pytest.mark.parametrize("returncode", [0, 1])
def test_disable_boots_out_before_removing_plist(login_item, tmp_path, returncode):
    path = login_item.plist_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.touch()

    def bootout(argv, **kwargs):
        assert path.exists()
        return CompletedProcess(argv, returncode, stderr="")

    run = MagicMock(side_effect=bootout)
    login_item.disable(home=tmp_path, run=run)
    run.assert_called_once_with(
        ["launchctl", "bootout", f"gui/{os.getuid()}/com.emilejouannet.claude-switcher"],
        capture_output=True, text=True,
    )
    assert not login_item.is_enabled(tmp_path)


def test_disable_absent_is_noop(login_item, tmp_path):
    run = MagicMock()
    login_item.disable(home=tmp_path, run=run)
    run.assert_not_called()
    assert not login_item.plist_path(tmp_path).exists()
