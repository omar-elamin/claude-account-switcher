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


class TestPlanQuickRetry:
    """The pure retry-decision helper that makes usage self-heal after a
    transient unavailable fetch (e.g. the first read racing a Keychain prompt)."""

    def _states(self, app, *avails):
        UsageState = importlib.import_module("claude_switcher.usage_state").UsageState
        return [None if a is None else UsageState(available=a, display="") for a in avails]

    def test_all_available_stops_and_refills(self, app_module):
        st = self._states(app_module, True, True)
        assert app_module._plan_quick_retry(st, 1) == (False, app_module.QUICK_RETRY_BUDGET)

    def test_unavailable_with_budget_retries_and_decrements(self, app_module):
        st = self._states(app_module, True, False)
        assert app_module._plan_quick_retry(st, 3) == (True, 2)

    def test_budget_exhausted_gives_up(self, app_module):
        st = self._states(app_module, False)
        assert app_module._plan_quick_retry(st, 0) == (False, 0)

    def test_missing_state_counts_as_unavailable(self, app_module):
        st = self._states(app_module, None)
        assert app_module._plan_quick_retry(st, 2) == (True, 1)

    def test_no_accounts_is_not_a_failure(self, app_module):
        assert app_module._plan_quick_retry([], 1) == (False, app_module.QUICK_RETRY_BUDGET)

    def test_last_retry_then_stops(self, app_module):
        st = self._states(app_module, False)
        should, left = app_module._plan_quick_retry(st, 1)
        assert (should, left) == (True, 0)
        assert app_module._plan_quick_retry(st, left) == (False, 0)


class TestRefreshAndLoginRouting:
    def _app(self, app_module):
        # bare instance without running __init__ (avoids rumps/Cocoa)
        app = app_module.ClaudeSwitcherApp.__new__(app_module.ClaudeSwitcherApp)
        app._refresh_in_progress = True
        app._refresh_requested = False
        app._manual_refresh = False
        app._quick_retries_left = app_module.QUICK_RETRY_BUDGET
        app._usage_state_cache = {}
        app._usage_cache = {}
        return app

    def test_refresh_click_during_inflight_is_queued_not_dropped(self, app_module):
        app = self._app(app_module)
        app._fetch_all_usage()                 # in flight -> should queue
        assert app._refresh_requested is True

    def test_manual_refresh_sets_flag_and_notifies(self, app_module):
        app = self._app(app_module)
        app._refresh_in_progress = True        # queue path, no thread
        app_module.rumps.notification.reset_mock()
        app._on_refresh_usage(None)
        assert app._manual_refresh is True
        assert app_module.rumps.notification.called
        assert "Refreshing" in app_module.rumps.notification.call_args.kwargs["subtitle"]

    def test_login_required_row_routes_to_login_flow(self, app_module):
        UsageState = importlib.import_module("claude_switcher.usage_state").UsageState
        app = self._app(app_module)
        app._usage_state_cache[("codex", "dead@test.com")] = UsageState(available=False, display="Login required")
        app._on_add_codex_account = MagicMock()
        app._switch_account = MagicMock()
        app._on_codex_account_click(SimpleNamespace(_email="dead@test.com"))
        app._on_add_codex_account.assert_called_once()
        app._switch_account.assert_not_called()

    def test_healthy_row_still_switches(self, app_module):
        UsageState = importlib.import_module("claude_switcher.usage_state").UsageState
        app = self._app(app_module)
        app._usage_state_cache[("codex", "ok@test.com")] = UsageState(available=True, display="1h 10%")
        app._on_add_codex_account = MagicMock()
        app._switch_account = MagicMock()
        app._on_codex_account_click(SimpleNamespace(_email="ok@test.com"))
        app._switch_account.assert_called_once_with("codex", "ok@test.com")


class TestLeaseKeepsLastKnownUsage:
    """Regression: while a sign-in is in progress (add-lease held), rows for that
    provider must keep their last known numbers, not flip to 'Checking…'."""

    def test_leased_provider_rows_are_skipped_and_not_retried(self, app_module):
        UsageState = importlib.import_module("claude_switcher.usage_state").UsageState
        AccountInfo = importlib.import_module("claude_switcher.config").AccountInfo
        app = app_module.ClaudeSwitcherApp.__new__(app_module.ClaudeSwitcherApp)
        app._refresh_in_progress = False
        app._refresh_requested = False
        app._manual_refresh = False
        app._quick_retries_left = app_module.QUICK_RETRY_BUDGET
        app._usage_state_cache = {("codex", "a@test.com"): UsageState(available=True, display="1h 5%")}
        app._usage_cache = {("codex", "a@test.com"): "1h 5%"}
        app._usage_items = {}
        app.config_path = "unused"
        app._schedule_quick_retry = MagicMock()
        app._update_usage_labels = MagicMock()
        app._rebuild_menu = MagicMock()
        app._attempt_auto_switch = MagicMock(return_value=None)
        app._fetch_usage_state = MagicMock(return_value=UsageState(available=False, display="Usage unavailable"))
        accts = [AccountInfo("a@test.com", "plus", "", True, "a", provider="codex")]

        # run the fetch synchronously: patch Thread to call target inline, main-thread to inline
        import threading as _th
        class _Inline:
            def __init__(self, target, daemon=None): self.t = target
            def start(self): self.t()
        with patch.object(app_module, "load_accounts", return_value=accts), \
             patch.object(app_module, "get_active_account", return_value=None), \
             patch.object(app_module, "_add_lease_held", return_value=True), \
             patch.object(app_module.threading, "Thread", _Inline), \
             patch.object(app_module, "_on_main_thread", lambda fn: fn()):
            app._fetch_all_usage()

        app._fetch_usage_state.assert_not_called()                 # leased row skipped
        assert app._usage_cache[("codex", "a@test.com")] == "1h 5%" # last-known kept, not Checking…
        app._schedule_quick_retry.assert_not_called()               # no retry cycle during sign-in
