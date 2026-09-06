"""Pure auto-switch decision logic."""

from collections.abc import Callable

from claude_switcher.config import AccountInfo
from claude_switcher.usage_state import UsageState

AccountKey = tuple[str, str]


def account_key(account: AccountInfo) -> AccountKey:
    """Return the stable cache key for an account."""
    return (account.provider, account.email)


def should_auto_switch(active_usage: UsageState, enabled: bool, threshold: float) -> bool:
    """Return whether the active account should trigger an auto-switch."""
    return enabled and active_usage.available and active_usage.is_exhausted(threshold)


def should_auto_reset(active_usage: UsageState, enabled: bool, threshold: float) -> bool:
    """Return whether the active account should trigger an auto-reset."""
    return should_auto_switch(active_usage, enabled, threshold)


def choose_auto_reset_target(
    accounts: list[AccountInfo],
    active_email: str,
    usage_by_account: dict[AccountKey, UsageState],
) -> AccountInfo | None:
    """Prefer the active Codex account, then an exhausted saved account."""
    candidates = []
    for account in accounts:
        if account.provider != "codex":
            continue
        state = usage_by_account.get(account_key(account))
        if state is None or state.reset_applicable <= 0:
            continue
        if account.email == active_email:
            return account
        if state.is_exhausted():
            candidates.append(account)
    return candidates[0] if candidates else None


def choose_auto_switch_target(
    provider: str,
    accounts: list[AccountInfo],
    active_email: str,
    usage_by_account: dict[AccountKey, UsageState],
    has_credentials: Callable[[AccountInfo], bool],
    threshold: float = 100.0,
) -> AccountInfo | None:
    """Choose the best same-provider account to switch to."""
    candidates = [
        account
        for account in accounts
        if account.provider == provider
        and account.email != active_email
        and has_credentials(account)
    ]

    known_available = []
    unknown_usage = []
    for account in candidates:
        state = usage_by_account.get(account_key(account))
        if state is None or not state.available:
            unknown_usage.append(account)
        elif not state.is_exhausted(threshold):
            known_available.append(account)

    if known_available:
        return known_available[0]
    if unknown_usage:
        return unknown_usage[0]
    return None
