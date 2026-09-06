import json
import os
import stat
import threading
from unittest.mock import patch

import pytest

import claude_switcher.config as config_mod

from claude_switcher.config import (
    AccountInfo,
    AppSettings,
    load_accounts,
    save_accounts,
    add_account,
    remove_account,
    get_active_account,
    set_active_account,
    load_settings,
    save_settings,
    is_auto_switch_enabled,
    set_auto_switch_enabled,
)


class TestAccountInfo:
    def test_create_account(self):
        acc = AccountInfo(
            email="test@test.com",
            subscription_type="pro",
            org_name="Test Org",
            active=True,
            keychain_account="testuser",
        )
        assert acc.email == "test@test.com"
        assert acc.active is True


class TestLoadSave:
    def test_load_empty_returns_empty_list(self, tmp_path):
        result = load_accounts(tmp_path / "accounts.json")
        assert result == []

    def test_load_corrupted_json_returns_empty_list(self, tmp_path):
        path = tmp_path / "accounts.json"
        path.write_text("not valid json{{{")
        result = load_accounts(path)
        assert result == []

    def test_load_missing_accounts_key_returns_empty_list(self, tmp_path):
        path = tmp_path / "accounts.json"
        path.write_text('{"other": []}')
        result = load_accounts(path)
        assert result == []

    def test_save_and_load_roundtrip(self, tmp_path):
        path = tmp_path / "accounts.json"
        accounts = [
            AccountInfo("a@test.com", "pro", "Org A", True, "usera"),
            AccountInfo("b@test.com", "pro", "Org B", False, "userb"),
        ]
        save_accounts(accounts, path)
        loaded = load_accounts(path)
        assert len(loaded) == 2
        assert loaded[0].email == "a@test.com"
        assert loaded[1].active is False

    def test_failed_atomic_replace_leaves_original_parseable(self, tmp_path):
        path = tmp_path / "accounts.json"
        original = {"version": 2, "accounts": [], "settings": {}}
        path.write_text(json.dumps(original), encoding="utf-8")

        with patch.object(config_mod.os, "replace", side_effect=OSError("interrupted")):
            with pytest.raises(OSError, match="interrupted"):
                save_accounts(
                    [AccountInfo("new@test.com", "pro", "", True, "new")], path
                )

        assert json.loads(path.read_text(encoding="utf-8")) == original
        assert list(tmp_path.iterdir()) == [path]

    def test_atomic_replace_chmods_before_publish(self, tmp_path):
        path = tmp_path / "accounts.json"
        observed_modes = []
        real_replace = os.replace

        def checked_replace(source, target):
            observed_modes.append(stat.S_IMODE(os.stat(source).st_mode))
            real_replace(source, target)

        with patch.object(config_mod.os, "replace", side_effect=checked_replace):
            save_accounts([], path)

        assert observed_modes == [0o600]
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_atomic_write_follows_existing_symlink(self, tmp_path):
        referent = tmp_path / "real-accounts.json"
        referent.write_text('{"accounts": []}', encoding="utf-8")
        link = tmp_path / "accounts.json"
        link.symlink_to(referent)

        save_accounts([AccountInfo("new@test.com", "pro", "", True, "new")], link)

        assert link.is_symlink()
        assert load_accounts(referent)[0].email == "new@test.com"

    @pytest.mark.parametrize(
        "damaged",
        [
            b"not-json",
            b"[]",
            b'{"accounts": {}}',
            b'{"accounts": [{"email": "missing-fields"}]}',
            b"\xff",
        ],
        ids=["invalid-json", "non-dict-root", "non-list-accounts", "dropped-row", "invalid-utf8"],
    )
    def test_corrupt_config_is_preserved_before_replacement(self, tmp_path, damaged):
        path = tmp_path / "accounts.json"
        path.write_bytes(damaged)

        add_account(AccountInfo("new@test.com", "pro", "", True, "new"), path)

        assert (tmp_path / "accounts.json.corrupt").read_bytes() == damaged
        assert load_accounts(path)[0].email == "new@test.com"

    def test_distinct_corruptions_get_distinct_backups(self, tmp_path):
        path = tmp_path / "accounts.json"
        first = b"first invalid"
        second = b"second invalid"
        path.write_bytes(first)
        save_accounts([], path)
        path.write_bytes(second)
        save_accounts([], path)

        assert (tmp_path / "accounts.json.corrupt").read_bytes() == first
        assert (tmp_path / "accounts.json.corrupt.1").read_bytes() == second

    def test_invalid_utf8_is_forgiving_and_preserved_byte_for_byte(self, tmp_path):
        path = tmp_path / "accounts.json"
        path.write_bytes(b'{"accounts": []}\xff')

        assert load_accounts(path) == []
        save_accounts([], path)

        assert (tmp_path / "accounts.json.corrupt").read_bytes() == b'{"accounts": []}\xff'


class TestAccountOperations:
    def test_add_account(self, tmp_path):
        path = tmp_path / "accounts.json"
        acc = AccountInfo("a@test.com", "pro", "Org", False, "usera")
        add_account(acc, path)
        loaded = load_accounts(path)
        assert len(loaded) == 1

    def test_add_existing_updates(self, tmp_path):
        path = tmp_path / "accounts.json"
        acc1 = AccountInfo("a@test.com", "pro", "Org", True, "usera")
        acc2 = AccountInfo("a@test.com", "team", "New Org", True, "usera")
        add_account(acc1, path)
        add_account(acc2, path)
        loaded = load_accounts(path)
        assert len(loaded) == 1
        assert loaded[0].subscription_type == "team"

    def test_remove_account(self, tmp_path):
        path = tmp_path / "accounts.json"
        add_account(AccountInfo("a@test.com", "pro", "Org", True, "u"), path)
        add_account(AccountInfo("b@test.com", "pro", "Org", False, "u"), path)
        remove_account("a@test.com", path)
        loaded = load_accounts(path)
        assert len(loaded) == 1
        assert loaded[0].email == "b@test.com"

    def test_get_active_account(self, tmp_path):
        path = tmp_path / "accounts.json"
        add_account(AccountInfo("a@test.com", "pro", "Org", True, "u"), path)
        add_account(AccountInfo("b@test.com", "pro", "Org", False, "u"), path)
        active = get_active_account(path)
        assert active is not None
        assert active.email == "a@test.com"

    def test_set_active_account(self, tmp_path):
        path = tmp_path / "accounts.json"
        add_account(AccountInfo("a@test.com", "pro", "Org", True, "u"), path)
        add_account(AccountInfo("b@test.com", "pro", "Org", False, "u"), path)
        set_active_account("b@test.com", path)
        loaded = load_accounts(path)
        assert loaded[0].active is False
        assert loaded[1].active is True

    def test_concurrent_adds_do_not_lose_entries(self, tmp_path, monkeypatch):
        path = tmp_path / "accounts.json"
        barrier = threading.Barrier(2)
        real_load_accounts = config_mod.load_accounts

        def synchronized_load(config_path):
            accounts = real_load_accounts(config_path)
            try:
                barrier.wait(timeout=0.2)
            except threading.BrokenBarrierError:
                pass
            return accounts

        monkeypatch.setattr(config_mod, "load_accounts", synchronized_load)
        errors = []

        def add(email):
            try:
                add_account(AccountInfo(email, "pro", "", False, email), path)
            except BaseException as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=add, args=("a@test.com",)),
            threading.Thread(target=add, args=("b@test.com",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=2)

        assert not errors
        assert not any(thread.is_alive() for thread in threads)
        assert {account.email for account in real_load_accounts(path)} == {
            "a@test.com",
            "b@test.com",
        }


class TestProviderField:
    def test_default_provider_is_claude(self):
        acc = AccountInfo("test@test.com", "pro", "", True, "user")
        assert acc.provider == "claude"

    def test_codex_provider(self):
        acc = AccountInfo(
            "test@test.com",
            "plus",
            "",
            True,
            "user",
            provider="codex",
        )
        assert acc.provider == "codex"

    def test_load_legacy_config_defaults_to_claude(self, tmp_path):
        path = tmp_path / "accounts.json"
        path.write_text(json.dumps({
            "accounts": [{
                "email": "old@test.com",
                "subscription_type": "pro",
                "org_name": "",
                "active": True,
                "keychain_account": "user",
            }]
        }))

        accounts = load_accounts(path)

        assert len(accounts) == 1
        assert accounts[0].provider == "claude"

    def test_same_email_different_providers(self, tmp_path):
        path = tmp_path / "accounts.json"
        add_account(AccountInfo("user@test.com", "pro", "", True, "u", provider="claude"), path)
        add_account(AccountInfo("user@test.com", "plus", "", True, "u", provider="codex"), path)

        accounts = load_accounts(path)

        assert len(accounts) == 2
        assert {a.provider for a in accounts} == {"claude", "codex"}

    def test_remove_only_matching_provider(self, tmp_path):
        path = tmp_path / "accounts.json"
        add_account(AccountInfo("user@test.com", "pro", "", True, "u", provider="claude"), path)
        add_account(AccountInfo("user@test.com", "plus", "", False, "u", provider="codex"), path)

        remove_account("user@test.com", path, provider="codex")

        accounts = load_accounts(path)
        assert len(accounts) == 1
        assert accounts[0].provider == "claude"

    def test_set_active_only_affects_same_provider(self, tmp_path):
        path = tmp_path / "accounts.json"
        add_account(AccountInfo("claude@test.com", "pro", "", True, "u", provider="claude"), path)
        add_account(AccountInfo("codex-a@test.com", "plus", "", True, "u", provider="codex"), path)
        add_account(AccountInfo("codex-b@test.com", "plus", "", False, "u", provider="codex"), path)

        set_active_account("codex-b@test.com", path, provider="codex")

        accounts = load_accounts(path)
        claude = next(a for a in accounts if a.provider == "claude")
        codex_a = next(a for a in accounts if a.email == "codex-a@test.com")
        codex_b = next(a for a in accounts if a.email == "codex-b@test.com")
        assert claude.active is True
        assert codex_a.active is False
        assert codex_b.active is True


class TestSettings:
    def test_default_auto_switch_disabled(self, tmp_path):
        path = tmp_path / "accounts.json"
        settings = load_settings(path)
        assert settings.auto_switch == {"claude": False, "codex": False}
        assert settings.auto_switch_threshold == 100.0

    def test_settings_roundtrip(self, tmp_path):
        path = tmp_path / "accounts.json"
        save_settings(AppSettings(auto_switch={"claude": True, "codex": False}), path)
        assert is_auto_switch_enabled("claude", path) is True
        assert is_auto_switch_enabled("codex", path) is False

    def test_toggle_auto_switch(self, tmp_path):
        path = tmp_path / "accounts.json"
        set_auto_switch_enabled("codex", True, path)
        assert is_auto_switch_enabled("codex", path) is True

    def test_save_accounts_preserves_settings(self, tmp_path):
        path = tmp_path / "accounts.json"
        set_auto_switch_enabled("claude", True, path)
        save_accounts([AccountInfo("a@test.com", "pro", "", True, "u")], path)
        assert is_auto_switch_enabled("claude", path) is True
