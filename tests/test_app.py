"""Focused tests for credential-conflict reporting in the menu app."""

import importlib
import sys
import types
from pathlib import Path
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
fake_foundation.NSBundle = MagicMock()

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
    app._manual_pin = {}
    app._rebuild_menu = MagicMock()
    app._fetch_all_usage = MagicMock()
    return app


def test_codex_add_lease_conflict_is_not_reported_as_switch_success(app_module, tmp_path):
    app = _app_shell(app_module, tmp_path)
    fake_rumps.notification.reset_mock()
    fake_rumps.alert.reset_mock()

    with patch.object(app_module, "get_active_account", return_value=None), patch.dict(
        app_module.PROVIDERS["codex"], {"switch": MagicMock(side_effect=RuntimeError("add in progress"))}
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
                with patch.dict(app_module.PROVIDERS[provider], {"add": entry}), \
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
        app._add_auto_reset_menu = MagicMock()
        app._add_reset_menu = MagicMock()
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
            entry = app_module.PROVIDERS[provider]["add"]
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
                with patch.dict(app_module.PROVIDERS[provider], {"add": (label, check, add, cancel, entry[4])}), \
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


@pytest.mark.parametrize("provider,active_fn,saved_fn,state_fn", [
    ("claude", "fetch_active_usage", "fetch_usage_for_account", "claude_usage_state"),
    ("codex", "fetch_active_codex_usage", "fetch_codex_usage_for_account", "codex_usage_state"),
])
@pytest.mark.parametrize("active_email", [None, "same@test.com", "other@test.com"])
@pytest.mark.parametrize("fails", [False, True])
def test_provider_usage_selects_live_or_saved_credentials(
    app_module, tmp_path, provider, active_fn, saved_fn, state_fn, active_email, fails
):
    app = _app_shell(app_module, tmp_path)
    account = SimpleNamespace(provider=provider, email="same@test.com")
    active = SimpleNamespace(email=active_email) if active_email else None
    raw = object()
    expected = app_module.UsageState(available=True, display="5h 25%")
    entry = app_module.PROVIDERS[provider]
    assert entry["fetch_active_usage"] is getattr(app_module, active_fn)
    assert entry["fetch_usage"] is getattr(app_module, saved_fn)
    assert entry["usage_state"] is getattr(app_module, state_fn)
    live, saved, convert = MagicMock(return_value=raw), MagicMock(return_value=raw), MagicMock(return_value=expected)
    with patch.dict(entry, {"fetch_active_usage": live, "fetch_usage": saved, "usage_state": convert}):
        selected = live if active_email == account.email else saved
        if fails:
            selected.side_effect = RuntimeError("fetch failed")
        state = app._fetch_usage_state(account, active)
    if active_email == account.email:
        live.assert_called_once_with()
        saved.assert_not_called()
    else:
        saved.assert_called_once_with(account.email)
        live.assert_not_called()
    if fails:
        assert state == app_module.UsageState(available=False, display="Usage unavailable")
        convert.assert_not_called()
    else:
        assert state is expected
        convert.assert_called_once_with(raw)


def test_unknown_provider_usage_remains_unavailable(app_module, tmp_path):
    app = _app_shell(app_module, tmp_path)
    state = app._fetch_usage_state(SimpleNamespace(provider="unknown", email="a@test.com"), None)
    assert state == app_module.UsageState(available=False, display="Usage unavailable")


@pytest.mark.parametrize("provider,switch_fn", [
    ("claude", "switch_account"), ("codex", "switch_codex_account"),
])
@pytest.mark.parametrize("automatic", [False, True])
@pytest.mark.parametrize("fails", [False, True])
def test_provider_switch_dispatch_and_result(app_module, tmp_path, provider, switch_fn, automatic, fails):
    app = _app_shell(app_module, tmp_path)
    active = SimpleNamespace(provider=provider, email="active@test.com")
    target = SimpleNamespace(provider=provider, email="target@test.com")
    app._usage_state_cache = {(provider, active.email): app_module.UsageState(available=True, display="100%")}
    app._last_auto_switch_attempt = {}
    settings = SimpleNamespace(auto_switch={provider: True}, auto_switch_threshold=95)
    fake_rumps.notification.reset_mock()
    fake_rumps.alert.reset_mock()
    assert app_module.PROVIDERS[provider]["switch"] is getattr(app_module, switch_fn)
    switch = MagicMock(side_effect=RuntimeError("switch failed") if fails else None)
    with patch.dict(app_module.PROVIDERS[provider], {"switch": switch}), \
         patch.object(app_module, "get_active_account", return_value=active), \
         patch.object(app_module, "load_settings", return_value=settings), \
         patch.object(app_module, "load_accounts", return_value=[active, target]), \
         patch.object(app, "_has_credentials", return_value=True), \
         patch.object(app_module, "should_auto_switch", return_value=True), \
         patch.object(app_module, "choose_auto_switch_target", return_value=target), \
         patch.object(app_module.threading, "Thread", ImmediateThread), \
         patch.object(app_module, "_on_main_thread", side_effect=lambda fn: fn()):
        if automatic:
            result = app._attempt_auto_switch(provider)
            if not fails:
                assert result.pop("reason") == "exhausted"
            assert result == ({"status": "error", "provider": provider, "email": active.email,
                               "message": "switch failed"} if fails else
                              {"status": "switched", "provider": provider, "email": target.email})
        else:
            app._switch_account(provider, target.email)
            assert app._switch_in_progress == set()
            app._rebuild_menu.assert_called_once_with()
            app._fetch_all_usage.assert_called_once_with()
            if fails:
                fake_rumps.alert.assert_called_once_with(title="Error", message="switch failed")
                fake_rumps.notification.assert_not_called()
            else:
                fake_rumps.alert.assert_not_called()
                fake_rumps.notification.assert_called_once_with(
                    title="Claude Switcher",
                    subtitle=f"{app_module.PROVIDER_LABELS[provider]} account switched", message=target.email)
    switch.assert_called_once_with(target.email, app.config_path)


def test_provider_lease_reads_current_flags_independently(app_module):
    from claude_switcher import core, codex_core
    for claude_held, codex_held in [(False, True), (True, False), (False, False)]:
        with patch.object(core, "_add_in_progress", claude_held), \
             patch.object(codex_core, "_add_in_progress", codex_held):
            assert app_module._add_lease_held("claude") is claude_held
            assert app_module._add_lease_held("codex") is codex_held


def test_provider_menu_preserves_groups_credentials_and_click_routing(app_module, tmp_path):
    app = _app_shell(app_module, tmp_path)
    app.menu = MagicMock()
    app._usage_cache = {}
    app._usage_state_cache = {("codex", "same@test.com"):
                             app_module.UsageState(available=False, display="Login required")}
    app._add_auto_switch_menu = MagicMock()
    app._switch_account = MagicMock()
    app._on_add = MagicMock()
    accounts = [SimpleNamespace(provider=p, email="same@test.com", active=False, subscription_type="pro")
                for p in ("codex", "claude")]
    def menu_item(title, callback=None):
        return SimpleNamespace(title=title, callback=callback, add=MagicMock(), set_callback=MagicMock())
    with patch.object(app_module, "load_accounts", return_value=accounts), \
         patch.object(app_module.keychain, "read_credentials", return_value="credentials") as read, \
         patch.object(app_module.rumps, "MenuItem", side_effect=menu_item):
        app_module.ClaudeSwitcherApp._rebuild_menu(app)
    assert [call.args[0] for call in read.call_args_list] == [
        "claude-switcher:same@test.com", "codex-switcher:same@test.com"]
    items = [call.args[0] for call in app.menu.add.call_args_list]
    assert [getattr(item, "title", None) for item in items[:7]] == [
        "── Claude Code ──", "○  same@test.com (pro)", "       │  •••", None,
        "── Codex CLI ──", "○  same@test.com (pro)", "       │  •••"]
    for item in (items[1], items[5]):
        item.callback(item)
    app._switch_account.assert_called_once_with("claude", "same@test.com")
    app._on_add.assert_called_once_with(items[5])
    remove = next(item for item in items if getattr(item, "title", None) == "−  Remove account")
    assert [(call.args[0].title, call.args[0]._provider) for call in remove.add.call_args_list] == [
        ("[Codex] same@test.com", "codex"), ("[Claude] same@test.com", "claude")]


def test_claude_removal_none_return_is_success(app_module, tmp_path):
    app = _app_shell(app_module, tmp_path)
    fake_rumps.notification.reset_mock()
    fake_rumps.alert.reset_mock()
    with patch.object(app_module, "get_active_account", return_value=None), \
         patch.object(app_module, "remove_saved_account", return_value=None) as remove:
        app._on_remove_account(SimpleNamespace(_provider="claude", _email="idle@test.com"))
    remove.assert_called_once_with("idle@test.com", app.config_path)
    fake_rumps.alert.assert_not_called()
    fake_rumps.notification.assert_called_once_with(
        title="Claude Switcher", subtitle="Claude Code account removed", message="idle@test.com")
    app._rebuild_menu.assert_called_once_with()
    app._fetch_all_usage.assert_called_once_with()


class ResetMenuItem:
    def __init__(self, title, callback=None):
        self.title, self.callback = title, callback
        self.children = []
        self.state = 0

    def add(self, item):
        self.children.append(item)

    def set_callback(self, callback):
        self.callback = callback


def _reset_app(app_module, tmp_path):
    app = _app_shell(app_module, tmp_path)
    app.menu = MagicMock()
    app._usage_state_cache = {}
    app._usage_cache = {}
    app._usage_items = {}
    app._last_auto_switch_attempt = {}
    app._last_auto_reset_attempt = {}
    app._last_auto_reset_by_account = {}
    app._refresh_in_progress = False
    app._refresh_requested = False
    app._manual_refresh = False
    app._quick_retries_left = app_module.QUICK_RETRY_BUDGET
    app._schedule_quick_retry = MagicMock()
    app_module.rumps.notification.reset_mock()
    app_module.rumps.alert.reset_mock()
    return app


def test_reset_menus_and_eligibility(app_module, tmp_path):
    from claude_switcher.config import AccountInfo, save_accounts, set_auto_reset_enabled
    from claude_switcher.usage_state import UsageState
    app = _reset_app(app_module, tmp_path)
    accounts = [AccountInfo(email, "pro", "", False, email, provider=p) for p, email in
                [("codex", "eligible"), ("codex", "ineligible"), ("codex", "unknown"), ("claude", "claude")]]
    save_accounts(accounts, app.config_path)
    set_auto_reset_enabled("codex", True, app.config_path)
    app._usage_state_cache = {
        ("codex", "eligible"): UsageState(True, "100%", reset_credits=3, reset_applicable=2),
        ("codex", "ineligible"): UsageState(True, "10%", reset_credits=4),
        ("claude", "claude"): UsageState(True, "100%", reset_credits=5, reset_applicable=5),
    }
    with patch.object(app_module.rumps, "MenuItem", ResetMenuItem), \
         patch.object(app, "_has_credentials", return_value=False):
        app_module.ClaudeSwitcherApp._rebuild_menu(app)
        items = [c.args[0] for c in app.menu.add.call_args_list]
        titles = [getattr(i, "title", None) for i in items]
        auto = items[titles.index("Auto-switch") + 1]
        assert auto.title == "Auto-reset"
        assert [(i.title, i.state) for i in auto.children] == [("Codex CLI", 1)]
        reset = items[titles.index("−  Remove account") + 1]
        assert reset.title == "↺ Reset Codex usage"
        assert [i.title for i in reset.children] == ["eligible (3 available)"]
        app._usage_state_cache = {}
        app.menu.reset_mock()
        app_module.ClaudeSwitcherApp._rebuild_menu(app)
        reset = next(c.args[0] for c in app.menu.add.call_args_list if getattr(c.args[0], "title", None) == "↺ Reset Codex usage")
        assert len(reset.children) == 1
        assert reset.children[0].title == "No reset applicable now"
        assert reset.children[0].callback is None


@pytest.mark.parametrize("initial", [False, True])
def test_toggle_auto_reset(app_module, tmp_path, initial):
    from claude_switcher.config import set_auto_reset_enabled, is_auto_reset_enabled
    app = _reset_app(app_module, tmp_path)
    set_auto_reset_enabled("codex", initial, app.config_path)
    app._on_toggle_auto_reset(SimpleNamespace(_provider="codex"))
    assert is_auto_reset_enabled("codex", app.config_path) is not initial
    app._rebuild_menu.assert_called_once()
    app_module.rumps.notification.assert_called_once_with(title="Claude Switcher", subtitle="Auto-reset Codex CLI",
                                                        message="Disabled" if initial else "Enabled")


@pytest.mark.parametrize("confirmed", [0, 1])
@pytest.mark.parametrize("outcome,subtitle", [("reset", "Reset applied"), ("nothing_to_reset", "Nothing to reset"),
    ("no_credit", "No reset credit available"), ("already_redeemed", "Already redeemed"), (RuntimeError("failed"), "Error")])
def test_manual_reset_confirmation_and_main_thread_completion(app_module, tmp_path, confirmed, outcome, subtitle):
    app = _reset_app(app_module, tmp_path)
    app._usage_state_cache[("codex", "user@test.com")] = app_module.UsageState(True, "100%", reset_credits=3, reset_applicable=2)
    sender = SimpleNamespace(_email="user@test.com")
    with patch.object(app_module.rumps, "alert", return_value=confirmed) as alert, \
         patch.object(app_module, "consume_reset_credit", side_effect=outcome if isinstance(outcome, Exception) else None,
                      return_value=outcome) as consume, \
         patch.object(app_module.threading, "Thread") as thread, \
         patch.object(app_module, "_on_main_thread") as main:
        app._on_reset_codex_usage(sender)
        alert.assert_called_once_with(title="Use a rate limit reset?", message="Use 1 of 3 banked resets for user@test.com? This resets that account's Codex 5-hour and weekly windows and cannot be undone.", ok="Reset", cancel="Cancel")
        consume.assert_not_called()
        if not confirmed:
            thread.assert_not_called()
            app._fetch_all_usage.assert_not_called()
            return
        assert thread.call_args.kwargs["daemon"] is True
        thread.call_args.kwargs["target"]()
        consume.assert_called_once_with(sender._email, app.config_path)
        app_module.rumps.notification.assert_not_called()
        app._fetch_all_usage.assert_not_called()
        main.call_args.args[0]()
    app_module.rumps.notification.assert_called_once()
    notification = app_module.rumps.notification.call_args.kwargs
    assert notification["subtitle"] == subtitle
    assert notification["title"] == ("Error" if isinstance(outcome, Exception) else "Claude Switcher")
    assert notification["message"] == ("failed" if isinstance(outcome, Exception) else
                                       "user@test.com: windows reset" if outcome == "reset" else sender._email)
    app._fetch_all_usage.assert_called_once()


@pytest.mark.parametrize("enabled,percent,target_state,provider,expected", [
    (True, 100, None, "codex", True), (False, 100, None, "codex", False),
    (True, 99, None, "codex", False), (True, 100, 10, "codex", False),
    (True, 100, "unknown", "codex", False), (True, 100, None, "claude", False),
])
def test_auto_reset_acceptance_conditions(app_module, tmp_path, enabled, percent, target_state, provider, expected):
    from claude_switcher.config import AccountInfo, AppSettings, save_accounts, save_settings
    from claude_switcher.usage_state import UsageState, UsageWindow
    app = _reset_app(app_module, tmp_path)
    active = AccountInfo("active", "plus", "", True, "active", provider=provider)
    accounts = [active]
    app._usage_state_cache[(provider, "active")] = UsageState(True, "usage", (UsageWindow("5h", percent),), 3, 2)
    if target_state is not None:
        accounts.append(AccountInfo("other", "plus", "", False, "other", provider=provider))
        app._usage_state_cache[(provider, "other")] = UsageState(False, "unknown") if target_state == "unknown" else UsageState(True, "usage", (UsageWindow("5h", target_state),))
    save_accounts(accounts, app.config_path)
    save_settings(AppSettings(auto_reset={provider: enabled}), app.config_path)
    with patch.object(app, "_has_credentials", return_value=True), \
         patch.object(app_module, "consume_reset_credit", return_value="reset") as consume, \
         patch.dict(app_module.PROVIDERS[provider], {"switch": MagicMock()}) as providers:
        result = app._attempt_auto_reset(provider)
        assert bool(result) is expected
        if expected:
            consume.assert_called_once_with("active", app.config_path)
        else:
            consume.assert_not_called()
        providers["switch"].assert_not_called()


@pytest.mark.parametrize("outcome", ["reset", "nothing_to_reset", "no_credit", "already_redeemed", RuntimeError("failed")])
def test_auto_reset_cooldowns_and_other_account(app_module, tmp_path, outcome):
    from claude_switcher.config import AccountInfo, AppSettings, save_accounts, save_settings
    from claude_switcher.usage_state import UsageState, UsageWindow
    app = _reset_app(app_module, tmp_path)
    accounts = [AccountInfo(e, "plus", "", e == "active", e, provider="codex") for e in ("active", "other")]
    save_accounts(accounts, app.config_path)
    save_settings(AppSettings(auto_reset={"codex": True}), app.config_path)
    app._usage_state_cache = {("codex", "active"): UsageState(True, "100%", (UsageWindow("5h", 100),)),
                             ("codex", "other"): UsageState(True, "100%", (UsageWindow("5h", 100),), 3, 2)}
    with patch.object(app, "_has_credentials", return_value=True), \
         patch.object(app_module.time, "time", return_value=10000) as now, \
         patch.object(app_module, "consume_reset_credit", return_value=outcome,
                      side_effect=outcome if isinstance(outcome, Exception) else None) as consume:
        assert app._attempt_auto_reset("codex") is not None
        consume.assert_called_once_with("other", app.config_path)
        now.return_value = 10059
        assert app._attempt_auto_reset("codex") is None
        now.return_value = 13599
        assert app._attempt_auto_reset("codex") is None
        assert consume.call_count == 1
        now.return_value = 13600
        assert app._attempt_auto_reset("codex") is not None
        assert consume.call_count == 2
        # Provider cooldown still applies when a different account gains a credit.
        app._usage_state_cache[("codex", "active")] = app._usage_state_cache[("codex", "other")]
        now.return_value = 13659
        assert app._attempt_auto_reset("codex") is None
        now.return_value = 13660
        assert app._attempt_auto_reset("codex") is not None
        assert consume.call_args.args[0] == "active"


@pytest.mark.parametrize("outcome,subtitle", [("reset", "Auto-reset applied"), ("nothing_to_reset", "Nothing to reset"),
    ("no_credit", "No reset credit available"), ("already_redeemed", "Already redeemed"), (RuntimeError("failed"), "Error")])
def test_refresh_runs_auto_reset_after_switch_and_refreshes_on_main(app_module, tmp_path, outcome, subtitle):
    from claude_switcher.config import AccountInfo, AppSettings, save_accounts, save_settings
    from claude_switcher.usage_state import UsageState, UsageWindow
    app = _reset_app(app_module, tmp_path)
    account = AccountInfo("active", "plus", "", True, "active", provider="codex")
    save_accounts([account], app.config_path)
    save_settings(AppSettings(auto_reset={"codex": True}), app.config_path)
    state = UsageState(True, "100%", (UsageWindow("5h", 100),), 3, 2)
    events = []
    app._attempt_auto_switch = MagicMock(side_effect=lambda p: events.append(p))

    def consume(*args):
        events.append("reset")
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    with patch.object(app, "_fetch_usage_state", return_value=state), \
         patch.object(app_module, "consume_reset_credit", side_effect=consume), \
         patch.object(app_module.threading, "Thread", ImmediateThread), \
         patch.object(app_module, "_on_main_thread") as main:
        app_module.ClaudeSwitcherApp._fetch_all_usage(app)
        assert events == ["claude", "codex", "reset"]
        app_module.rumps.notification.assert_not_called()
        app._fetch_all_usage.assert_not_called()
        main.call_args.args[0]()
    app_module.rumps.notification.assert_called_once()
    assert app_module.rumps.notification.call_args.kwargs["subtitle"] == subtitle
    if outcome == "reset":
        assert app_module.rumps.notification.call_args.kwargs["message"] == "active: windows reset (2 left)"
    app._fetch_all_usage.assert_called_once()
    app._rebuild_menu.assert_called_once()


class TestResetMenuRebuildPolicy:
    """The menu is rebuilt only when its structure changes, not on every refresh."""

    def _app(self, app_module):
        app = app_module.ClaudeSwitcherApp.__new__(app_module.ClaudeSwitcherApp)
        app._usage_state_cache = {}
        app._last_reset_eligible = frozenset()
        return app

    def test_eligible_set_reads_only_codex_applicable(self, app_module):
        from claude_switcher.usage_state import UsageState
        app = self._app(app_module)
        app._usage_state_cache = {
            ("codex", "a@t"): UsageState(True, "x", reset_credits=3, reset_applicable=2),
            ("codex", "b@t"): UsageState(True, "x", reset_credits=3, reset_applicable=0),
            ("claude", "c@t"): UsageState(True, "x", reset_credits=9, reset_applicable=9),
            ("codex", "d@t"): None,
        }
        assert app._reset_eligible_emails() == frozenset({"a@t"})

    def test_consume_wrapper_reports_any_exception(self, app_module, monkeypatch):
        app = self._app(app_module)
        app.config_path = "unused"
        monkeypatch.setattr(app_module, "consume_reset_credit", lambda *a, **k: (_ for _ in ()).throw(OSError("keychain down")))
        result = app._consume_reset("a@t", 2)
        assert result["code"] == "error" and "keychain down" in result["message"]


@pytest.fixture(params=["claude", "codex"])
def fefo_app(app_module, tmp_path, request, monkeypatch):
    from claude_switcher.config import AccountInfo, save_accounts, set_auto_switch_enabled
    from claude_switcher.usage_state import UsageState, UsageWindow
    provider = request.param
    app = _reset_app(app_module, tmp_path)
    accounts = [AccountInfo(n, "pro", "", n == "active", n, provider=provider)
                for n in ("active", "later", "earliest", "unknown")]
    save_accounts(accounts, app.config_path)
    set_auto_switch_enabled(provider, True, app.config_path)
    app._usage_state_cache = {
        (provider, a.email): UsageState(True, "", (UsageWindow("7d", 20, resets_at=reset),))
        for a, reset in zip(accounts, (300 * 3600, 200 * 3600, 100 * 3600))
    }
    app._has_credentials = lambda a: True
    switch = MagicMock()
    monkeypatch.setitem(app_module.PROVIDERS[provider], "switch", switch)
    monkeypatch.setattr(app_module.time, "time", lambda: 1000)
    return app, provider, switch


def test_proactive_switch_chooses_earliest(fefo_app):
    app, provider, switch = fefo_app
    assert app._attempt_auto_switch(provider) == {
        "status": "switched", "reason": "proactive", "provider": provider, "email": "earliest"}
    switch.assert_called_once_with("earliest", app.config_path)


@pytest.mark.parametrize("guard", ["off", "pin", "active-best", "cooldown", "disabled", "missing", "unavailable", "no-best"])
def test_proactive_switch_guards(fefo_app, guard):
    from claude_switcher.config import set_proactive_switch_enabled, set_auto_switch_enabled
    from claude_switcher.usage_state import UsageState, UsageWindow
    app, provider, switch = fefo_app
    if guard == "off":
        set_proactive_switch_enabled(False, app.config_path)
    elif guard == "pin":
        app._manual_pin[provider] = "active"
    elif guard == "active-best":
        app._usage_state_cache[(provider, "active")] = UsageState(True, "", (UsageWindow("7d", 20, resets_at=50 * 3600),))
    elif guard == "cooldown":
        app._last_auto_switch_attempt[provider] = 950
    elif guard == "disabled":
        set_auto_switch_enabled(provider, False, app.config_path)
    elif guard == "missing":
        del app._usage_state_cache[(provider, "active")]
    elif guard == "unavailable":
        app._usage_state_cache[(provider, "active")] = UsageState(False, "")
    else:
        app._has_credentials = lambda a: False
    assert app._attempt_auto_switch(provider) is None
    switch.assert_not_called()


@pytest.mark.parametrize("fallback", [False, True])
def test_exhausted_switch_uses_best_or_unknown_fallback(fefo_app, fallback):
    from claude_switcher.config import set_proactive_switch_enabled
    from claude_switcher.usage_state import UsageState, UsageWindow
    app, provider, switch = fefo_app
    set_proactive_switch_enabled(False, app.config_path)
    app._manual_pin[provider] = "active"
    full = UsageState(True, "", (UsageWindow("7d", 100, resets_at=50 * 3600),))
    app._usage_state_cache[(provider, "active")] = full
    if fallback:
        app._usage_state_cache = {key: full for key in app._usage_state_cache}
    target = "unknown" if fallback else "earliest"
    assert app._attempt_auto_switch(provider) == {
        "status": "switched", "reason": "exhausted", "provider": provider, "email": target}
    switch.assert_called_once_with(target, app.config_path)
    assert app._attempt_auto_switch(provider) is None
    assert switch.call_count == 1


def test_old_manual_pin_does_not_block_proactive_switch(fefo_app):
    app, provider, switch = fefo_app
    app._manual_pin[provider] = "later"
    assert app._attempt_auto_switch(provider)["reason"] == "proactive"
    switch.assert_called_once_with("earliest", app.config_path)


def test_manual_switch_sets_pin_before_starting(fefo_app, app_module):
    app, provider, switch = fefo_app
    def check_pin():
        assert app._manual_pin[provider] == "earliest"

    with patch.object(app_module.threading, "Thread") as thread:
        thread.return_value.start.side_effect = check_pin
        app._switch_account(provider, "earliest")
    assert app._manual_pin == {provider: "earliest"}
    thread.return_value.start.assert_called_once_with()


@pytest.mark.parametrize("initial", [True, False])
def test_proactive_menu_toggle_persists(app_module, tmp_path, initial):
    from claude_switcher.config import load_settings, save_settings, AppSettings
    app = _reset_app(app_module, tmp_path)
    save_settings(AppSettings(proactive_switch=initial), app.config_path)
    with patch.object(app_module.rumps, "MenuItem", ResetMenuItem):
        app._add_auto_switch_menu()
    menu = app.menu.add.call_args.args[0]
    assert [i.title for i in menu.children[:2]] == ["Claude Code", "Codex CLI"]
    assert menu.children[2] is app_module.rumps.separator
    item = menu.children[3]
    assert item.title == "Use expiring quota first"
    assert item.state == int(initial)
    item.callback(item)
    assert load_settings(app.config_path).proactive_switch is not initial
    assert item.state == int(not initial)
    app_module.rumps.notification.assert_called_once_with(
        title="Claude Switcher",
        subtitle="Proactive switching disabled" if initial else "Proactive switching enabled",
        message="Only switch when the active account runs out." if initial else
                "Switch to the account whose quota expires soonest, before the active one runs out.")


@pytest.mark.parametrize("reason", ["proactive", "exhausted"])
def test_auto_switch_notification_reason(app_module, tmp_path, reason):
    app = _reset_app(app_module, tmp_path)
    app._notify_auto_switch_result({"status": "switched", "reason": reason, "provider": "claude", "email": "target"})
    app_module.rumps.notification.assert_called_once_with(
        title="Claude Switcher",
        subtitle="Auto-switched Claude Code early" if reason == "proactive" else "Auto-switched Claude Code",
        message="target: its quota expires sooner" if reason == "proactive" else "target")


@pytest.mark.parametrize("enabled", [False, True])
def test_start_at_login_menu_position_and_checkmark(app_module, tmp_path, enabled):
    app = _reset_app(app_module, tmp_path)
    with patch.object(app_module.rumps, "MenuItem", ResetMenuItem), \
         patch.object(app_module.login_item, "is_enabled", return_value=enabled):
        app_module.ClaudeSwitcherApp._rebuild_menu(app)
    items = [call.args[0] for call in app.menu.add.call_args_list]
    assert items[-3] is app_module.rumps.separator
    assert items[-2].title == "Start at login"
    assert items[-2].state == int(enabled)
    assert items[-2].callback == app._on_toggle_start_at_login
    assert items[-1].title == "⏻  Quit"


def test_start_at_login_from_source_notifies_without_toggling(app_module, tmp_path):
    app = _app_shell(app_module, tmp_path)
    sender = SimpleNamespace(state=0)
    fake_rumps.notification.reset_mock()
    with patch.object(app_module, "NSBundle") as bundle, \
         patch.object(app_module, "login_item") as login_item:
        bundle.mainBundle.return_value.bundlePath.return_value = "/usr/local/bin/python3"
        app._on_toggle_start_at_login(sender)
    assert login_item.mock_calls == []
    assert sender.state == 0
    fake_rumps.notification.assert_called_once_with(
        title="Claude Switcher",
        subtitle="Start at login needs the built app",
        message="Run the app from /Applications (build with ./build_local.sh --install).",
    )


@pytest.mark.parametrize("enabled", [False, True])
def test_start_at_login_toggle_from_bundle(app_module, tmp_path, enabled):
    app = _app_shell(app_module, tmp_path)
    sender = SimpleNamespace(state=int(enabled))
    path = "/Applications/Claude Switcher.app"
    fake_rumps.notification.reset_mock()
    with patch.object(app_module, "NSBundle") as bundle, \
         patch.object(app_module, "login_item") as login_item:
        bundle.mainBundle.return_value.bundlePath.return_value = path
        login_item.is_enabled.return_value = enabled
        app._on_toggle_start_at_login(sender)
    if enabled:
        login_item.disable.assert_called_once_with()
        login_item.enable.assert_not_called()
    else:
        login_item.enable.assert_called_once_with(Path(path))
        login_item.disable.assert_not_called()
    assert sender.state == int(not enabled)
    fake_rumps.notification.assert_called_once_with(
        title="Claude Switcher",
        subtitle="Start at login disabled" if enabled else "Start at login enabled",
        message="Claude Switcher will not open at your next login." if enabled else f"Claude Switcher will open at your next login. ({path})",
    )


@pytest.mark.parametrize("enabled", [False, True])
def test_start_at_login_failure_notifies(app_module, tmp_path, enabled):
    app = _app_shell(app_module, tmp_path)
    sender = SimpleNamespace(state=int(enabled))
    fake_rumps.notification.reset_mock()
    with patch.object(app_module, "NSBundle") as bundle, \
         patch.object(app_module, "login_item") as login_item:
        bundle.mainBundle.return_value.bundlePath.return_value = "/Applications/Claude Switcher.app"
        login_item.is_enabled.return_value = enabled
        login_item.enable.side_effect = RuntimeError("Permission denied")
        login_item.disable.side_effect = RuntimeError("Permission denied")
        app._on_toggle_start_at_login(sender)
    assert sender.state == int(enabled)
    fake_rumps.notification.assert_called_once_with(
        title="Claude Switcher", subtitle="Start at login failed", message="Permission denied",
    )
