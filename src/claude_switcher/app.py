"""macOS menu bar application using rumps."""

import threading
import time
from pathlib import Path

import rumps
from Foundation import NSBundle, NSOperationQueue

from claude_switcher import codex_core, core, keychain, login_item
from claude_switcher.auto_switch import (
    account_key,
    choose_auto_switch_target,
    choose_fefo_target,
    should_auto_switch,
    choose_auto_reset_target,
    should_auto_reset,
)
from claude_switcher.codex_core import (
    check_codex_cli,
    cancel_codex_login,
    import_current_codex_account,
    switch_codex_account,
    add_new_codex_account,
    remove_codex_account,
)
from claude_switcher.codex_usage import (
    fetch_active_codex_usage,
    fetch_codex_usage_for_account,
    codex_usage_state,
    consume_reset_credit,
)
from claude_switcher.config import (
    load_accounts,
    get_active_account,
    load_settings,
    set_auto_switch_enabled,
    set_proactive_switch_enabled,
    set_auto_reset_enabled,
    DEFAULT_CONFIG_PATH,
)
from claude_switcher.core import (
    check_claude_cli,
    cancel_login,
    import_current_account,
    switch_account,
    add_new_account,
    remove_saved_account,
)
from claude_switcher.usage import fetch_usage_for_account, fetch_active_usage, claude_usage_state
from claude_switcher.usage_state import UsageState


PROVIDER_LABELS = {
    "claude": "Claude Code",
    "codex": "Codex CLI",
}
# Provider-specific Add inputs and app dispatch/display values.
# add: (label, check_cli, add_fn, cancel_fn, login_instruction).
PROVIDERS = {
    "claude": {
        "add": (
            "Claude", check_claude_cli, add_new_account, cancel_login,
            "Sign in in the browser window that appears, then come back here.",
        ),
        "add_success": "Claude account added",
        "core": core,
        "credential_prefix": "claude-switcher:",
        "account_click": "_on_claude_account_click",
        "switch": switch_account,
        "fetch_active_usage": fetch_active_usage,
        "fetch_usage": fetch_usage_for_account,
        "usage_state": claude_usage_state,
    },
    "codex": {
        "add": (
            "Codex", check_codex_cli, add_new_codex_account, cancel_codex_login,
            "Sign in in the Terminal window that appears, then come back here.",
        ),
        "add_success": "Signed in to Codex",
        "core": codex_core,
        "credential_prefix": "codex-switcher:",
        "account_click": "_on_codex_account_click",
        "switch": switch_codex_account,
        "fetch_active_usage": fetch_active_codex_usage,
        "fetch_usage": fetch_codex_usage_for_account,
        "usage_state": codex_usage_state,
    },
}
AUTO_SWITCH_COOLDOWN_SECONDS = 60
AUTO_RESET_ACCOUNT_COOLDOWN_SECONDS = 3600
RESET_MESSAGES = {
    "reset": "Reset applied",
    "nothing_to_reset": "Nothing to reset",
    "no_credit": "No reset credit available",
    "already_redeemed": "Already redeemed",
}

# When a usage refresh comes back unavailable (e.g. the first fetch races a macOS
# Keychain-access prompt, which blocks `security` past its timeout), retry a few
# times on a short delay instead of leaving stale text until the 300s timer.
QUICK_RETRY_BUDGET = 3
QUICK_RETRY_DELAY_SECONDS = 6


def _plan_quick_retry(states, retries_left):
    """Decide whether to schedule a quick usage retry.

    Returns (should_retry, new_retries_left). Retry while any usage window is
    unavailable and budget remains; refill the budget once everything is
    available so a later transient failure gets a fresh set of quick retries.
    A missing state (None) counts as unavailable.
    """
    any_unavailable = any(state is None or not state.available for state in states)
    if not any_unavailable:
        return False, QUICK_RETRY_BUDGET
    if retries_left <= 0:
        return False, 0
    return True, retries_left - 1


def _add_lease_held(provider: str) -> bool:
    """True while an interactive sign-in for this provider is in progress.

    During a sign-in the provider's live credential is cleared and its
    persistence is locked, so any usage fetch can only come back "unavailable".
    Callers use this to keep the last known numbers on screen rather than
    painting rows "Checking…" for the whole login window (up to 5 minutes).
    """
    entry = PROVIDERS.get(provider, PROVIDERS["codex"])
    return bool(entry["core"]._add_in_progress)


def _wait_for_lease_release(provider: str, timeout: float = 15.0) -> bool:
    """After cancelling a sign-in, wait for its add flow to restore the snapshot
    and release the lease, so a fresh add can take it. Polls; never blocks the UI
    (callers run this on the add's background thread)."""
    deadline = time.monotonic() + timeout
    while _add_lease_held(provider):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.2)
    return True


def _on_main_thread(fn):
    """Schedule fn() to run on the main thread via Cocoa's operation queue."""
    NSOperationQueue.mainQueue().addOperationWithBlock_(fn)


class ClaudeSwitcherApp(rumps.App):
    def __init__(self):
        icon_path = Path(__file__).parent / "resources" / "icon.png"
        super().__init__("", icon=str(icon_path), template=True, quit_button=None)
        self.config_path = DEFAULT_CONFIG_PATH
        self._usage_cache: dict[tuple[str, str], str] = {}
        self._usage_state_cache: dict[tuple[str, str], UsageState] = {}
        self._usage_items: dict[tuple[str, str], rumps.MenuItem] = {}
        self._last_auto_switch_attempt: dict[str, float] = {}
        self._manual_pin: dict[str, str] = {}
        self._last_reset_eligible: frozenset[str] = frozenset()
        self._last_auto_reset_attempt: dict[str, float] = {}
        self._last_auto_reset_by_account: dict[str, float] = {}
        self._refresh_in_progress = False
        self._switch_in_progress: set[str] = set()
        self._quick_retries_left = QUICK_RETRY_BUDGET
        self._quick_retry_timer: threading.Timer | None = None
        self._refresh_requested = False   # a Refresh click landed mid-flight; honour it after
        self._manual_refresh = False      # current refresh is user-initiated -> notify when done
        self._signing_in_since: dict[str, float] = {}  # provider -> time.time() Add was clicked
        self._first_launch()
        self._rebuild_menu()
        self._fetch_all_usage()
        self._auto_switch_timer = rumps.Timer(self._on_periodic_usage_refresh, 300)
        self._auto_switch_timer.start()

    def _first_launch(self):
        """Import existing Claude and Codex accounts on first launch."""
        if self.config_path.exists():
            return

        imported_any = False
        claude_available = check_claude_cli()
        codex_available = check_codex_cli()

        # Claude import failures propagate; only Codex import failures are skipped below.
        if claude_available:
            imported = import_current_account(self.config_path)
            if imported:
                imported_any = True
                rumps.notification(
                    title="Claude Switcher",
                    subtitle="Claude account imported",
                    message=f"{imported.email} ({imported.subscription_type})",
                )

        # Codex import can reject its auth storage; notify and continue first launch.
        if codex_available:
            try:
                imported = import_current_codex_account(self.config_path)
            except Exception as exc:
                imported = None
                rumps.notification(
                    title="Claude Switcher",
                    subtitle="Codex import skipped",
                    message=str(exc),
                )
            if imported:
                imported_any = True
                rumps.notification(
                    title="Claude Switcher",
                    subtitle="Codex account imported",
                    message=f"{imported.email} ({imported.subscription_type})",
                )

        if not imported_any and not claude_available and not codex_available:
            rumps.alert(
                title="CLI not found",
                message="Please install Claude Code or Codex CLI before using Claude Switcher.",
            )

    def _reset_eligible_emails(self) -> frozenset[str]:
        """Codex accounts that can apply a rate-limit reset now, per cached usage."""
        cache = getattr(self, "_usage_state_cache", {}) or {}
        return frozenset(
            email for (provider, email), state in cache.items()
            if provider == "codex" and state is not None and state.reset_applicable > 0
        )

    def _rebuild_menu(self):
        """Rebuild the menu from current account state."""
        self._last_reset_eligible = self._reset_eligible_emails()
        accounts = load_accounts(self.config_path)
        self.menu.clear()
        self._usage_items = {}

        section_added = False
        for provider in PROVIDERS:
            # Keep each provider's accounts in its own section, even for shared emails.
            provider_accounts = [a for a in accounts if a.provider == provider]
            if provider_accounts:
                if section_added:
                    self.menu.add(rumps.separator)
                self._add_provider_section(provider, provider_accounts)
                section_added = True

        self.menu.add(rumps.separator)
        self._add_auto_switch_menu()
        self._add_auto_reset_menu()
        for provider, entry in PROVIDERS.items():
            label = entry["add"][0]
            item = rumps.MenuItem(f"\u271A  Add {label} account...", callback=self._on_add)
            item._provider = provider
            self.menu.add(item)
        self.menu.add(rumps.MenuItem("\u21BB  Refresh usage", callback=self._on_refresh_usage))

        if accounts:
            remove_menu = rumps.MenuItem("\u2212  Remove account")
            for account in accounts:
                provider_label = PROVIDERS.get(account.provider, PROVIDERS["codex"])["add"][0]
                item = rumps.MenuItem(f"[{provider_label}] {account.email}", callback=self._on_remove_account)
                item._email = account.email
                item._provider = account.provider
                remove_menu.add(item)
            self.menu.add(remove_menu)

        self._add_reset_menu(accounts)

        self.menu.add(rumps.separator)
        item = rumps.MenuItem("Start at login", callback=self._on_toggle_start_at_login)
        item.state = int(login_item.is_enabled())
        self.menu.add(item)
        self.menu.add(rumps.MenuItem("\u23FB  Quit", callback=rumps.quit_application))

    def _add_provider_section(self, provider: str, accounts):
        header = rumps.MenuItem(f"\u2500\u2500 {PROVIDER_LABELS[provider]} \u2500\u2500")
        header.set_callback(None)
        self.menu.add(header)

        for account in accounts:
            has_creds = self._has_credentials(account)
            prefix = "\u25C9  " if account.active else "\u25CB  "
            if has_creds:
                label = f"{prefix}{account.email} ({account.subscription_type})"
                callback = getattr(self, PROVIDERS[provider]["account_click"])
                item = rumps.MenuItem(label, callback=callback)
            else:
                item = rumps.MenuItem(f"{prefix}{account.email} (unavailable)", callback=None)
            item._email = account.email
            item._provider = provider
            self.menu.add(item)

            if has_creds:
                key = account_key(account)
                cached = self._usage_cache.get(key, "\u2022\u2022\u2022")
                usage_label = rumps.MenuItem(f"       \u2502  {cached}", callback=None)
                usage_label._email = account.email
                usage_label._provider = provider
                self._usage_items[key] = usage_label
                self.menu.add(usage_label)

    def _add_auto_switch_menu(self):
        settings = load_settings(self.config_path)
        auto_menu = rumps.MenuItem("Auto-switch")
        for provider in ("claude", "codex"):
            item = rumps.MenuItem(PROVIDER_LABELS[provider], callback=self._on_toggle_auto_switch)
            item._provider = provider
            item.state = 1 if settings.auto_switch.get(provider, False) else 0
            auto_menu.add(item)
        auto_menu.add(rumps.separator)
        item = rumps.MenuItem("Use expiring quota first", callback=self._on_toggle_proactive_switch)
        item.state = 1 if settings.proactive_switch else 0
        auto_menu.add(item)
        self.menu.add(auto_menu)

    def _has_credentials(self, account) -> bool:
        entry = PROVIDERS.get(account.provider, PROVIDERS["codex"])
        service = f"{entry['credential_prefix']}{account.email}"
        return keychain.read_credentials(service) is not None

    def _add_auto_reset_menu(self):
        settings = load_settings(self.config_path)
        auto_menu = rumps.MenuItem("Auto-reset")
        item = rumps.MenuItem("Codex CLI", callback=self._on_toggle_auto_reset)
        item._provider = "codex"
        item.state = 1 if settings.auto_reset.get("codex", False) else 0
        auto_menu.add(item)
        self.menu.add(auto_menu)

    def _add_reset_menu(self, accounts):
        reset_menu = rumps.MenuItem("↺ Reset Codex usage")
        eligible = False
        for account in accounts:
            if account.provider != "codex":
                continue
            state = self._usage_state_cache.get(account_key(account))
            if state is None or state.reset_applicable <= 0:
                continue
            item = rumps.MenuItem(f"{account.email} ({state.reset_credits} available)",
                                  callback=self._on_reset_codex_usage)
            item._email = account.email
            reset_menu.add(item)
            eligible = True
        if not eligible:
            reset_menu.add(rumps.MenuItem("No reset applicable now", callback=None))
        self.menu.add(reset_menu)

    def _on_reset_codex_usage(self, sender):
        email = sender._email
        state = self._usage_state_cache.get(("codex", email))
        if state is None or state.reset_applicable <= 0:
            return
        if rumps.alert(
            title="Use a rate limit reset?",
            message=f"Use 1 of {state.reset_credits} banked resets for {email}? This resets that account's Codex 5-hour and weekly windows and cannot be undone.",
            ok="Reset", cancel="Cancel",
        ) != 1:
            return

        def _reset():
            result = self._consume_reset(email, state.reset_credits)

            def _finish():
                self._notify_reset_result(result)
                self._fetch_all_usage()

            _on_main_thread(_finish)

        threading.Thread(target=_reset, daemon=True).start()

    def _consume_reset(self, email: str, credits: int) -> dict:
        try:
            code = consume_reset_credit(email, self.config_path)
            return {"code": code, "email": email, "credits": credits}
        except Exception as exc:  # noqa: BLE001 - a dead thread would hide the failure
            return {"code": "error", "email": email, "message": str(exc)}

    def _notify_reset_result(self, result: dict, automatic: bool = False):
        code, email = result["code"], result["email"]
        if code == "error":
            rumps.notification(title="Error", subtitle="Error", message=result["message"])
            return
        subtitle = RESET_MESSAGES[code]
        message = email
        if code == "reset":
            message = f"{email}: windows reset"
            if automatic:
                subtitle = "Auto-reset applied"
                message += f" ({result['credits'] - 1} left)"
        rumps.notification(title="Claude Switcher", subtitle=subtitle, message=message)

    def _on_claude_account_click(self, sender):
        self._switch_account("claude", sender._email)

    def _on_codex_account_click(self, sender):
        # Codex revoked backups open login instead of switching; Claude rows always switch.
        state = self._usage_state_cache.get(("codex", sender._email))
        if state is not None and not state.available and "Login required" in state.display:
            self._on_add(sender)
            return
        self._switch_account("codex", sender._email)

    def _switch_account(self, provider: str, email: str):
        active = get_active_account(self.config_path, provider=provider)
        if active and active.email == email:
            return
        if provider in self._switch_in_progress:
            rumps.notification(
                title="Claude Switcher",
                subtitle=f"{PROVIDER_LABELS[provider]} switch already running",
                message="Wait for the current switch to finish.",
            )
            return

        self._manual_pin[provider] = email
        self._switch_in_progress.add(provider)

        def _switch():
            error = None
            try:
                PROVIDERS.get(provider, PROVIDERS["codex"])["switch"](email, self.config_path)
            except Exception as exc:
                error = str(exc)

            def _finish():
                self._switch_in_progress.discard(provider)
                if error:
                    rumps.alert(title="Error", message=error)
                else:
                    rumps.notification(
                        title="Claude Switcher",
                        subtitle=f"{PROVIDER_LABELS[provider]} account switched",
                        message=email,
                    )
                self._rebuild_menu()
                self._fetch_all_usage()

            _on_main_thread(_finish)

        threading.Thread(target=_switch, daemon=True).start()

    def _on_add(self, sender):
        """Add an account through the selected provider's login flow."""
        provider = sender._provider
        entry = PROVIDERS[provider]
        label, check_cli, add_fn, cancel_fn, login_instruction = entry["add"]
        if not check_cli():
            rumps.alert(
                title=f"{label} CLI not found",
                message=f"Please install {PROVIDER_LABELS[provider]} before adding an account.",
            )
            return
        # Clicking Add while a sign-in is already open means "start over":
        # cancel the old one (its snapshot is restored by its own cancelled
        # path) and begin a fresh login. One button, obvious intent.
        restarting = self._signing_in(provider)
        if restarting:
            cancel_fn()

        self._signing_in_since[provider] = time.time()
        rumps.notification(
            title="Claude Switcher",
            subtitle=f"Restarting {label} login…" if restarting else f"Opening {label} login…",
            message=login_instruction,
        )

        def _add():
            try:
                if restarting and not _wait_for_lease_release(provider):
                    raise RuntimeError(f"The previous {label} sign-in did not stop in time. Try again.")
                result = add_fn(self.config_path)
                if result:
                    title, subtitle, message = (
                        "Claude Switcher",
                        entry["add_success"],
                        f"{result.email} ({result.subscription_type})",
                    )
                else:
                    title, subtitle, message = (
                        "Claude Switcher",
                        "Cancelled",
                        "Login was cancelled or failed.",
                    )
            except Exception as exc:
                title, subtitle, message = "Claude Switcher", "Error", str(exc)

            def _finish():
                rumps.notification(title=title, subtitle=subtitle, message=message)
                self._rebuild_menu()
                self._fetch_all_usage()

            _on_main_thread(_finish)

        threading.Thread(target=_add, daemon=True).start()

    def _on_toggle_start_at_login(self, sender):
        bundle_path = NSBundle.mainBundle().bundlePath()
        if not bundle_path.endswith(".app"):
            rumps.notification(
                title="Claude Switcher",
                subtitle="Start at login needs the built app",
                message="Run the app from /Applications (build with ./build_local.sh --install).",
            )
            return
        try:
            enabled = not login_item.is_enabled()
            if enabled:
                login_item.enable(Path(bundle_path))
            else:
                login_item.disable()
            sender.state = int(enabled)
            rumps.notification(
                title="Claude Switcher",
                subtitle="Start at login enabled" if enabled else "Start at login disabled",
                message=bundle_path if enabled else "Claude Switcher will not open at login.",
            )
        except RuntimeError as exc:
            rumps.notification(
                title="Claude Switcher", subtitle="Start at login failed", message=str(exc),
            )

    def _on_toggle_auto_switch(self, sender):
        provider = sender._provider
        settings = load_settings(self.config_path)
        enabled = not settings.auto_switch.get(provider, False)
        set_auto_switch_enabled(provider, enabled, self.config_path)
        self._rebuild_menu()
        rumps.notification(
            title="Claude Switcher",
            subtitle=f"Auto-switch {PROVIDER_LABELS[provider]}",
            message="Enabled" if enabled else "Disabled",
        )

    def _on_toggle_proactive_switch(self, sender):
        enabled = not load_settings(self.config_path).proactive_switch
        set_proactive_switch_enabled(enabled, self.config_path)
        sender.state = 1 if enabled else 0
        rumps.notification(
            title="Claude Switcher",
            subtitle=f"Proactive switching {'enabled' if enabled else 'disabled'}",
            message=("Switch to the account whose quota expires soonest, before the active one runs out."
                     if enabled else "Only switch when the active account runs out."),
        )

    def _fetch_all_usage(self):
        """Fetch usage for all accounts in a background thread."""
        if self._refresh_in_progress:
            # Don't silently drop the request: run again once this one finishes.
            self._refresh_requested = True
            return
        self._refresh_in_progress = True
        accounts = load_accounts(self.config_path)
        active_by_provider = {
            "claude": get_active_account(self.config_path, provider="claude"),
            "codex": get_active_account(self.config_path, provider="codex"),
        }

        def _fetch():
            auto_switch_results = []
            auto_reset_result = None
            try:
                for account in accounts:
                    key = account_key(account)
                    if _add_lease_held(account.provider):
                        # Sign-in in progress for this provider: a fetch can only
                        # return "unavailable". Keep the last known numbers; the
                        # post-login refresh will resolve these rows.
                        continue
                    state = self._fetch_usage_state(account, active_by_provider.get(account.provider))
                    self._usage_state_cache[key] = state
                    self._usage_cache[key] = state.display

                for provider in ("claude", "codex"):
                    result = self._attempt_auto_switch(provider)
                    if result:
                        auto_switch_results.append(result)
                    if provider == "codex":
                        auto_reset_result = self._attempt_auto_reset(provider)
            finally:
                def _finish():
                    self._refresh_in_progress = False
                    switched = any(r["status"] == "switched" for r in auto_switch_results)

                    states = [self._usage_state_cache.get(account_key(a)) for a in accounts]
                    should_retry, self._quick_retries_left = _plan_quick_retry(
                        states, self._quick_retries_left
                    )
                    if should_retry and any(_add_lease_held(a.provider) for a in accounts):
                        # Don't cycle "Checking…"/retries while a sign-in is in
                        # progress; the post-login refresh resolves the rows.
                        should_retry = False
                    if should_retry:
                        # Show progress on the rows that have no data yet, rather
                        # than leaving them reading "Usage unavailable".
                        for account, state in zip(accounts, states):
                            if state is None or not state.available:
                                self._usage_cache[account_key(account)] = "Checking…"

                    # Rebuild only when the menu's structure changed: a switch, or
                    # the set of Codex accounts that can apply a reset. Rebuilding on
                    # every 5-minute tick would flicker an open menu and re-run the
                    # per-account keychain reads on the main thread.
                    last_eligible = getattr(self, "_last_reset_eligible", frozenset())
                    if switched or self._reset_eligible_emails() != last_eligible:
                        self._rebuild_menu()
                    self._update_usage_labels()
                    for result in auto_switch_results:
                        self._notify_auto_switch_result(result)
                    if auto_reset_result:
                        self._notify_reset_result(auto_reset_result, automatic=True)
                    if self._manual_refresh:
                        # The menu closed on click, so tell the user it finished
                        # and give them the numbers without reopening.
                        self._manual_refresh = False
                        rumps.notification(
                            title="Claude Switcher",
                            subtitle="Usage updated",
                            message=self._active_usage_summary(),
                        )
                    if switched or auto_reset_result:
                        self._fetch_all_usage()
                    elif should_retry:
                        self._schedule_quick_retry()
                    elif self._refresh_requested:
                        # A click landed while this refresh was in flight; honour it.
                        self._refresh_requested = False
                        self._fetch_all_usage()

                _on_main_thread(_finish)

        threading.Thread(target=_fetch, daemon=True).start()

    def _on_toggle_auto_reset(self, sender):
        settings = load_settings(self.config_path)
        enabled = not settings.auto_reset.get("codex", False)
        set_auto_reset_enabled("codex", enabled, self.config_path)
        self._rebuild_menu()
        rumps.notification(title="Claude Switcher", subtitle="Auto-reset Codex CLI",
                           message="Enabled" if enabled else "Disabled")

    def _attempt_auto_reset(self, provider: str) -> dict | None:
        if provider != "codex":
            return None
        active = get_active_account(self.config_path, provider=provider)
        if active is None:
            return None
        settings = load_settings(self.config_path)
        state = self._usage_state_cache.get(account_key(active))
        if state is None or not should_auto_reset(
            state, settings.auto_reset.get(provider, False), settings.auto_switch_threshold
        ):
            return None
        accounts = load_accounts(self.config_path)
        # Compute directly even when auto-switch is disabled or cooling down.
        if choose_auto_switch_target(
            provider, accounts, active.email, self._usage_state_cache,
            self._has_credentials, settings.auto_switch_threshold,
        ) is not None:
            return None
        target = choose_auto_reset_target(accounts, active.email, self._usage_state_cache)
        if target is None:
            return None
        now = time.time()
        last_attempt = self._last_auto_reset_attempt.get(provider)
        last_account_attempt = self._last_auto_reset_by_account.get(target.email)
        if (last_attempt is not None and now - last_attempt < AUTO_SWITCH_COOLDOWN_SECONDS) or (
            last_account_attempt is not None and now - last_account_attempt < AUTO_RESET_ACCOUNT_COOLDOWN_SECONDS
        ):
            return None
        # Guard attempts as well as successes: a delayed reset must not burn another credit.
        self._last_auto_reset_attempt[provider] = now
        self._last_auto_reset_by_account[target.email] = now
        return self._consume_reset(target.email, self._usage_state_cache[account_key(target)].reset_credits)

    def _schedule_quick_retry(self):
        """Refetch usage after a short delay, on the main thread. One pending at a time."""
        if self._quick_retry_timer is not None:
            self._quick_retry_timer.cancel()

        def _fire():
            _on_main_thread(self._fetch_all_usage)

        self._quick_retry_timer = threading.Timer(QUICK_RETRY_DELAY_SECONDS, _fire)
        self._quick_retry_timer.daemon = True
        self._quick_retry_timer.start()

    def _fetch_usage_state(self, account, active_account) -> UsageState:
        try:
            entry = PROVIDERS[account.provider]
            usage = (
                entry["fetch_active_usage"]()
                if active_account and active_account.email == account.email
                else entry["fetch_usage"](account.email)
            )
            return entry["usage_state"](usage)
        except Exception:
            pass
        return UsageState(available=False, display="Usage unavailable")

    def _attempt_auto_switch(self, provider: str) -> dict | None:
        settings = load_settings(self.config_path)
        if not settings.auto_switch.get(provider, False):
            return None
        active = get_active_account(self.config_path, provider=provider)
        if not active:
            return None

        active_state = self._usage_state_cache.get(account_key(active))
        if not active_state or not active_state.available:
            return None

        accounts = load_accounts(self.config_path)
        best = choose_fefo_target(
            provider, accounts, self._usage_state_cache, self._has_credentials,
            settings.auto_switch_threshold, active_email=active.email,
        )
        exhausted = should_auto_switch(active_state, True, settings.auto_switch_threshold)
        if not exhausted:
            if not settings.proactive_switch or best is None or best.email == active.email:
                return None
            if self._manual_pin.get(provider) == active.email:
                return None

        now = time.time()
        last_attempt = self._last_auto_switch_attempt.get(provider, 0)
        if now - last_attempt < AUTO_SWITCH_COOLDOWN_SECONDS:
            return None
        self._last_auto_switch_attempt[provider] = now

        target = best
        if exhausted and (target is None or target.email == active.email):
            target = choose_auto_switch_target(
                provider=provider,
                accounts=accounts,
                active_email=active.email,
                usage_by_account=self._usage_state_cache,
                has_credentials=self._has_credentials,
                threshold=settings.auto_switch_threshold,
            )
        if not target:
            return {"status": "no_target", "provider": provider, "email": active.email}

        try:
            PROVIDERS.get(provider, PROVIDERS["codex"])["switch"](target.email, self.config_path)
        except Exception as exc:
            return {
                "status": "error",
                "provider": provider,
                "email": active.email,
                "message": str(exc),
            }

        return {"status": "switched", "reason": "exhausted" if exhausted else "proactive",
                "provider": provider, "email": target.email}

    def _notify_auto_switch_result(self, result: dict):
        provider = result["provider"]
        label = PROVIDER_LABELS[provider]
        if result["status"] == "switched":
            rumps.notification(
                title="Claude Switcher",
                subtitle=(f"Auto-switched {label} early" if result.get("reason") == "proactive"
                          else f"Auto-switched {label}"),
                message=(f"{result['email']}: its quota expires sooner"
                         if result.get("reason") == "proactive" else result["email"]),
            )
        elif result["status"] == "no_target":
            rumps.notification(
                title="Claude Switcher",
                subtitle=f"{label} limit reached",
                message="No available account to switch to.",
            )
        elif result["status"] == "error":
            rumps.notification(
                title="Claude Switcher",
                subtitle=f"{label} auto-switch failed",
                message=result.get("message", "Unknown error"),
            )

    def _update_usage_labels(self):
        """Update usage labels in the menu from cache."""
        for key, item in self._usage_items.items():
            usage_text = self._usage_cache.get(key, "Usage unavailable")
            item.title = f"       \u2502  {usage_text}"

    _SIGNIN_GRACE_SECONDS = 3.0

    def _signing_in(self, provider: str) -> bool:
        """True while a sign-in for this provider is in progress.

        The real truth is the provider's add-lease. It is taken on a background
        thread a moment after Add is clicked, so for a short grace after the
        click we also treat the provider as signing in — that lets the Cancel
        item appear instantly and lets a double-click be refused, without
        racing the thread. Once the lease is released the grace has long
        expired, so the finish rebuild hides the item.
        """
        if _add_lease_held(provider):
            return True
        since = self._signing_in_since.get(provider)
        return since is not None and (time.time() - since) < self._SIGNIN_GRACE_SECONDS

    def _active_usage_summary(self) -> str:
        """One-line usage for each provider's active account, for notifications."""
        parts = []
        for provider in ("claude", "codex"):
            active = get_active_account(self.config_path, provider=provider)
            if active:
                text = self._usage_cache.get(account_key(active), "unavailable")
                parts.append(f"{PROVIDER_LABELS[provider]}: {text}")
        return "  ·  ".join(parts) or "Reopen the menu to see usage."

    def _on_refresh_usage(self, _):
        """Refresh usage data for all accounts, with visible feedback."""
        self._quick_retries_left = QUICK_RETRY_BUDGET
        self._manual_refresh = True
        # The menu closes on click; without this the click looks like a no-op.
        rumps.notification(
            title="Claude Switcher",
            subtitle="Refreshing usage…",
            message="You'll get a notice with the numbers when it's done.",
        )
        self._fetch_all_usage()

    def _on_periodic_usage_refresh(self, _):
        self._quick_retries_left = QUICK_RETRY_BUDGET
        self._fetch_all_usage()

    def _on_remove_account(self, sender):
        """Remove a saved account."""
        email = sender._email
        provider = sender._provider
        active = get_active_account(self.config_path, provider=provider)

        if active and active.email == email:
            rumps.alert(
                title="Cannot remove",
                message=f"You cannot remove the active {PROVIDER_LABELS[provider]} account. Switch first.",
            )
            return

        try:
            # Claude signals failure by exception; Codex also returns False for busy/changed accounts.
            if provider == "claude":
                remove_saved_account(email, self.config_path)
            elif not remove_codex_account(email, self.config_path):
                rumps.alert(
                    title="Account busy",
                    message="The Codex account changed or is busy. Please try again in a moment.",
                )
                self._rebuild_menu()
                return
        except RuntimeError as exc:
            rumps.alert(title="Account busy", message=str(exc))
            self._rebuild_menu()
            return
        rumps.notification(
            title="Claude Switcher",
            subtitle=f"{PROVIDER_LABELS[provider]} account removed",
            message=email,
        )
        self._rebuild_menu()
        self._fetch_all_usage()


def main():
    ClaudeSwitcherApp().run()


if __name__ == "__main__":
    main()
