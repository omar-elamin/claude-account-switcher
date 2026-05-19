"""Tests for Codex CLI business logic."""

import base64
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import claude_switcher.codex_core as codex_core_mod
from claude_switcher.codex_core import (
    CODEX_KEYRING_UNSUPPORTED_MESSAGE,
    add_new_codex_account,
    check_codex_cli,
    get_codex_auth_status,
    read_codex_credentials,
    import_current_codex_account,
    run_codex_login,
    switch_codex_account,
    remove_codex_account,
)
from claude_switcher.config import AccountInfo, load_accounts, save_accounts


def _jwt(payload):
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"header.{encoded}.sig"


def _auth_json(email="user@test.com", plan="plus"):
    return json.dumps({
        "auth_mode": "chatgpt",
        "tokens": {
            "access_token": "sk-test",
            "account_id": "acc-123",
            "refresh_token": "refresh",
            "id_token": _jwt({
                "email": email,
                "https://api.openai.com/auth": {
                    "chatgpt_plan_type": plan,
                    "chatgpt_account_id": "acc-123",
                },
            }),
        },
    })


class TestCodexCLI:
    @patch("claude_switcher.codex_core.shutil.which", return_value="/usr/local/bin/codex")
    def test_check_cli_found(self, mock_which):
        assert check_codex_cli() is True

    @patch("claude_switcher.codex_core.Path.is_file", return_value=False)
    @patch("claude_switcher.codex_core.shutil.which", return_value=None)
    def test_check_cli_not_found_in_path(self, mock_which, mock_is_file):
        assert check_codex_cli() is False

    @patch("claude_switcher.codex_core.subprocess.run")
    def test_get_auth_status_logged_in(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="Logged in using ChatGPT", stderr="")
        status = get_codex_auth_status()
        assert status is not None
        assert status["loggedIn"] is True

    @patch("claude_switcher.codex_core.subprocess.run")
    def test_get_auth_status_not_logged_in(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="")
        assert get_codex_auth_status() is None


class TestCodexCredentials:
    def test_read_from_auth_file(self, tmp_path):
        auth_file = tmp_path / "auth.json"
        config_file = tmp_path / "config.toml"
        auth_file.write_text('{"token": "sk-test-123"}')
        config_file.write_text('cli_auth_credentials_store = "file"')

        with patch.object(codex_core_mod, "CODEX_AUTH_FILE", auth_file), patch.object(
            codex_core_mod, "CODEX_CONFIG_FILE", config_file
        ):
            creds = read_codex_credentials()

        assert creds is not None
        assert "sk-test-123" in creds

    def test_read_missing_file_returns_none_in_file_mode(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text('cli_auth_credentials_store = "file"')

        with patch.object(codex_core_mod, "CODEX_AUTH_FILE", tmp_path / "missing.json"), patch.object(
            codex_core_mod, "CODEX_CONFIG_FILE", config_file
        ):
            assert read_codex_credentials() is None

    def test_keyring_mode_raises_clear_error(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text('cli_auth_credentials_store = "keyring"')

        with patch.object(codex_core_mod, "CODEX_CONFIG_FILE", config_file):
            with pytest.raises(RuntimeError, match="keyring credential storage"):
                read_codex_credentials()
        assert "cli_auth_credentials_store" in CODEX_KEYRING_UNSUPPORTED_MESSAGE


class TestImportCodexAccount:
    @patch("claude_switcher.codex_core.keychain")
    @patch("claude_switcher.codex_core.get_codex_auth_status")
    def test_import_success(self, mock_status, mock_kc, tmp_path):
        auth_file = tmp_path / "auth.json"
        config_file = tmp_path / "config.toml"
        auth_file.write_text(_auth_json(email="user@test.com", plan="pro"))
        config_file.write_text('cli_auth_credentials_store = "file"')
        mock_status.return_value = {"loggedIn": True}

        with patch.object(codex_core_mod, "CODEX_AUTH_FILE", auth_file), patch.object(
            codex_core_mod, "CODEX_CONFIG_FILE", config_file
        ):
            result = import_current_codex_account(tmp_path / "accounts.json")

        assert result is not None
        assert result.email == "user@test.com"
        assert result.subscription_type == "pro"
        assert result.provider == "codex"
        mock_kc.write_credentials.assert_called_once()

    def test_import_no_credentials(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text('cli_auth_credentials_store = "file"')

        with patch.object(codex_core_mod, "CODEX_AUTH_FILE", tmp_path / "missing.json"), patch.object(
            codex_core_mod, "CODEX_CONFIG_FILE", config_file
        ):
            assert import_current_codex_account(tmp_path / "accounts.json") is None

    def test_import_no_auto_credentials_returns_none(self, tmp_path):
        with patch.object(codex_core_mod, "CODEX_AUTH_FILE", tmp_path / "missing.json"), patch.object(
            codex_core_mod, "CODEX_CONFIG_FILE", tmp_path / "missing.toml"
        ):
            assert import_current_codex_account(tmp_path / "accounts.json") is None


class TestCodexLogin:
    @patch("claude_switcher.codex_core._launch_codex_login_terminal")
    @patch("claude_switcher.codex_core.time.sleep")
    def test_run_codex_login_waits_for_auth_file(self, mock_sleep, mock_launch):
        with patch(
            "claude_switcher.codex_core._read_codex_credentials_from_file",
            side_effect=[None, _auth_json(email="new@test.com")],
        ):
            assert run_codex_login(timeout=3) is True
        mock_launch.assert_called_once()

    @patch("claude_switcher.codex_core.subprocess.run")
    @patch("claude_switcher.codex_core._codex_cmd", return_value="/opt/homebrew/bin/codex")
    def test_launch_codex_login_opens_terminal(self, mock_cmd, mock_run, tmp_path):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        with patch("claude_switcher.codex_core.tempfile.gettempdir", return_value=str(tmp_path)):
            codex_core_mod._launch_codex_login_terminal()

        script_path = tmp_path / "claude-switcher-codex-login-{}.command".format(
            codex_core_mod.os.getpid()
        )
        assert script_path.exists()
        assert 'cli_auth_credentials_store="file"' in script_path.read_text()
        mock_run.assert_called_once()
        assert mock_run.call_args.args[0][0] == "open"

    @patch("claude_switcher.codex_core.import_current_codex_account")
    @patch("claude_switcher.codex_core.run_codex_login")
    @patch("claude_switcher.codex_core.run_codex_logout")
    def test_add_new_account_opens_login_when_no_current_file(
        self, mock_logout, mock_login, mock_import, tmp_path
    ):
        config = tmp_path / "accounts.json"
        mock_login.return_value = True
        mock_import.return_value = AccountInfo(
            "new@test.com", "plus", "", True, "new@test.com", provider="codex"
        )

        with patch.object(codex_core_mod, "CODEX_AUTH_FILE", tmp_path / "missing.json"), patch.object(
            codex_core_mod, "CODEX_CONFIG_FILE", tmp_path / "missing.toml"
        ):
            result = add_new_codex_account(config)

        assert result.email == "new@test.com"
        mock_logout.assert_called_once()
        mock_login.assert_called_once()

    @patch("claude_switcher.codex_core._write_codex_credentials")
    @patch("claude_switcher.codex_core.run_codex_login")
    @patch("claude_switcher.codex_core.run_codex_logout")
    @patch("claude_switcher.codex_core.keychain")
    def test_add_new_account_restores_saved_creds_when_cancelled(
        self, mock_kc, mock_logout, mock_login, mock_write, tmp_path
    ):
        config = tmp_path / "accounts.json"
        config_file = tmp_path / "config.toml"
        auth_file = tmp_path / "auth.json"
        config_file.write_text('cli_auth_credentials_store = "file"')
        save_accounts([
            AccountInfo("old@test.com", "plus", "", True, "old@test.com", provider="codex")
        ], config)
        mock_kc.read_credentials.return_value = _auth_json(email="old@test.com")
        mock_login.return_value = False

        with patch.object(codex_core_mod, "CODEX_AUTH_FILE", auth_file), patch.object(
            codex_core_mod, "CODEX_CONFIG_FILE", config_file
        ):
            result = add_new_codex_account(config)

        assert result is None
        mock_write.assert_called_once_with(_auth_json(email="old@test.com"))


class TestSwitchCodexAccount:
    @patch("claude_switcher.codex_core._write_codex_credentials")
    @patch("claude_switcher.codex_core.keychain")
    def test_switch_saves_current_loads_target(self, mock_kc, mock_write, tmp_path):
        config = tmp_path / "accounts.json"
        auth_file = tmp_path / "auth.json"
        config_file = tmp_path / "config.toml"
        auth_file.write_text('{"token": "old-token"}')
        config_file.write_text('cli_auth_credentials_store = "file"')
        save_accounts([
            AccountInfo("old@test.com", "plus", "", True, "old", provider="codex"),
            AccountInfo("new@test.com", "plus", "", False, "new", provider="codex"),
        ], config)
        mock_kc.read_credentials.return_value = '{"token": "target-token"}'

        with patch.object(codex_core_mod, "CODEX_AUTH_FILE", auth_file), patch.object(
            codex_core_mod, "CODEX_CONFIG_FILE", config_file
        ):
            switch_codex_account("new@test.com", config)

        mock_kc.write_credentials.assert_any_call(
            "codex-switcher:old@test.com", "old", '{"token": "old-token"}'
        )
        mock_write.assert_called_once_with('{"token": "target-token"}')
        active = [a for a in load_accounts(config) if a.provider == "codex" and a.active]
        assert active[0].email == "new@test.com"

    @patch("claude_switcher.codex_core.keychain")
    def test_switch_missing_keychain_raises(self, mock_kc, tmp_path):
        config = tmp_path / "accounts.json"
        save_accounts([AccountInfo("new@test.com", "plus", "", False, "new", provider="codex")], config)
        mock_kc.read_credentials.return_value = None

        with pytest.raises(RuntimeError, match="Credentials not found"):
            switch_codex_account("new@test.com", config)


class TestRemoveCodexAccount:
    @patch("claude_switcher.codex_core.keychain")
    def test_remove_account(self, mock_kc, tmp_path):
        config = tmp_path / "accounts.json"
        save_accounts([AccountInfo("rm@test.com", "plus", "", False, "rm", provider="codex")], config)

        remove_codex_account("rm@test.com", config)

        mock_kc.delete_credentials.assert_called_with("codex-switcher:rm@test.com")
        assert load_accounts(config) == []
