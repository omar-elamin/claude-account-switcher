from claude_switcher.auto_switch import (
    account_key,
    should_auto_switch,
    choose_auto_switch_target,
)
from claude_switcher.config import AccountInfo
from claude_switcher.usage_state import UsageState, UsageWindow


def _account(email, provider="claude", active=False):
    return AccountInfo(email, "pro", "", active, email, provider=provider)


def _usage(percent):
    return UsageState(True, f"{percent}%", (UsageWindow("test", percent),))


def test_should_auto_switch_requires_enabled_and_exhausted_usage():
    assert should_auto_switch(_usage(100), True, 100) is True
    assert should_auto_switch(_usage(99.9), True, 100) is False
    assert should_auto_switch(_usage(100), False, 100) is False
    assert should_auto_switch(UsageState(False, "Usage unavailable"), True, 100) is False


def test_choose_target_same_provider_only():
    active = _account("active@test.com", "claude", True)
    target = _account("target@test.com", "claude")
    other_provider_same_email = _account("target@test.com", "codex")

    chosen = choose_auto_switch_target(
        "claude",
        [active, other_provider_same_email, target],
        "active@test.com",
        {account_key(target): _usage(12), account_key(other_provider_same_email): _usage(0)},
        lambda account: True,
    )

    assert chosen == target


def test_choose_target_ignores_missing_credentials():
    active = _account("active@test.com", "claude", True)
    no_creds = _account("no-creds@test.com", "claude")
    target = _account("target@test.com", "claude")

    chosen = choose_auto_switch_target(
        "claude",
        [active, no_creds, target],
        "active@test.com",
        {account_key(no_creds): _usage(0), account_key(target): _usage(10)},
        lambda account: account.email != "no-creds@test.com",
    )

    assert chosen == target


def test_choose_target_returns_none_when_all_targets_exhausted():
    active = _account("active@test.com", "claude", True)
    exhausted = _account("exhausted@test.com", "claude")

    chosen = choose_auto_switch_target(
        "claude",
        [active, exhausted],
        "active@test.com",
        {account_key(exhausted): _usage(100)},
        lambda account: True,
    )

    assert chosen is None


def test_choose_target_uses_unknown_usage_as_fallback():
    active = _account("active@test.com", "claude", True)
    unknown = _account("unknown@test.com", "claude")

    chosen = choose_auto_switch_target(
        "claude",
        [active, unknown],
        "active@test.com",
        {},
        lambda account: True,
    )

    assert chosen == unknown


def test_should_auto_reset_matches_switch_semantics():
    from claude_switcher.auto_switch import should_auto_reset
    for state in [_usage(100), _usage(99.9), UsageState(False, "unavailable"), _usage(95)]:
        for enabled in (False, True):
            for threshold in (95, 100):
                assert should_auto_reset(state, enabled, threshold) == should_auto_switch(state, enabled, threshold)


def test_reset_target_prefers_active_then_first_exhausted_codex():
    from claude_switcher.auto_switch import choose_auto_reset_target
    from dataclasses import replace
    active = _account("active", "codex", True)
    other = _account("other", "codex")
    later = _account("later", "codex")
    claude = _account("active", "claude")
    healthy = _account("healthy", "codex")
    accounts = [claude, healthy, other, later, active]
    credit = replace(_usage(100), reset_credits=3, reset_applicable=2)
    states = {account_key(a): credit for a in accounts}
    states[account_key(healthy)] = replace(credit, windows=(UsageWindow("5h", 10),))
    assert choose_auto_reset_target(accounts, active.email, states) == active
    states[account_key(active)] = _usage(100)
    assert choose_auto_reset_target(accounts, active.email, states) == other
    del states[account_key(other)]
    assert choose_auto_reset_target(accounts, active.email, states) == later
    states[account_key(later)] = replace(credit, reset_applicable=0)
    assert choose_auto_reset_target(accounts, active.email, states) is None
    assert choose_auto_reset_target(accounts, active.email, {}) is None
