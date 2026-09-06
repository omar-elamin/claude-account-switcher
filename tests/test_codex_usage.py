"""Tests for Codex usage module."""

import json

import pytest
import threading
import urllib.error
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

import claude_switcher.codex_core as codex_core_mod
import claude_switcher.codex_usage as codex_usage_mod
from claude_switcher.config import AccountInfo, remove_account, save_accounts, set_active_account

from claude_switcher.codex_usage import (
    CODEX_LOGIN_REQUIRED_USAGE,
    _extract_codex_token,
    fetch_codex_usage,
    fetch_active_codex_usage,
    fetch_codex_usage_for_account,
    fetch_codex_usage_with_refresh,
    format_codex_usage,
    codex_usage_state,
)

FAKE_CREDS_NESTED = json.dumps({
    "auth_mode": "chatgpt",
    "tokens": {
        "access_token": "sk-test-token",
        "account_id": "acc-123",
    },
})

FAKE_CREDS_FLAT = json.dumps({
    "access_token": "sk-test-token",
    "account_id": "acc-123",
})


class TestExtractCodexToken:
    def test_extracts_token_from_nested_structure(self):
        token, account_id = _extract_codex_token(FAKE_CREDS_NESTED)
        assert token == "sk-test-token"
        assert account_id == "acc-123"

    def test_extracts_token_from_flat_structure(self):
        token, account_id = _extract_codex_token(FAKE_CREDS_FLAT)
        assert token == "sk-test-token"
        assert account_id == "acc-123"

    def test_extracts_token_from_hex_encoded_structure(self):
        token, account_id = _extract_codex_token(FAKE_CREDS_NESTED.encode("utf-8").hex())
        assert token == "sk-test-token"
        assert account_id == "acc-123"

    def test_returns_none_for_invalid_json(self):
        result = _extract_codex_token("not json")
        assert result is None

    def test_returns_none_for_missing_fields(self):
        result = _extract_codex_token('{"other": "data"}')
        assert result is None


class TestFormatCodexUsage:
    def test_formats_both_windows(self):
        reset_primary = (datetime.now(timezone.utc) + timedelta(hours=2)).timestamp()
        reset_secondary = (datetime.now(timezone.utc) + timedelta(days=5, hours=13)).timestamp()
        usage = {
            "rate_limit": {
                "primary_window": {"used_percent": 43, "reset_at": reset_primary},
                "secondary_window": {"used_percent": 29, "reset_at": reset_secondary},
            }
        }
        result = format_codex_usage(usage)
        assert "43%" in result
        assert "29%" in result

    def test_returns_unavailable_for_none(self):
        assert format_codex_usage(None) == "Usage unavailable"

    def test_returns_unavailable_for_empty(self):
        assert format_codex_usage({}) == "Usage unavailable"

    def test_returns_login_required_for_expired_session(self):
        assert codex_usage_state(CODEX_LOGIN_REQUIRED_USAGE).display == "Login required"

    def test_usage_state_marks_exhausted_at_100(self):
        usage = {
            "rate_limit": {
                "primary_window": {"used_percent": 100},
                "secondary_window": {"used_percent": 12},
            }
        }
        state = codex_usage_state(usage)
        assert state.available is True
        assert state.is_exhausted() is True
        assert state.max_percent == 100

    def test_usage_state_does_not_mark_99_9_exhausted(self):
        usage = {"rate_limit": {"primary_window": {"used_percent": 99.9}}}
        assert codex_usage_state(usage).is_exhausted() is False


class TestFetchCodexUsageForAccount:
    @patch("claude_switcher.codex_usage.urlopen")
    def test_fetch_tries_second_endpoint_after_first_fails(self, mock_urlopen):
        response_data = json.dumps({"rate_limit": {"primary_window": {"used_percent": 10}}}).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = response_data
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.side_effect = [urllib.error.URLError("nope"), mock_resp]

        result = fetch_codex_usage(FAKE_CREDS_NESTED)

        assert result["rate_limit"]["primary_window"]["used_percent"] == 10
        assert mock_urlopen.call_count == 2

    @patch("claude_switcher.codex_usage._fetch_codex_usage_once")
    @patch("claude_switcher.codex_usage.keychain")
    def test_fetches_for_account(self, mock_kc, mock_fetch):
        mock_kc.read_credentials.return_value = FAKE_CREDS_NESTED
        mock_fetch.return_value = {"rate_limit": {}}
        result = fetch_codex_usage_for_account("user@test.com")
        mock_kc.read_credentials.assert_called_with("codex-switcher:user@test.com")
        assert result is not None

    @patch("claude_switcher.codex_usage.keychain")
    def test_returns_none_when_no_creds(self, mock_kc):
        mock_kc.read_credentials.return_value = None
        result = fetch_codex_usage_for_account("user@test.com")
        assert result is None

    @patch("claude_switcher.codex_usage._fetch_codex_usage_once")
    @patch("claude_switcher.codex_usage.refresh_codex_credentials")
    def test_fetch_refreshes_stale_credentials(self, mock_refresh, mock_fetch_once):
        refreshed = json.dumps({
            "auth_mode": "chatgpt",
            "tokens": {"access_token": "fresh", "account_id": "acc-123"},
        })
        mock_fetch_once.side_effect = [None, {"rate_limit": {}}]
        mock_refresh.return_value = refreshed

        usage, refreshed_creds = fetch_codex_usage_with_refresh(FAKE_CREDS_NESTED)

        assert usage == {"rate_limit": {}}
        assert refreshed_creds == refreshed

    @patch("claude_switcher.codex_usage.refresh_codex_credentials")
    @patch("claude_switcher.codex_usage._fetch_codex_usage_once")
    @patch("claude_switcher.codex_usage.keychain")
    def test_fetch_for_account_saves_refreshed_credentials(
        self, mock_kc, mock_fetch, mock_refresh, tmp_path
    ):
        config = tmp_path / "accounts.json"
        save_accounts([
            AccountInfo("user@test.com", "plus", "", False, "user@test.com", provider="codex")
        ], config)
        mock_kc.read_credentials.return_value = FAKE_CREDS_NESTED
        mock_fetch.side_effect = [None, {"rate_limit": {}}]
        mock_refresh.return_value = '{"fresh": true}'

        assert fetch_codex_usage_for_account("user@test.com", config) == {"rate_limit": {}}
        mock_kc.write_credentials.assert_called_once_with(
            "codex-switcher:user@test.com",
            "user@test.com",
            '{"fresh": true}',
        )

    @patch("claude_switcher.codex_usage.refresh_codex_credentials")
    @patch("claude_switcher.codex_usage._fetch_codex_usage_once")
    @patch("claude_switcher.codex_usage.keychain")
    def test_hex_saved_backup_compares_raw_then_refreshes(
        self, mock_kc, mock_fetch, mock_refresh, tmp_path
    ):
        config = tmp_path / "accounts.json"
        save_accounts([
            AccountInfo("user@test.com", "plus", "", False, "user@test.com", provider="codex")
        ], config)
        raw_hex = FAKE_CREDS_NESTED.encode("utf-8").hex()
        mock_kc.read_credentials.side_effect = [raw_hex, raw_hex]
        mock_fetch.side_effect = [None, {"rate_limit": {}}]
        mock_refresh.return_value = '{"fresh": true}'

        assert fetch_codex_usage_for_account("user@test.com", config) == {"rate_limit": {}}

        mock_refresh.assert_called_once_with(FAKE_CREDS_NESTED)
        mock_kc.write_credentials.assert_called_once()

    @patch("claude_switcher.codex_usage.refresh_codex_credentials")
    @patch("claude_switcher.codex_usage._fetch_codex_usage_once", return_value=None)
    @patch("claude_switcher.codex_usage.keychain")
    def test_changed_saved_source_drops_work_before_consuming_refresh_token(
        self, mock_kc, mock_fetch, mock_refresh, tmp_path
    ):
        config = tmp_path / "accounts.json"
        save_accounts([
            AccountInfo("user@test.com", "plus", "", False, "user@test.com", provider="codex")
        ], config)
        mock_kc.read_credentials.side_effect = [FAKE_CREDS_NESTED, '{"new": true}']

        assert fetch_codex_usage_for_account("user@test.com", config) is None

        mock_refresh.assert_not_called()
        mock_kc.write_credentials.assert_not_called()

    @patch("claude_switcher.codex_usage.refresh_codex_credentials")
    @patch("claude_switcher.codex_usage._fetch_codex_usage_once")
    @patch("claude_switcher.codex_usage.keychain")
    def test_saved_refresh_drops_if_account_became_active(
        self, mock_kc, mock_fetch, mock_refresh, tmp_path
    ):
        config = tmp_path / "accounts.json"
        save_accounts([
            AccountInfo("user@test.com", "plus", "", False, "user@test.com", provider="codex")
        ], config)
        mock_kc.read_credentials.return_value = FAKE_CREDS_NESTED
        fetch_started = threading.Event()
        release_fetch = threading.Event()
        results = []

        def blocked_fetch(creds):
            fetch_started.set()
            assert release_fetch.wait(timeout=2)
            return None

        mock_fetch.side_effect = blocked_fetch
        thread = threading.Thread(
            target=lambda: results.append(fetch_codex_usage_for_account("user@test.com", config))
        )
        thread.start()
        assert fetch_started.wait(timeout=2)
        set_active_account("user@test.com", config, provider="codex")
        release_fetch.set()
        thread.join(timeout=2)

        assert not thread.is_alive()
        assert results == [None]
        mock_refresh.assert_not_called()
        mock_kc.write_credentials.assert_not_called()

    def test_saved_refresh_finishing_after_removal_does_not_recreate_backup(self, tmp_path):
        config = tmp_path / "accounts.json"
        save_accounts([
            AccountInfo("user@test.com", "plus", "", False, "user@test.com", provider="codex")
        ], config)
        fetch_started = threading.Event()
        release_fetch = threading.Event()
        results = []

        def blocked_fetch(creds):
            fetch_started.set()
            assert release_fetch.wait(timeout=2)
            return None

        with patch.object(codex_usage_mod, "keychain") as mock_kc, patch.object(
            codex_usage_mod, "_fetch_codex_usage_once", side_effect=blocked_fetch
        ), patch.object(codex_usage_mod, "refresh_codex_credentials") as mock_refresh:
            mock_kc.read_credentials.return_value = FAKE_CREDS_NESTED
            thread = threading.Thread(
                target=lambda: results.append(fetch_codex_usage_for_account("user@test.com", config))
            )
            thread.start()
            assert fetch_started.wait(timeout=2)
            remove_account("user@test.com", config, provider="codex")
            release_fetch.set()
            thread.join(timeout=2)

        assert not thread.is_alive()
        assert results == [None]
        mock_refresh.assert_not_called()
        mock_kc.write_credentials.assert_not_called()


class TestFetchActiveCodexUsageConcurrency:
    def test_refresh_finishing_after_switch_does_not_overwrite_new_active_file(self, tmp_path):
        config = tmp_path / "accounts.json"
        config_file = tmp_path / "config.toml"
        auth_file = tmp_path / "auth.json"
        config_file.write_text('cli_auth_credentials_store = "file"')
        old_creds = json.dumps({
            "email": "old@test.com",
            "tokens": {"access_token": "old", "account_id": "one", "refresh_token": "refresh"},
        })
        new_creds = json.dumps({
            "email": "new@test.com",
            "tokens": {"access_token": "new", "account_id": "two", "refresh_token": "refresh"},
        })
        auth_file.write_text(old_creds)
        save_accounts([
            AccountInfo("old@test.com", "plus", "", True, "old", provider="codex"),
            AccountInfo("new@test.com", "plus", "", False, "new", provider="codex"),
        ], config)
        fetch_started = threading.Event()
        release_fetch = threading.Event()
        results = []

        def blocked_fetch(creds):
            fetch_started.set()
            assert release_fetch.wait(timeout=2)
            return None

        with patch.object(codex_core_mod, "CODEX_AUTH_FILE", auth_file), patch.object(
            codex_core_mod, "CODEX_CONFIG_FILE", config_file
        ), patch.object(codex_usage_mod, "_fetch_codex_usage_once", side_effect=blocked_fetch), patch.object(
            codex_usage_mod, "refresh_codex_credentials"
        ) as usage_refresh, patch.object(codex_usage_mod, "keychain") as usage_kc, patch.object(
            codex_core_mod, "keychain"
        ) as core_kc, patch.object(
            codex_core_mod, "refresh_codex_credentials", return_value=None
        ):
            core_kc.read_credentials.return_value = new_creds
            thread = threading.Thread(
                target=lambda: results.append(fetch_active_codex_usage(config))
            )
            thread.start()
            assert fetch_started.wait(timeout=2)
            codex_core_mod.switch_codex_account("new@test.com", config)
            release_fetch.set()
            thread.join(timeout=2)

        assert not thread.is_alive()
        assert results == [None]
        assert auth_file.read_text() == new_creds
        usage_refresh.assert_not_called()
        usage_kc.write_credentials.assert_not_called()

    def test_same_email_relogin_invalidates_in_flight_refresh(self, tmp_path):
        config = tmp_path / "accounts.json"
        config_file = tmp_path / "config.toml"
        auth_file = tmp_path / "auth.json"
        config_file.write_text('cli_auth_credentials_store = "file"')
        old_creds = json.dumps({
            "email": "same@test.com",
            "tokens": {"access_token": "old", "account_id": "one", "refresh_token": "old-refresh"},
        })
        new_creds = json.dumps({
            "email": "same@test.com",
            "tokens": {"access_token": "new", "account_id": "one", "refresh_token": "new-refresh"},
        })
        auth_file.write_text(old_creds)
        save_accounts([
            AccountInfo("same@test.com", "plus", "", True, "same", provider="codex")
        ], config)
        fetch_started = threading.Event()
        release_fetch = threading.Event()

        def blocked_fetch(creds):
            fetch_started.set()
            assert release_fetch.wait(timeout=2)
            return None

        with patch.object(codex_core_mod, "CODEX_AUTH_FILE", auth_file), patch.object(
            codex_core_mod, "CODEX_CONFIG_FILE", config_file
        ), patch.object(codex_usage_mod, "_fetch_codex_usage_once", side_effect=blocked_fetch), patch.object(
            codex_usage_mod, "refresh_codex_credentials"
        ) as mock_refresh, patch.object(codex_usage_mod, "keychain") as mock_kc:
            thread = threading.Thread(target=lambda: fetch_active_codex_usage(config))
            thread.start()
            assert fetch_started.wait(timeout=2)
            auth_file.write_text(new_creds)
            release_fetch.set()
            thread.join(timeout=2)

        assert not thread.is_alive()
        assert auth_file.read_text() == new_creds
        mock_refresh.assert_not_called()
        mock_kc.write_credentials.assert_not_called()

    def test_usage_writeback_cannot_land_during_codex_add_login(self, tmp_path):
        config = tmp_path / "accounts.json"
        config_file = tmp_path / "config.toml"
        auth_file = tmp_path / "auth.json"
        config_file.write_text('cli_auth_credentials_store = "file"')
        current = json.dumps({
            "email": "old@test.com",
            "tokens": {"access_token": "old", "account_id": "one", "refresh_token": "refresh"},
        })
        auth_file.write_text(current)
        save_accounts([
            AccountInfo("old@test.com", "plus", "", True, "old", provider="codex")
        ], config)
        usage_started = threading.Event()
        release_usage = threading.Event()
        login_started = threading.Event()
        release_login = threading.Event()
        add_errors = []

        def blocked_fetch(creds):
            usage_started.set()
            assert release_usage.wait(timeout=2)
            return None

        def blocked_login():
            login_started.set()
            assert release_login.wait(timeout=2)
            return False

        shared_keychain = MagicMock()
        with patch.object(codex_core_mod, "CODEX_AUTH_FILE", auth_file), patch.object(
            codex_core_mod, "CODEX_CONFIG_FILE", config_file
        ), patch.object(codex_usage_mod, "_fetch_codex_usage_once", side_effect=blocked_fetch), patch.object(
            codex_usage_mod, "refresh_codex_credentials"
        ) as usage_refresh, patch.object(codex_usage_mod, "keychain", shared_keychain), patch.object(
            codex_core_mod, "keychain", shared_keychain
        ), patch.object(codex_core_mod, "run_codex_logout"), patch.object(
            codex_core_mod, "run_codex_login", side_effect=blocked_login
        ):
            usage_thread = threading.Thread(target=lambda: fetch_active_codex_usage(config))
            usage_thread.start()
            assert usage_started.wait(timeout=2)

            def add_in_thread():
                try:
                    codex_core_mod.add_new_codex_account(config)
                except BaseException as exc:
                    add_errors.append(exc)

            add_thread = threading.Thread(target=add_in_thread)
            add_thread.start()
            assert login_started.wait(timeout=2)
            assert not auth_file.exists()
            release_usage.set()
            usage_thread.join(timeout=2)
            assert not auth_file.exists()
            release_login.set()
            add_thread.join(timeout=2)

        assert not usage_thread.is_alive()
        assert not add_thread.is_alive()
        assert not add_errors
        usage_refresh.assert_not_called()


@pytest.mark.parametrize("value, expected", [
    ("2026-03-19T12:00:00Z", "?"),
    (1773921600, "2h 0m"),
    ("1773921600", "2h 0m"),
    (None, "?"),
    ({}, "?"),
    ([], "?"),
    (12345, "now"),
    ("12345", "now"),
])
def test_reset_adapter_input_contract(value, expected):
    from claude_switcher.codex_usage import _format_reset_delta

    with patch("claude_switcher.codex_usage.datetime", wraps=datetime) as clock:
        clock.now.return_value = datetime(2026, 3, 19, 10, tzinfo=timezone.utc)
        assert _format_reset_delta(value) == expected


@pytest.mark.parametrize("seconds, expected", [
    (-1, "now"), (0, "now"), (1, "0m"), (59, "0m"), (60, "1m"),
    (3599, "59m"), (3600, "1h 0m"), (86399, "23h 59m"),
    (86400, "1d 0h"), (133200, "1d 13h"),
])
def test_reset_adapter_countdown_boundaries(seconds, expected):
    from claude_switcher.codex_usage import _format_reset_delta

    now = datetime(2026, 3, 19, 10, tzinfo=timezone.utc)
    target = now + timedelta(seconds=seconds)
    with patch("claude_switcher.codex_usage.datetime", wraps=datetime) as clock:
        clock.now.return_value = now
        assert _format_reset_delta(target.timestamp()) == expected


def test_null_reset_keeps_codex_unknown_countdown():
    usage = {"rate_limit": {"primary_window": {"used_percent": 3, "reset_at": None}}}
    state = codex_usage_state(usage)
    assert state.available is True
    assert state.display == "1h 3% (?)"
