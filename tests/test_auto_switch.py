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


def test_target_window_uses_provider_target():
    from claude_switcher.auto_switch import target_window, fefo_key
    five = UsageWindow("5h", 10)
    seven = UsageWindow("7d", 20)
    fable = UsageWindow("Fable", 30, scoped=True)
    state = UsageState(True, "", (five, seven, fable))
    assert target_window(state, "claude") is fable
    assert target_window(UsageState(True, "", (five, seven)), "claude") is seven
    assert target_window(state, "codex") is five
    for empty in (None, UsageState(False, "", (seven,)), UsageState(True, "")):
        assert target_window(empty, "claude") is None
        assert fefo_key(empty, "claude") == (float("inf"), float("inf"))
    assert target_window(UsageState(True, "", (five,)), "claude") is None


H = 3600  # reset gaps in hours: gaps under an hour count as a tie


def _weekly(percent, reset):
    return UsageState(True, "", (UsageWindow("7d", percent, resets_at=reset),))


def test_fefo_earliest_reset_then_most_left_then_list_order():
    from claude_switcher.auto_switch import choose_fefo_target, fefo_key
    accounts = [_account(name) for name in ("late", "early", "more-left", "tie", "unknown-reset")]
    states = dict(zip(map(account_key, accounts), [
        _weekly(0, 200 * H), _weekly(70, 100 * H), _weekly(20, 100 * H),
        _weekly(20, 100 * H), _weekly(0, None),
    ]))
    assert fefo_key(states[account_key(accounts[2])], "claude") == (100 * H, -80)
    assert fefo_key(states[account_key(accounts[4])], "claude") == (float("inf"), -100)
    assert choose_fefo_target("claude", accounts, states, lambda a: True) == accounts[2]
    assert choose_fefo_target("claude", accounts[:2], states, lambda a: True) == accounts[1]
    assert choose_fefo_target("claude", [accounts[4], accounts[0]], states, lambda a: True) == accounts[0]


def test_fefo_filters_unusable_accounts_and_includes_active():
    from claude_switcher.auto_switch import choose_fefo_target
    active = _account("active", active=True)
    exhausted, unavailable, no_creds, unknown = [_account(n) for n in ("full", "unavailable", "no-creds", "unknown")]
    other = _account("other", provider="codex")
    accounts = [exhausted, unavailable, no_creds, unknown, other, active]
    states = {account_key(a): _weekly(0, 10) for a in accounts}
    states[account_key(exhausted)] = UsageState(True, "", (
        UsageWindow("7d", 0, resets_at=10), UsageWindow("Fable", 95, scoped=True)))
    states[account_key(unavailable)] = UsageState(False, "")
    del states[account_key(unknown)]
    states[account_key(active)] = _weekly(20, 200)
    credentials = lambda a: a != no_creds
    assert choose_fefo_target("claude", accounts, states, credentials, threshold=95) == active
    assert choose_fefo_target("claude", accounts[:-1], states, credentials, threshold=95) is None
    assert choose_fefo_target("claude", [], {}, credentials) is None


from claude_switcher.auto_switch import choose_fefo_target  # noqa: E402  (tie-break tests)


def _state(percent, resets_at, label="7d"):
    return UsageState(True, "", (UsageWindow(label, percent, resets_at=resets_at),))


def test_fefo_tie_within_an_hour_keeps_the_active_account():
    a, b = _account("a@test.com", "codex", True), _account("b@test.com", "codex")
    usage = {account_key(a): _state(60, 1000.0), account_key(b): _state(20, 1000.0 + 1800)}
    chosen = choose_fefo_target("codex", [a, b], usage, lambda _: True, active_email="a@test.com")
    assert chosen == a  # b has more leftover but resets within the tie window: no flip


def test_fefo_tie_beyond_an_hour_is_not_a_tie():
    a, b = _account("a@test.com", "codex", True), _account("b@test.com", "codex")
    usage = {account_key(a): _state(60, 1000.0 + 7200), account_key(b): _state(20, 1000.0)}
    assert choose_fefo_target("codex", [a, b], usage, lambda _: True, active_email="a@test.com") == b


def test_fefo_tie_without_active_prefers_most_leftover_then_order():
    a, b, c = (_account(e, "codex") for e in ("a@test.com", "b@test.com", "c@test.com"))
    usage = {account_key(a): _state(60, 1000.0), account_key(b): _state(20, 1000.0), account_key(c): _state(20, 1000.0)}
    assert choose_fefo_target("codex", [a, b, c], usage, lambda _: True, active_email="zzz@test.com") == b


def test_fefo_two_untouched_accounts_tie_and_keep_active():
    a, b = _account("a@test.com", "claude", True), _account("b@test.com", "claude")
    usage = {account_key(a): _state(5, None, "Fable"), account_key(b): _state(0, None, "Fable")}
    assert choose_fefo_target("claude", [a, b], usage, lambda _: True, active_email="a@test.com") == a
