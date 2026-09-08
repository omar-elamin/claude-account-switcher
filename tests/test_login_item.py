import plistlib
from pathlib import Path

import pytest

from claude_switcher import login_item


def test_plist_path_and_enabled_state(tmp_path):
    assert login_item.plist_path(tmp_path) == tmp_path / "Library" / "LaunchAgents" / f"{login_item.LABEL}.plist"
    assert not login_item.is_enabled(tmp_path)
    login_item.enable(Path("/Applications/Claude Switcher.app"), home=tmp_path)
    assert login_item.is_enabled(tmp_path)
    login_item.disable(home=tmp_path)
    assert not login_item.is_enabled(tmp_path)


def test_enable_writes_exact_plist_and_is_idempotent(tmp_path):
    bundle = Path("/Applications/Claude Switcher.app")
    login_item.enable(bundle, home=tmp_path)
    login_item.enable(bundle, home=tmp_path)
    with login_item.plist_path(tmp_path).open("rb") as file:
        assert plistlib.load(file) == {
            "Label": "com.emilejouannet.claude-switcher",
            "ProgramArguments": ["/Applications/Claude Switcher.app/Contents/MacOS/Claude Switcher"],
            "RunAtLoad": True,
            "KeepAlive": False,
            "ProcessType": "Interactive",
        }


def test_enable_never_calls_launchctl(tmp_path, monkeypatch):
    # Bootstrapping while the app runs would start a second copy; the file alone is the mechanism.
    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("launchctl must not be called"))
    login_item.enable(Path("/Applications/Claude Switcher.app"), home=tmp_path)
    login_item.disable(home=tmp_path)


def test_disable_is_a_no_op_when_absent(tmp_path):
    login_item.disable(home=tmp_path)
    assert not login_item.is_enabled(tmp_path)


def test_unwritable_home_raises_plain_error(tmp_path):
    blocker = tmp_path / "Library"
    blocker.write_text("not a directory")
    with pytest.raises(RuntimeError, match="Could not write the launch agent"):
        login_item.enable(Path("/Applications/Claude Switcher.app"), home=tmp_path)
