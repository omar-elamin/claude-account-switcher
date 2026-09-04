"""Focused tests for credential-conflict reporting in the menu app."""

import importlib
import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


fake_rumps = types.ModuleType("rumps")
fake_rumps.App = type("App", (), {})
fake_rumps.MenuItem = type("MenuItem", (), {})
fake_rumps.Timer = type("Timer", (), {})
fake_rumps.separator = object()
fake_rumps.notification = MagicMock()
fake_rumps.alert = MagicMock()
fake_rumps.quit_application = MagicMock()

fake_foundation = types.ModuleType("Foundation")
fake_foundation.NSOperationQueue = MagicMock()

@pytest.fixture
def app_module():
    sys.modules.pop("claude_switcher.app", None)
    with patch.dict(sys.modules, {"rumps": fake_rumps, "Foundation": fake_foundation}):
        module = importlib.import_module("claude_switcher.app")
        yield module
    sys.modules.pop("claude_switcher.app", None)


class ImmediateThread:
    def __init__(self, target, daemon=False):
        self.target = target

    def start(self):
        self.target()


def _app_shell(app_mod, tmp_path):
    app = object.__new__(app_mod.ClaudeSwitcherApp)
    app.config_path = tmp_path / "accounts.json"
    app._switch_in_progress = set()
    app._rebuild_menu = MagicMock()
    app._fetch_all_usage = MagicMock()
    return app


def test_codex_add_lease_conflict_is_not_reported_as_switch_success(app_module, tmp_path):
    app = _app_shell(app_module, tmp_path)
    fake_rumps.notification.reset_mock()
    fake_rumps.alert.reset_mock()

    with patch.object(app_module, "get_active_account", return_value=None), patch.object(
        app_module, "switch_codex_account", side_effect=RuntimeError("add in progress")
    ), patch.object(app_module.threading, "Thread", ImmediateThread), patch.object(
        app_module, "_on_main_thread", side_effect=lambda fn: fn()
    ):
        app._switch_account("codex", "target@test.com")

    fake_rumps.alert.assert_called_once()
    assert "add in progress" in fake_rumps.alert.call_args.kwargs["message"]
    fake_rumps.notification.assert_not_called()


def test_busy_codex_removal_rebuilds_without_success_notification(app_module, tmp_path):
    app = _app_shell(app_module, tmp_path)
    sender = SimpleNamespace(_email="idle@test.com", _provider="codex")
    fake_rumps.notification.reset_mock()
    fake_rumps.alert.reset_mock()

    with patch.object(app_module, "get_active_account", return_value=None), patch.object(
        app_module, "remove_codex_account", return_value=False
    ):
        app._on_remove_account(sender)

    fake_rumps.notification.assert_not_called()
    fake_rumps.alert.assert_called_once()
    assert "try again in a moment" in fake_rumps.alert.call_args.kwargs["message"].lower()
    app._rebuild_menu.assert_called_once()
