"""The usage timer checks active accounts every 60 s and all accounts every 300 s."""

import importlib
import sys
import types
from collections import Counter
from unittest.mock import MagicMock, patch

import pytest

from claude_switcher.config import AccountInfo, AppSettings, save_accounts, save_settings
from claude_switcher.usage_state import UsageState, UsageWindow


class _FakeApp:
    def __init__(self, *args, **kwargs):
        pass


class _FakeTimer:
    created: list = []

    def __init__(self, callback, interval):
        self.callback, self.interval, self.started = callback, interval, False
        _FakeTimer.created.append(self)

    def start(self):
        self.started = True


class _FakeMenuItem:
    def __init__(self, title, callback=None):
        self.title, self.callback, self.state = title, callback, 0

    def add(self, item):
        pass

    def set_callback(self, callback):
        self.callback = callback


fake_rumps = types.ModuleType("rumps")
fake_rumps.App = _FakeApp
fake_rumps.MenuItem = _FakeMenuItem
fake_rumps.Timer = _FakeTimer
fake_rumps.separator = object()
fake_rumps.notification = MagicMock()
fake_rumps.alert = MagicMock()
fake_rumps.quit_application = MagicMock()

fake_foundation = types.ModuleType("Foundation")
fake_foundation.NSOperationQueue = MagicMock()
fake_foundation.NSBundle = MagicMock()

CLAUDE = ["c-active", "c-1", "c-2", "c-3", "c-room"]
CODEX = ["x-active", "x-1", "x-2", "x-room"]
ALL_ACCOUNTS = [("claude", e) for e in CLAUDE] + [("codex", e) for e in CODEX]
ACTIVES = [("claude", "c-active"), ("codex", "x-active")]


@pytest.fixture
def app_module():
    sys.modules.pop("claude_switcher.app", None)
    with patch.dict(sys.modules, {"rumps": fake_rumps, "Foundation": fake_foundation}):
        module = importlib.import_module("claude_switcher.app")
        yield module
    sys.modules.pop("claude_switcher.app", None)


def _state(percent, *, scoped_fable=None):
    windows = [UsageWindow("5h", 1.0), UsageWindow("7d", percent)]
    if scoped_fable is not None:
        windows.append(UsageWindow("Fable", scoped_fable, scoped=True))
    return UsageState(True, f"7d {percent}%", tuple(windows))


class Harness:
    """A bare app on a real config file, with fetches and switches recorded."""

    def __init__(self, app_module, tmp_path, monkeypatch, threshold=95.0, basis="fable"):
        self.module = app_module
        app = object.__new__(app_module.ClaudeSwitcherApp)
        app.config_path = tmp_path / "accounts.json"
        save_accounts([AccountInfo(email, "pro", "", email.endswith("active"), email, provider=p)
                       for p, email in ALL_ACCOUNTS], app.config_path)
        save_settings(AppSettings(auto_switch={"claude": True, "codex": True},
                                  auto_switch_threshold=threshold, proactive_switch=False,
                                  claude_route_based_on=basis), app.config_path)
        app.menu = MagicMock()
        app._gateway = None
        app._usage_cache = {}
        app._usage_state_cache = {}
        app._reset_cache = {}
        app._reset_items = {}
        app._usage_items = {}
        app._last_auto_switch_attempt = {}
        app._manual_pin = {}
        app._last_auto_reset_attempt = {}
        app._last_auto_reset_by_account = {}
        app._refresh_in_progress = False
        app._switch_in_progress = set()
        app._quick_retries_left = app_module.QUICK_RETRY_BUDGET
        app._quick_retry_timer = None
        app._refresh_requested = False
        app._manual_refresh = False
        app._signing_in_since = {}
        app._usage_ticks = 0
        app._rebuild_menu = MagicMock()
        app._schedule_quick_retry = MagicMock()
        app._has_credentials = lambda account: True
        self.app = app

        # Other accounts: full except the "room" account of each provider.
        for key in ALL_ACCOUNTS:
            self.cached(key, _state(20.0 if key[1].endswith("room") else 100.0))
        self.cached(("claude", "c-active"), _state(50.0))
        self.cached(("codex", "x-active"), _state(50.0))
        self.live = {key: _state(10.0) for key in ALL_ACCOUNTS}

        self.fetched = []
        def fetch(account, active_account):
            key = (account.provider, account.email)
            self.fetched.append(key)
            return self.live[key]
        app._fetch_usage_state = MagicMock(side_effect=fetch)

        self.switch = {p: MagicMock() for p in ("claude", "codex")}
        for provider, fn in self.switch.items():
            monkeypatch.setitem(app_module.PROVIDERS[provider], "switch", fn)
            monkeypatch.setattr(app_module.PROVIDERS[provider]["reset"], "poll",
                                lambda *a, **k: app_module.reset_service.ResetStatus())

        # Worker threads and main-thread blocks are queued, then run on demand.
        self.threads, self.main, self.in_main = [], [], False
        module = app_module
        harness = self
        class _QueuedThread:
            def __init__(self, target, daemon=False):
                self.target = target
            def start(self):
                harness.threads.append(self.target)
        monkeypatch.setattr(module.threading, "Thread", _QueuedThread)
        monkeypatch.setattr(module, "_on_main_thread", self.main.append)
        fake_rumps.notification.reset_mock()

    def cached(self, key, state):
        self.app._usage_state_cache[key] = state
        self.app._usage_cache[key] = state.display

    def run_workers(self):
        while self.threads:
            self.threads.pop(0)()

    def run_main(self):
        while self.main:
            self.in_main = True
            try:
                self.main.pop(0)()
            finally:
                self.in_main = False

    def settle(self):
        while self.threads or self.main:
            self.run_workers()
            self.run_main()

    def tick(self):
        self.app._on_periodic_usage_refresh(None)

    def notices(self):
        return [c.kwargs["subtitle"] for c in fake_rumps.notification.call_args_list]


@pytest.fixture
def harness(app_module, tmp_path, monkeypatch):
    return Harness(app_module, tmp_path, monkeypatch)


def test_usage_timer_ticks_every_60_seconds(app_module, monkeypatch):
    _FakeTimer.created.clear()
    cls = app_module.ClaudeSwitcherApp
    monkeypatch.setattr(cls, "_first_launch", lambda self: None)
    monkeypatch.setattr(cls, "_rebuild_menu", lambda self: None)
    monkeypatch.setattr(cls, "_fetch_all_usage", lambda self: None)
    monkeypatch.setattr(app_module, "load_settings", lambda path: AppSettings())
    app = cls()
    assert [(t.interval, t.started) for t in _FakeTimer.created] == [(60, True)]
    assert _FakeTimer.created[0].callback == app._on_periodic_usage_refresh


def test_60s_tick_fetches_only_the_active_accounts(harness):
    harness.live[("claude", "c-active")] = _state(60.0)
    harness.tick()
    harness.run_workers()
    assert harness.fetched == ACTIVES
    harness.run_main()
    assert harness.app._usage_cache[("claude", "c-active")] == "7d 60.0%"
    assert not harness.threads and not harness.main
    for fn in harness.switch.values():
        fn.assert_not_called()


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("percent, switches", [(95.0, True), (100.0, True), (94.9, False)])
def test_60s_tick_switches_active_account_at_threshold(harness, provider, percent, switches):
    active, room = {"claude": ("c-active", "c-room"), "codex": ("x-active", "x-room")}[provider]
    harness.live[(provider, active)] = _state(percent)
    harness.tick()
    harness.run_workers()
    assert harness.fetched == ACTIVES   # the 60 s check reads no other account
    other = "codex" if provider == "claude" else "claude"
    harness.switch[other].assert_not_called()
    if not switches:
        harness.switch[provider].assert_not_called()
        harness.settle()
        assert harness.fetched == ACTIVES
        assert harness.notices() == []
        return
    harness.switch[provider].assert_called_once_with(room, harness.app.config_path)
    harness.run_main()
    label = harness.module.PROVIDER_LABELS[provider]
    assert harness.notices() == [f"Auto-switched {label}"]
    harness.app._rebuild_menu.assert_called_once()
    # After a switch, the other rows refresh. The fetch log is kept across the
    # follow-up refresh: no account is fetched twice for one tick.
    harness.settle()
    counts = Counter(harness.fetched)
    assert max(counts.values()) == 1, counts
    assert counts == Counter(ALL_ACCOUNTS)
    assert harness.switch[provider].call_count == 1


@pytest.mark.parametrize("basis, switches", [("fable", True), ("weekly", False)])
def test_60s_tick_uses_the_claude_routing_basis(app_module, tmp_path, monkeypatch, basis, switches):
    h = Harness(app_module, tmp_path, monkeypatch, basis=basis)
    h.live[("claude", "c-active")] = _state(50.0, scoped_fable=96.0)
    h.tick()
    h.run_workers()
    assert h.fetched == ACTIVES
    assert h.switch["claude"].called is switches


def test_fifth_tick_refreshes_all_accounts_once_and_no_double_fetch(harness):
    per_tick = []
    for _ in range(10):
        harness.fetched.clear()
        harness.tick()
        harness.settle()
        per_tick.append(Counter(harness.fetched))
    active_only, full = Counter(ACTIVES), Counter(ALL_ACCOUNTS)
    assert per_tick == [active_only] * 4 + [full] + [active_only] * 4 + [full]
    for fn in harness.switch.values():
        fn.assert_not_called()


def test_60s_tick_does_nothing_while_a_refresh_is_in_flight(harness):
    harness.app._refresh_in_progress = True
    harness.tick()
    assert harness.threads == [] and harness.fetched == []
    # The in-flight refresh already covers the active accounts: nothing is queued.
    assert harness.app._refresh_requested is False


def test_full_refresh_requested_during_60s_check_runs_after_it(harness):
    harness.tick()
    harness.app._fetch_all_usage()          # e.g. the Refresh menu item
    assert harness.app._refresh_requested is True
    assert len(harness.threads) == 1        # no second fetch starts in parallel
    harness.run_workers()
    assert harness.fetched == ACTIVES
    harness.fetched.clear()
    harness.settle()
    assert Counter(harness.fetched) == Counter(ALL_ACCOUNTS)
    assert harness.app._refresh_requested is False


def test_failed_60s_fetch_keeps_last_numbers_and_makes_no_switch(harness):
    harness.cached(("claude", "c-active"), _state(97.0))
    harness.live[("claude", "c-active")] = UsageState(False, "Usage unavailable")
    harness.tick()
    harness.settle()
    assert harness.fetched == ACTIVES
    assert harness.app._usage_cache[("claude", "c-active")] == "7d 97.0%"
    harness.switch["claude"].assert_not_called()


def test_60s_tick_skips_a_provider_during_its_sign_in(harness, monkeypatch):
    monkeypatch.setattr(harness.module, "_add_lease_held", lambda provider: provider == "codex")
    harness.tick()
    harness.settle()
    assert harness.fetched == [("claude", "c-active")]


def test_60s_tick_does_not_repeat_the_limit_reached_notice(harness):
    harness.cached(("claude", "c-room"), _state(100.0))
    harness.live[("claude", "c-active")] = _state(99.0)
    harness.tick()
    harness.settle()
    assert harness.fetched == ACTIVES
    harness.switch["claude"].assert_not_called()
    assert harness.notices() == []


@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_minute_switch_reads_no_keychain_on_the_main_thread(harness, monkeypatch, provider):
    active = {"claude": "c-active", "codex": "x-active"}[provider]
    del harness.app._rebuild_menu            # use the real menu rebuild
    monkeypatch.setattr(harness.module.login_item, "is_enabled", lambda: False)
    main_thread_reads = []

    def has_credentials(account):
        if harness.in_main:
            main_thread_reads.append((account.provider, account.email))
        return True

    harness.app._has_credentials = has_credentials
    harness.live[(provider, active)] = _state(99.0)
    harness.tick()
    harness.settle()
    harness.switch[provider].assert_called_once()
    assert main_thread_reads == []
    # The rebuilt menu still shows a usage row for every account with credentials.
    assert set(harness.app._usage_items) == set(ALL_ACCOUNTS)
