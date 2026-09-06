"""macOS menu bar application using rumps."""

import threading
import time
from pathlib import Path

import rumps
from Foundation import NSOperationQueue

from claude_switcher import keychain
from claude_switcher.auto_switch import (
    account_key,
    choose_auto_switch_target,
    should_auto_switch,
)
from claude_switcher.codex_core import (
    check_codex_cli,
    import_current_codex_account,
    switch_codex_account,
    add_new_codex_account,
    remove_codex_account,
)
from claude_switcher.codex_usage import (
    fetch_active_codex_usage,
    fetch_codex_usage_for_account,
    codex_usage_state,
)
from claude_switcher.config import (
    load_accounts,
    get_active_account,
    load_settings,
    set_auto_switch_enabled,
    DEFAULT_CONFIG_PATH,
)
from claude_switcher.core import (
    check_claude_cli,
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
AUTO_SWITCH_COOLDOWN_SECONDS = 60

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
    from claude_switcher import codex_core as _codex
    from claude_switcher import core as _core

    flag = _core._add_in_progress if provider == "claude" else _codex._add_in_progress
    return bool(flag)


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
        self._refresh_in_progress = False
        self._switch_in_progress: set[str] = set()
        self._quick_retries_left = QUICK_RETRY_BUDGET
        self._quick_retry_timer: threading.Timer | None = None
        self._refresh_requested = False   # a Refresh click landed mid-flight; honour it after
        self._manual_refresh = False      # current refresh is user-initiated -> notify when done
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

        if claude_available:
            imported = import_current_account(self.config_path)
            if imported:
                imported_any = True
                rumps.notification(
                    title="Claude Switcher",
                    subtitle="Claude account imported",
                    message=f"{imported.email} ({imported.subscription_type})",
                )

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

    def _rebuild_menu(self):
        """Rebuild the menu from current account state."""
        accounts = load_accounts(self.config_path)
        self.menu.clear()
        self._usage_items = {}

        claude_accounts = [a for a in accounts if a.provider == "claude"]
        codex_accounts = [a for a in accounts if a.provider == "codex"]

        if claude_accounts:
            self._add_provider_section("claude", claude_accounts)
        if codex_accounts:
            if claude_accounts:
                self.menu.add(rumps.separator)
            self._add_provider_section("codex", codex_accounts)

        self.menu.add(rumps.separator)
        self._add_auto_switch_menu()
        self.menu.add(rumps.MenuItem("\u271A  Add Claude account...", callback=self._on_add_claude_account))
        self.menu.add(rumps.MenuItem("\u271A  Add Codex account...", callback=self._on_add_codex_account))
        self.menu.add(rumps.MenuItem("\u2715  Cancel sign-in", callback=self._on_cancel_signin))
        self.menu.add(rumps.MenuItem("\u21BB  Refresh usage", callback=self._on_refresh_usage))

        if accounts:
            remove_menu = rumps.MenuItem("\u2212  Remove account")
            for account in accounts:
                provider_label = "Claude" if account.provider == "claude" else "Codex"
                item = rumps.MenuItem(f"[{provider_label}] {account.email}", callback=self._on_remove_account)
                item._email = account.email
                item._provider = account.provider
                remove_menu.add(item)
            self.menu.add(remove_menu)

        self.menu.add(rumps.separator)
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
                callback = (
                    self._on_claude_account_click
                    if provider == "claude"
                    else self._on_codex_account_click
                )
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
        self.menu.add(auto_menu)

    def _has_credentials(self, account) -> bool:
        service = (
            f"claude-switcher:{account.email}"
            if account.provider == "claude"
            else f"codex-switcher:{account.email}"
        )
        return keychain.read_credentials(service) is not None

    def _on_claude_account_click(self, sender):
        self._switch_account("claude", sender._email)

    def _on_codex_account_click(self, sender):
        # A row showing "Login required" holds a revoked backup; switching to it
        # can only fail. Send the user straight to the login flow instead.
        state = self._usage_state_cache.get(("codex", sender._email))
        if state is not None and not state.available and "Login required" in state.display:
            self._on_add_codex_account(None)
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

        self._switch_in_progress.add(provider)

        def _switch():
            error = None
            try:
                if provider == "claude":
                    switch_account(email, self.config_path)
                else:
                    switch_codex_account(email, self.config_path)
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

    def _on_add_claude_account(self, _):
        """Add a new Claude Code account via claude auth login."""
        if not check_claude_cli():
            rumps.alert(
                title="Claude CLI not found",
                message="Please install Claude Code before adding an account.",
            )
            return

        def _add():
            try:
                result = add_new_account(self.config_path)
                if result:
                    title, subtitle, message = (
                        "Claude Switcher",
                        "Claude account added",
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

    def _on_add_codex_account(self, _):
        """Add a new Codex CLI account via codex login."""
        if not check_codex_cli():
            rumps.alert(
                title="Codex CLI not found",
                message="Please install Codex CLI before adding an account.",
            )
            return

        # Immediate feedback: the menu closes on click, and the login opens in a
        # separate Terminal window, so without this the click looks like a no-op.
        rumps.notification(
            title="Claude Switcher",
            subtitle="Opening Codex login…",
            message="Sign in in the Terminal window that appears, then come back here.",
        )

        def _add():
            try:
                result = add_new_codex_account(self.config_path)
                if result:
                    title, subtitle, message = (
                        "Claude Switcher",
                        "Signed in to Codex",
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
            finally:
                def _finish():
                    self._refresh_in_progress = False
                    switched = any(r["status"] == "switched" for r in auto_switch_results)
                    if switched:
                        self._rebuild_menu()

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

                    self._update_usage_labels()
                    for result in auto_switch_results:
                        self._notify_auto_switch_result(result)
                    if self._manual_refresh:
                        # The menu closed on click, so tell the user it finished
                        # and give them the numbers without reopening.
                        self._manual_refresh = False
                        rumps.notification(
                            title="Claude Switcher",
                            subtitle="Usage updated",
                            message=self._active_usage_summary(),
                        )
                    if switched:
                        self._fetch_all_usage()
                    elif should_retry:
                        self._schedule_quick_retry()
                    elif self._refresh_requested:
                        # A click landed while this refresh was in flight; honour it.
                        self._refresh_requested = False
                        self._fetch_all_usage()

                _on_main_thread(_finish)

        threading.Thread(target=_fetch, daemon=True).start()

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
            if account.provider == "claude":
                usage = (
                    fetch_active_usage()
                    if active_account and active_account.email == account.email
                    else fetch_usage_for_account(account.email)
                )
                return claude_usage_state(usage)
            if account.provider == "codex":
                usage = (
                    fetch_active_codex_usage()
                    if active_account and active_account.email == account.email
                    else fetch_codex_usage_for_account(account.email)
                )
                return codex_usage_state(usage)
        except Exception:
            pass
        return UsageState(available=False, display="Usage unavailable")

    def _attempt_auto_switch(self, provider: str) -> dict | None:
        settings = load_settings(self.config_path)
        active = get_active_account(self.config_path, provider=provider)
        if not active:
            return None

        active_state = self._usage_state_cache.get(account_key(active))
        if not active_state or not should_auto_switch(
            active_state,
            settings.auto_switch.get(provider, False),
            settings.auto_switch_threshold,
        ):
            return None

        now = time.time()
        last_attempt = self._last_auto_switch_attempt.get(provider, 0)
        if now - last_attempt < AUTO_SWITCH_COOLDOWN_SECONDS:
            return None
        self._last_auto_switch_attempt[provider] = now

        accounts = load_accounts(self.config_path)
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
            if provider == "claude":
                switch_account(target.email, self.config_path)
            else:
                switch_codex_account(target.email, self.config_path)
        except Exception as exc:
            return {
                "status": "error",
                "provider": provider,
                "email": active.email,
                "message": str(exc),
            }

        return {"status": "switched", "provider": provider, "email": target.email}

    def _notify_auto_switch_result(self, result: dict):
        provider = result["provider"]
        label = PROVIDER_LABELS[provider]
        if result["status"] == "switched":
            rumps.notification(
                title="Claude Switcher",
                subtitle=f"Auto-switched {label}",
                message=result["email"],
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

    def _on_cancel_signin(self, _):
        """Abort whichever sign-in is in progress and restore the previous login.

        Each provider's add flow treats a cancelled login as its normal
        cancelled path, so it restores the snapshot and releases the add-lease.
        """
        from claude_switcher.codex_core import cancel_codex_login
        from claude_switcher.core import cancel_login as cancel_claude_login

        cancelled = []
        if _add_lease_held("claude"):
            cancel_claude_login()
            cancelled.append("Claude")
        if _add_lease_held("codex"):
            cancel_codex_login()
            cancelled.append("Codex")

        if not cancelled:
            rumps.notification(
                title="Claude Switcher",
                subtitle="Nothing to cancel",
                message="No sign-in is in progress.",
            )
            return
        rumps.notification(
            title="Claude Switcher",
            subtitle="Sign-in cancelled",
            message=f"{' and '.join(cancelled)} sign-in stopped. Your previous login is being restored.",
        )

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
