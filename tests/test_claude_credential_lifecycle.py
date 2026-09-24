"""Credential lifecycle regressions through real account files and core flows.

Only the OS Keychain, login subprocess and HTTP boundary are replaced. All
credentials are synthetic; these tests never touch a user's login.
"""

import json
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from claude_switcher import config, core, usage

REAL_KEYCHAIN = {name: getattr(core.keychain, name) for name in (
    "read_credentials", "write_credentials", "read_account_attribute",
    "snapshot_credentials", "restore_credentials", "delete_credentials")}


def credentials(label, *, expired=False):
    return json.dumps({"claudeAiOauth": {
        "accessToken": label, "refreshToken": label + "-refresh",
        "expiresAt": 1 if expired else 4102444800000,
    }, "mcpOAuth": {"fixture": "preserve"}})


CLEARED = json.dumps({"claudeAiOauth": {
    "accessToken": "", "refreshToken": "", "expiresAt": 0,
}, "mcpOAuth": {"fixture": "preserve"}})


@pytest.fixture
def lifecycle(tmp_path, monkeypatch):
    path = tmp_path / "accounts.json"
    state = tmp_path / "claude.json"
    monkeypatch.setattr(core, "CLAUDE_STATE_FILE", state)
    state.write_text(json.dumps({"oauthAccount": {"emailAddress": "a@example.test"},
                                 "unrelated": "preserve"}))
    for email, active in [("a@example.test", True), ("b@example.test", False)]:
        config.add_account(config.AccountInfo(
            email, "max", "fixture", active, "fixture-attribute",
            oauth_account={"emailAddress": email}, provider="claude"), path)
    a = "claude-switcher:a@example.test"
    b = "claude-switcher:b@example.test"
    store = {core.CLAUDE_SERVICE: credentials("a-live"),
             a: credentials("a-backup"), b: credentials("b-backup")}
    writes = []

    def write(service, account, blob):
        writes.append(service)
        store[service] = blob

    monkeypatch.setattr(core.keychain, "read_credentials", store.get)
    monkeypatch.setattr(core.keychain, "write_credentials", write)
    monkeypatch.setattr(core.keychain, "read_account_attribute", lambda _: "fixture-attribute")
    monkeypatch.setattr(core.keychain, "snapshot_credentials",
                        lambda service: ("fixture-attribute", store[service]) if service in store else None)
    monkeypatch.setattr(core.keychain, "restore_credentials", lambda service, pair: write(service, *pair))
    monkeypatch.setattr(core.keychain, "delete_credentials", lambda service: store.pop(service, None) is not None)
    monkeypatch.setattr(core, "run_auth_login", lambda: False)
    monkeypatch.setattr(core, "get_auth_status", lambda: {"loggedIn": True, "email": "a@example.test"})
    monkeypatch.setattr(usage, "_fetch_usage_once", lambda blob: {"five_hour": {"utilization": 1}})
    return SimpleNamespace(path=path, state=state, store=store, writes=writes, a=a, b=b)


def test_switch_away_from_cleared_login_preserves_backup_and_can_return(lifecycle):
    s = lifecycle
    original = s.store[s.a]
    s.store[core.CLAUDE_SERVICE] = CLEARED
    core.switch_account("b@example.test", s.path)
    assert s.store[s.a] == original
    assert s.store[core.CLAUDE_SERVICE] == s.store[s.b]
    core.switch_account("a@example.test", s.path)
    assert s.store[core.CLAUDE_SERVICE] == original
    assert config.get_active_account(s.path).email == "a@example.test"
    assert json.loads(s.state.read_text())["unrelated"] == "preserve"


@pytest.mark.parametrize("bad", [CLEARED, "{}", "null", "[]", "invalid-json",
    json.dumps({"claudeAiOauth": {"accessToken": 123, "refreshToken": "r"}})])
def test_invalid_target_cannot_replace_working_login(lifecycle, bad):
    s = lifecycle
    s.store[s.b] = bad
    before = (dict(s.store), s.path.read_bytes(), s.state.read_bytes())
    with pytest.raises(core.ClaudeCredentialsExpiredError):
        core.switch_account("b@example.test", s.path)
    assert (s.store, s.path.read_bytes(), s.state.read_bytes()) == before
    assert s.writes == []


def test_expired_but_refreshable_account_can_still_be_switched_to(lifecycle):
    s = lifecycle
    s.store[s.b] = credentials("b-expired", expired=True)
    core.switch_account("b@example.test", s.path)
    assert s.store[core.CLAUDE_SERVICE] == s.store[s.b]


def test_cancelled_add_does_not_poison_good_backup(lifecycle):
    s = lifecycle
    original = s.store[s.a]
    s.store[core.CLAUDE_SERVICE] = CLEARED
    assert core.add_new_account(s.path) is None
    assert s.store[s.a] == original
    assert s.store[core.CLAUDE_SERVICE] == CLEARED


def test_import_cleared_login_cannot_overwrite_saved_account(lifecycle):
    s = lifecycle
    s.store[core.CLAUDE_SERVICE] = CLEARED
    before = dict(s.store)
    assert core.import_current_account(s.path) is None
    assert s.store == before
    assert s.writes == []


def test_usage_poll_reconciles_external_login_and_survives_switch_roundtrip(lifecycle):
    s = lifecycle
    fresh = s.store[core.CLAUDE_SERVICE]
    s.store[s.a] = CLEARED
    usage.fetch_active_usage(s.path)
    assert s.store[s.a] == fresh
    assert s.writes == [s.a]  # Reconciliation must never rewrite the live slot.
    usage.fetch_active_usage(s.path)
    assert s.writes == [s.a]
    core.switch_account("b@example.test", s.path)
    core.switch_account("a@example.test", s.path)
    assert s.store[core.CLAUDE_SERVICE] == fresh


def test_usage_poll_with_cleared_login_preserves_backup(lifecycle):
    s = lifecycle
    original = s.store[s.a]
    s.store[core.CLAUDE_SERVICE] = CLEARED
    usage.fetch_active_usage(s.path)
    assert s.store[s.a] == original
    assert s.writes == []


def test_usage_poll_never_attributes_other_identity_to_active_account(lifecycle):
    s = lifecycle
    s.state.write_text(json.dumps({"oauthAccount": {"emailAddress": "b@example.test"}}))
    original = dict(s.store)
    usage.fetch_active_usage(s.path)
    assert s.store == original
    assert s.writes == []


def test_overlapping_poll_and_switch_preserve_fresh_login(lifecycle, monkeypatch):
    s = lifecycle
    fresh = s.store[core.CLAUDE_SERVICE]
    s.store[s.a] = CLEARED
    fetching, finish = threading.Event(), threading.Event()

    def fetch(blob):
        fetching.set()
        assert finish.wait(3)
        return {"five_hour": {"utilization": 1}}

    monkeypatch.setattr(usage, "_fetch_usage_once", fetch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        poll = pool.submit(usage.fetch_active_usage, s.path)
        try:
            assert fetching.wait(3)
            assert s.store[s.a] == fresh
            pool.submit(core.switch_account, "b@example.test", s.path).result(timeout=3)
        finally:
            finish.set()
        poll.result(timeout=3)
    assert config.get_active_account(s.path).email == "b@example.test"
    assert s.store[core.CLAUDE_SERVICE] == s.store[s.b]
    assert s.store[s.a] == fresh


def test_usage_poll_defers_reconciliation_during_account_add(lifecycle, monkeypatch):
    s = lifecycle
    s.store[s.a] = CLEARED
    monkeypatch.setattr(core, "_add_in_progress", True)
    usage.fetch_active_usage(s.path)
    assert s.store[s.a] == CLEARED
    assert s.writes == []


def test_repeated_switch_to_same_account_preserves_new_login(lifecycle):
    s = lifecycle
    fresh = s.store[core.CLAUDE_SERVICE]
    core.switch_account("a@example.test", s.path)
    assert s.store[core.CLAUDE_SERVICE] == fresh
    assert core.CLAUDE_SERVICE not in s.writes


@pytest.mark.parametrize("change", ["token", "identity"])
def test_external_login_change_during_poll_is_not_saved_under_stale_identity(lifecycle, monkeypatch, change):
    s = lifecycle
    old = s.store[s.a]
    def read_attribute(service):
        if change == "token":
            s.store[core.CLAUDE_SERVICE] = credentials("changed-externally")
        else:
            s.state.write_text(json.dumps({"oauthAccount": {"emailAddress": "b@example.test"}}))
        return "fixture-attribute"
    monkeypatch.setattr(core.keychain, "read_account_attribute", read_attribute)
    usage.fetch_active_usage(s.path)
    assert s.store[s.a] == old
    assert s.writes == []


@pytest.mark.skipif(sys.platform != "darwin", reason="native macOS Keychain integration")
def test_recovery_roundtrip_through_real_isolated_keychain(lifecycle, monkeypatch, tmp_path):
    s = lifecycle
    kc = str(tmp_path / "lifecycle.keychain-db")
    create = subprocess.run(["/usr/bin/security", "create-keychain", "-p", "fixture", kc],
                            capture_output=True, text=True)
    if create.returncode:
        pytest.skip("sandbox cannot create isolated Keychain")
    try:
        subprocess.run(["/usr/bin/security", "unlock-keychain", "-p", "fixture", kc],
                       capture_output=True, check=True)
        def isolated_run(args, **kwargs):
            assert args[0] == "security"
            assert args[1] in {"find-generic-password", "add-generic-password", "delete-generic-password"}
            return subprocess.run(["/usr/bin/security", *args[1:], kc], **kwargs)
        monkeypatch.setattr(core.keychain, "subprocess", SimpleNamespace(
            run=isolated_run, TimeoutExpired=subprocess.TimeoutExpired))
        for name, func in REAL_KEYCHAIN.items():
            monkeypatch.setattr(core.keychain, name, func)
        for service, blob in s.store.items():
            core.keychain.write_credentials(service, "fixture-attribute", blob)
        fresh = credentials("synthetic-" + "x" * 3000)
        core.keychain.write_credentials(core.CLAUDE_SERVICE, "fixture-attribute", fresh)
        core.keychain.write_credentials(s.a, "fixture-attribute", CLEARED)
        usage.fetch_active_usage(s.path)
        assert core.keychain.read_credentials(s.a) == fresh
        core.switch_account("b@example.test", s.path)
        core.switch_account("a@example.test", s.path)
        assert core.keychain.read_credentials(core.CLAUDE_SERVICE) == fresh
        core.keychain.write_credentials(s.b, "fixture-attribute", CLEARED)
        with pytest.raises(core.ClaudeCredentialsExpiredError):
            core.switch_account("b@example.test", s.path)
        assert core.keychain.read_credentials(core.CLAUDE_SERVICE) == fresh
        assert config.get_active_account(s.path).email == "a@example.test"
    finally:
        subprocess.run(["/usr/bin/security", "delete-keychain", kc], capture_output=True, check=True)
