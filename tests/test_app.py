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
        app._on_add = MagicMock()
        app._switch_account = MagicMock()
        sender = SimpleNamespace(_email="dead@test.com", _provider="codex")
        app._on_codex_account_click(sender)
        app._on_add.assert_called_once_with(sender)
        app._switch_account.assert_not_called()

    def test_healthy_row_still_switches(self, app_module):
        UsageState = importlib.import_module("claude_switcher.usage_state").UsageState
        app = self._app(app_module)
        app._usage_state_cache[("codex", "ok@test.com")] = UsageState(available=True, display="1h 10%")
        app._on_add = MagicMock()
        app._switch_account = MagicMock()
        app._on_codex_account_click(SimpleNamespace(_email="ok@test.com", _provider="codex"))
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


class TestAddRestartsInProgressSignIn:
    """Add while a sign-in for that provider is open = "start over": cancel the
    old one, wait for its lease to release, start fresh. No separate Cancel UI."""

    def _app(self, app_module):
        app = app_module.ClaudeSwitcherApp.__new__(app_module.ClaudeSwitcherApp)
        app._signing_in_since = {}; app.config_path = "unused"
        app._rebuild_menu = MagicMock(); app._fetch_all_usage = MagicMock()
        return app

    def test_add_while_signing_in_cancels_then_starts_fresh(self, app_module):
        for provider, label, window in (("claude", "Claude", "browser"), ("codex", "Codex", "Terminal")):
            for held, released in ((True, True), (True, False), (False, True)):
                app = self._app(app_module)
                # Also cover restart during the grace before the worker takes its lease.
                if not held:
                    app._signing_in_since[provider] = 99.0
                app_module.rumps.notification.reset_mock()
                check, add, cancel = MagicMock(return_value=True), MagicMock(return_value=None), MagicMock()
                events = []
                cancel.side_effect = lambda: events.append("cancel")
                add.side_effect = lambda path: events.append("add")
                def wait(p):
                    assert p == provider
                    events.append("wait")
                    return released
                entry = (label, check, add, cancel, f"Sign in in the {window} window that appears, then come back here.")
                with patch.dict(app_module.ADD_PROVIDERS, {provider: entry}), \
                     patch.object(app_module, "_add_lease_held", return_value=held), \
                     patch.object(app_module.time, "time", return_value=100.0), \
                     patch.object(app_module, "_wait_for_lease_release", side_effect=wait), \
                     patch.object(app_module.threading, "Thread") as thread, \
                     patch.object(app_module, "_on_main_thread") as on_main:
                    app._on_add(SimpleNamespace(_provider=provider))
                    cancel.assert_called_once()       # old sign-in cancelled
                    thread.assert_called_once()       # fresh login started (not refused)
                    thread.return_value.start.assert_called_once()
                    assert thread.call_args.kwargs["daemon"] is True
                    assert "Restarting" in app_module.rumps.notification.call_args.kwargs["subtitle"]
                    app_module.rumps.notification.assert_called_once_with(
                        title="Claude Switcher", subtitle=f"Restarting {label} login…", message=entry[4])
                    assert app._signing_in_since == {provider: 100.0}
                    assert events == ["cancel"]
                    thread.call_args.kwargs["target"]()
                    assert events == (["cancel", "wait", "add"] if released else ["cancel", "wait"])
                    if released:
                        add.assert_called_once_with(app.config_path)
                    else:
                        add.assert_not_called()
                    app._rebuild_menu.assert_not_called()
                    app._fetch_all_usage.assert_not_called()
                    on_main.assert_called_once()
                    on_main.call_args.args[0]()
                app_module.rumps.notification.assert_called_with(
                    title="Claude Switcher",
                    subtitle="Cancelled" if released else "Error",
                    message="Login was cancelled or failed." if released else
                        f"The previous {label} sign-in did not stop in time. Try again.")
                app._rebuild_menu.assert_called_once()
                app._fetch_all_usage.assert_called_once()

    def test_add_when_idle_does_not_cancel(self, app_module):
        app = self._app(app_module)
        app.menu = MagicMock()
        app._add_auto_switch_menu = MagicMock()
        # Exercise the real menu builder: both rows must carry their provider
        # and invoke the unified handler with the existing labels and order.
        with patch.object(app_module, "load_accounts", return_value=[]), \
             patch.object(app_module.rumps, "MenuItem", side_effect=lambda title, callback=None:
                          SimpleNamespace(title=title, callback=callback)):
            app_module.ClaudeSwitcherApp._rebuild_menu(app)
        add_items = [c.args[0] for c in app.menu.add.call_args_list
                     if getattr(c.args[0], "callback", None) == app._on_add]
        assert [(item.title, item._provider) for item in add_items] == [
            ("✚  Add Claude account...", "claude"), ("✚  Add Codex account...", "codex")]
        for item, label, window, cli, add_name, cancel_name, success in (
            (add_items[0], "Claude", "browser", "check_claude_cli", "add_new_account", "cancel_login", "Claude account added"),
            (add_items[1], "Codex", "Terminal", "check_codex_cli", "add_new_codex_account", "cancel_codex_login", "Signed in to Codex"),
        ):
            provider = item._provider
            entry = app_module.ADD_PROVIDERS[provider]
            assert entry == (label, getattr(app_module, cli), getattr(app_module, add_name),
                             getattr(app_module, cancel_name),
                             f"Sign in in the {window} window that appears, then come back here.")
            for outcome in ("missing_cli", "success", "cancelled", "error"):
                app._signing_in_since = {}
                app._rebuild_menu.reset_mock(); app._fetch_all_usage.reset_mock()
                app_module.rumps.notification.reset_mock(); app_module.rumps.alert.reset_mock()
                check = MagicMock(return_value=outcome != "missing_cli")
                add = MagicMock(return_value=SimpleNamespace(email="new@test.com", subscription_type="pro")
                                if outcome == "success" else None)
                if outcome == "error":
                    add.side_effect = RuntimeError("login failed")
                cancel = MagicMock()
                with patch.dict(app_module.ADD_PROVIDERS, {provider: (label, check, add, cancel, entry[4])}), \
                     patch.object(app_module, "_add_lease_held", return_value=False), \
                     patch.object(app_module.time, "time", return_value=100.0), \
                     patch.object(app_module, "_wait_for_lease_release") as wait, \
                     patch.object(app_module.threading, "Thread") as thread, \
                     patch.object(app_module, "_on_main_thread") as on_main:
                    item.callback(item)
                    check.assert_called_once_with()
                    cancel.assert_not_called()
                    if outcome == "missing_cli":
                        app_module.rumps.alert.assert_called_once_with(
                            title=f"{label} CLI not found",
                            message=f"Please install {app_module.PROVIDER_LABELS[provider]} before adding an account.")
                        thread.assert_not_called(); add.assert_not_called()
                        app_module.rumps.notification.assert_not_called()
                        assert app._signing_in_since == {}
                        app._rebuild_menu.assert_not_called(); app._fetch_all_usage.assert_not_called()
                        continue
                    app_module.rumps.alert.assert_not_called()
                    thread.assert_called_once()
                    thread.return_value.start.assert_called_once()
                    assert "Opening" in app_module.rumps.notification.call_args.kwargs["subtitle"]
                    app_module.rumps.notification.assert_called_once_with(
                        title="Claude Switcher", subtitle=f"Opening {label} login…", message=entry[4])
                    assert app._signing_in_since == {provider: 100.0}
                    thread.call_args.kwargs["target"]()
                    wait.assert_not_called()
                    add.assert_called_once_with(app.config_path)
                    app._rebuild_menu.assert_not_called(); app._fetch_all_usage.assert_not_called()
                    on_main.assert_called_once()
                    on_main.call_args.args[0]()
                subtitle, message = {
                    "success": (success, "new@test.com (pro)"),
                    "cancelled": ("Cancelled", "Login was cancelled or failed."),
                    "error": ("Error", "login failed"),
                }[outcome]
                app_module.rumps.notification.assert_called_with(
                    title="Claude Switcher", subtitle=subtitle, message=message)
                app._rebuild_menu.assert_called_once(); app._fetch_all_usage.assert_called_once()

    def test_wait_for_lease_release_returns_when_lease_clears(self, app_module):
        held = {"v": True}
        with patch.object(app_module, "_add_lease_held", lambda p: held["v"]), \
             patch.object(app_module.time, "sleep", lambda s: held.__setitem__("v", False)):
            assert app_module._wait_for_lease_release("codex", timeout=5) is True

    def test_wait_for_lease_release_times_out(self, app_module):
        with patch.object(app_module, "_add_lease_held", lambda p: True), \
             patch.object(app_module.time, "sleep", lambda s: None):
            assert app_module._wait_for_lease_release("codex", timeout=0.0) is False

    def test_signing_in_grace_and_lease(self, app_module):
        app = self._app(app_module)
        with patch.object(app_module, "_add_lease_held", lambda p: False):
            assert app._signing_in("codex") is False
            app._signing_in_since["codex"] = app_module.time.time()
            assert app._signing_in("codex") is True
            app._signing_in_since["codex"] = app_module.time.time() - 60
            assert app._signing_in("codex") is False
        with patch.object(app_module, "_add_lease_held", lambda p: p == "claude"):
            assert app._signing_in("claude") is True
