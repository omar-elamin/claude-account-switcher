"""Pure auto-switch decision logic."""

import time
from collections.abc import Callable

from claude_switcher.config import AccountInfo
from claude_switcher.usage_state import UsageState, UsageWindow

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


def target_window(state: UsageState | None, provider: str) -> UsageWindow | None:
    """Return Codex's first window or Claude's Fable window, falling back to 7d."""
    if state is None or not state.available or not state.windows:
        return None
    if provider == "codex":
        return state.windows[0]
    if provider == "claude":
        for label in ("Fable", "7d"):
            for window in state.windows:
                if window.label == label:
                    return window
    return None


def fefo_key(state: UsageState | None, provider: str) -> tuple[float, float]:
    """Sort by earliest target reset, then most quota left; missing targets last."""
    target = target_window(state, provider)
    if target is None:
        return (float("inf"), float("inf"))
    return (target.resets_at if target.resets_at is not None else float("inf"),
            -(100 - target.percent))


TIE_SECONDS = 3600.0
QUIET_SECONDS = 24 * 3600.0
WEEK_SECONDS = 7 * 86400.0
UNTOUCHED_TOLERANCE_SECONDS = 15 * 60.0


def is_untouched(state: UsageState | None, provider: str, now: float) -> bool:
    """True when the target window's clock has not started.

    A window's clock starts at first use after a reset. Until then nothing is used and
    Claude reports no reset time at all, while Codex reports a reset a full week away.
    Such an account is at 100% but generates no refill until it is used, so starting
    its clock early (when nothing else is about to expire) brings its next refill
    forward by up to a week.
    """
    window = target_window(state, provider)
    if window is None or window.percent != 0:
        return False          # anything used means the clock is running, whatever the reset field says
    if window.resets_at is None:
        return True
    return window.resets_at - now >= WEEK_SECONDS - UNTOUCHED_TOLERANCE_SECONDS


def choose_fefo_target(
    provider: str,
    accounts: list[AccountInfo],
    usage_by_account: dict[AccountKey, UsageState],
    has_credentials: Callable[[AccountInfo], bool],
    threshold: float = 100.0,
    active_email: str | None = None,
    tie_seconds: float = TIE_SECONDS,
    quiet_seconds: float = QUIET_SECONDS,
    now: float | None = None,
) -> AccountInfo | None:
    """Choose a usable account by earliest target-window reset, including the active one.

    Resets within tie_seconds of the earliest count as a tie. Among tied accounts the
    active account wins, so two accounts with the same reset time do not swap back and
    forth as their leftovers drift; otherwise the most leftover wins, then list order.
    Optimality does not depend on the tie-break (any tie rule is optimal).

    Starting clocks: if some candidate's window has not started (see is_untouched) and
    no running candidate resets within quiet_seconds, choose among the untouched ones
    (same tie rules) so that its weekly refill is scheduled as early as possible.
    Otherwise choose among the running ones. Measured against a proven upper bound,
    this rule is within about 1% of optimal near capacity, where plain earliest-reset
    loses up to 5% because an account that reset while others still had quota sat
    idle and its refill cadence slipped.
    """
    candidates = []
    for account in accounts:
        if account.provider != provider or not has_credentials(account):
            continue
        state = usage_by_account.get(account_key(account))
        if state is not None and state.available and not state.is_exhausted(threshold):
            candidates.append(account)
    if not candidates:
        return None
    now = time.time() if now is None else now
    keys = {account.email: fefo_key(usage_by_account[account_key(account)], provider) for account in candidates}
    untouched = [a for a in candidates if is_untouched(usage_by_account[account_key(a)], provider, now)]
    running = [a for a in candidates if a not in untouched]
    quiet = not any(keys[a.email][0] - now <= quiet_seconds for a in running)
    pool = untouched if (untouched and quiet) else running   # running is non-empty here: no running means quiet
    earliest = min(keys[a.email][0] for a in pool)
    tied = [a for a in pool if keys[a.email][0] == earliest or keys[a.email][0] - earliest <= tie_seconds]
    for account in tied:
        if account.email == active_email:
            return account
    return min(tied, key=lambda account: keys[account.email][1])
