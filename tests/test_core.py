import json
import threading
from unittest.mock import patch, MagicMock

import pytest

import claude_switcher.core as core_mod

from claude_switcher.core import (
    check_claude_cli,
    get_auth_status,
    import_current_account,
    switch_account,
    add_new_account,
    remove_saved_account,
)
from claude_switcher.config import AccountInfo


class TestClaudeCLI:
    @patch("claude_switcher.core.shutil.which")
    def test_check_cli_found(self, mock_which):
        mock_which.return_value = "/usr/local/bin/claude"
        assert check_claude_cli() is True

    @patch("claude_switcher.core.Path.is_file", return_value=False)
    @patch("claude_switcher.core.shutil.which", return_value=None)
    def test_check_cli_not_found(self, mock_which, mock_is_file):
        assert check_claude_cli() is False

    @patch("claude_switcher.core.subprocess.run")
    def test_get_auth_status(self, mock_run):
        status = {"loggedIn": True, "email": "test@test.com", "subscriptionType": "pro", "orgName": "Org"}
        mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(status))
        result = get_auth_status()
        assert result["email"] == "test@test.com"

    @patch("claude_switcher.core.subprocess.run")
    def test_get_auth_status_failure_returns_none(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        result = get_auth_status()
        assert result is None


class TestImportCurrentAccount:
    @patch("claude_switcher.core._read_oauth_account")
    @patch("claude_switcher.core.get_auth_status")
    @patch("claude_switcher.core.keychain")
    def test_import_success(self, mock_kc, mock_status, mock_oauth, tmp_path):
        mock_status.return_value = {"email": "test@test.com", "subscriptionType": "pro", "orgName": "Org"}
        mock_kc.read_credentials.return_value = '{"accessToken":"tok"}'
        mock_kc.read_account_attribute.return_value = "testuser"
        mock_oauth.return_value = {"emailAddress": "test@test.com"}

        config_path = tmp_path / "accounts.json"
        result = import_current_account(config_path)

        assert result is not None
        assert result.email == "test@test.com"
        assert result.oauth_account == {"emailAddress": "test@test.com"}
        mock_kc.write_credentials.assert_called_once_with(
            "claude-switcher:test@test.com", "testuser", '{"accessToken":"tok"}'
        )

    @patch("claude_switcher.core.get_auth_status")
    @patch("claude_switcher.core.keychain")
    def test_import_no_credentials(self, mock_kc, mock_status, tmp_path):
        mock_kc.read_credentials.return_value = None
        result = import_current_account(tmp_path / "accounts.json")
        assert result is None


class TestSwitchAccount:
    @patch("claude_switcher.core._write_oauth_account")
    @patch("claude_switcher.core._read_oauth_account")
    @patch("claude_switcher.core.keychain")
    def test_switch_saves_current_then_loads_target(self, mock_kc, mock_read_oauth, mock_write_oauth, tmp_path):
        config_path = tmp_path / "accounts.json"
        from claude_switcher.config import add_account, AccountInfo
        add_account(AccountInfo("a@test.com", "pro", "Org A", True, "usera"), config_path)
        add_account(AccountInfo("b@test.com", "pro", "Org B", False, "userb",
                                oauth_account={"emailAddress": "b@test.com"}), config_path)

        mock_kc.read_credentials.side_effect = [
            '{"accessToken":"refreshed-a"}',
            '{"accessToken":"tok-b"}',
        ]
        mock_read_oauth.return_value = {"emailAddress": "a@test.com"}

        switch_account("b@test.com", config_path)

        mock_kc.write_credentials.assert_any_call(
            "claude-switcher:a@test.com", "usera", '{"accessToken":"refreshed-a"}'
        )
        mock_kc.write_credentials.assert_any_call(
            "Claude Code-credentials", "userb", '{"accessToken":"tok-b"}'
        )
        mock_write_oauth.assert_called_once_with({"emailAddress": "b@test.com"})

    @patch("claude_switcher.core._write_oauth_account")
    @patch("claude_switcher.core._read_oauth_account")
    @patch("claude_switcher.core.keychain")
    def test_switch_missing_keychain_entry_raises(self, mock_kc, mock_read_oauth, mock_write_oauth, tmp_path):
        config_path = tmp_path / "accounts.json"
        from claude_switcher.config import add_account, AccountInfo
        add_account(AccountInfo("a@test.com", "pro", "Org", True, "u"), config_path)
        add_account(AccountInfo("b@test.com", "pro", "Org", False, "u"), config_path)
        mock_kc.read_credentials.side_effect = ['{"tok":"a"}', None]
        mock_read_oauth.return_value = {"emailAddress": "a@test.com"}

        try:
            switch_account("b@test.com", config_path)
            assert False, "Should have raised"
        except RuntimeError as e:
            assert "not found" in str(e).lower()


class TestAddNewAccount:
    @patch("claude_switcher.core._read_oauth_account")
    @patch("claude_switcher.core.get_auth_status")
    @patch("claude_switcher.core.run_auth_login")
    @patch("claude_switcher.core.run_auth_logout")
    @patch("claude_switcher.core.keychain")
    def test_add_account_full_flow(self, mock_kc, mock_logout, mock_login, mock_status, mock_oauth, tmp_path):
        config_path = tmp_path / "accounts.json"
        from claude_switcher.config import add_account, AccountInfo
        add_account(AccountInfo("a@test.com", "pro", "Org A", True, "usera"), config_path)

        mock_kc.snapshot_credentials.return_value = ("usera", '{"accessToken":"tok-a"}')
        mock_kc._single_line.side_effect = lambda value: value
        mock_kc.read_credentials.return_value = '{"accessToken":"tok-new"}'
        mock_kc.read_account_attribute.side_effect = ["newuser"]
        mock_kc.delete_credentials.return_value = False
        mock_login.return_value = True
        mock_status.return_value = {"email": "new@test.com", "subscriptionType": "pro", "orgName": "New Org"}
        mock_oauth.return_value = {"emailAddress": "new@test.com"}

        result = add_new_account(config_path)
        assert result is not None
        assert result.email == "new@test.com"
        # Must NOT call `claude auth logout`: that would revoke the previous
        # account server-side and invalidate its just-saved backup.
        mock_logout.assert_not_called()
        # The local slot is still cleared before the fresh login.
        assert mock_kc.delete_credentials.called
        mock_login.assert_called_once()

    @patch("claude_switcher.core.run_auth_login")
    @patch("claude_switcher.core.run_auth_logout")
    @patch("claude_switcher.core.keychain")
    def test_add_account_login_cancelled(self, mock_kc, mock_logout, mock_login, tmp_path):
        config_path = tmp_path / "accounts.json"
        mock_kc.snapshot_credentials.return_value = None
        mock_kc.delete_credentials.return_value = False
        mock_login.return_value = False

        result = add_new_account(config_path)
        assert result is None

    def test_cancelled_add_without_active_config_restores_live_snapshot(self, tmp_path):
        snapshot = ("live-account", '{"accessToken":"live"}')
        with patch.object(core_mod, "keychain") as mock_kc, patch.object(
            core_mod, "run_auth_logout"
        ), patch.object(core_mod, "run_auth_login", return_value=False):
            mock_kc.snapshot_credentials.return_value = snapshot
            mock_kc._single_line.side_effect = lambda value: value
            mock_kc.delete_credentials.return_value = False

            assert add_new_account(tmp_path / "accounts.json") is None

        mock_kc.restore_credentials.assert_called_once_with(
            core_mod.CLAUDE_SERVICE, snapshot
        )

    @pytest.mark.parametrize("outcome", ["login-false", "import-none", "import-raises"])
    def test_all_failed_add_outcomes_restore_exact_live_pair(self, tmp_path, outcome):
        from claude_switcher.config import add_account

        config_path = tmp_path / "accounts.json"
        add_account(
            AccountInfo("old@test.com", "pro", "", True, "config-account"),
            config_path,
        )
        snapshot = ("live-account", '{"accessToken":"live"}')

        with patch.object(core_mod, "keychain") as mock_kc, patch.object(
            core_mod, "run_auth_logout"
        ), patch.object(core_mod, "run_auth_login") as mock_login, patch.object(
            core_mod, "import_current_account"
        ) as mock_import:
            mock_kc.snapshot_credentials.return_value = snapshot
            mock_kc._single_line.side_effect = lambda value: value
            mock_kc.delete_credentials.return_value = False
            mock_login.return_value = outcome != "login-false"
            if outcome == "import-none":
                mock_import.return_value = None
            elif outcome == "import-raises":
                mock_import.side_effect = RuntimeError("import failed")

            if outcome == "import-raises":
                with pytest.raises(RuntimeError, match="import failed"):
                    add_new_account(config_path)
            else:
                assert add_new_account(config_path) is None

        mock_kc.write_credentials.assert_called_once_with(
            "claude-switcher:old@test.com", "live-account", snapshot[1]
        )
        mock_kc.restore_credentials.assert_called_once_with(
            core_mod.CLAUDE_SERVICE, snapshot
        )
        mock_kc.read_credentials.assert_not_called()

    def test_login_exception_after_delete_restores_snapshot(self, tmp_path):
        snapshot = ("live-account", "live-password")
        with patch.object(core_mod, "keychain") as mock_kc, patch.object(
            core_mod, "run_auth_logout"
        ), patch.object(
            core_mod, "run_auth_login", side_effect=PermissionError("claude denied")
        ):
            mock_kc.snapshot_credentials.return_value = snapshot
            mock_kc._single_line.side_effect = lambda value: value
            mock_kc.delete_credentials.return_value = False

            with pytest.raises(PermissionError, match="claude denied"):
                add_new_account(tmp_path / "accounts.json")

        mock_kc.restore_credentials.assert_called_once_with(
            core_mod.CLAUDE_SERVICE, snapshot
        )

    def test_unrestorable_live_value_aborts_before_logout(self, tmp_path):
        with patch.object(core_mod, "keychain") as mock_kc, patch.object(
            core_mod, "run_auth_logout"
        ) as mock_logout:
            mock_kc.snapshot_credentials.return_value = ("live", "first\nsecond")
            mock_kc._single_line.side_effect = ValueError("must be single-line JSON")

            with pytest.raises(ValueError, match="single-line"):
                add_new_account(tmp_path / "accounts.json")

        mock_logout.assert_not_called()
        mock_kc.delete_credentials.assert_not_called()

    def test_restore_failure_re_raises_original_add_exception(self, tmp_path):
        original = PermissionError("login binary denied")
        restore_error = RuntimeError("restore failed")
        with patch.object(core_mod, "keychain") as mock_kc, patch.object(
            core_mod, "run_auth_logout"
        ), patch.object(core_mod, "run_auth_login", side_effect=original):
            mock_kc.snapshot_credentials.return_value = ("live", "password")
            mock_kc._single_line.side_effect = lambda value: value
            mock_kc.delete_credentials.return_value = False
            mock_kc.restore_credentials.side_effect = restore_error

            with pytest.raises(PermissionError, match="login binary denied") as caught:
                add_new_account(tmp_path / "accounts.json")

        assert caught.value is original
        assert caught.value.__cause__ is restore_error

    def test_claude_add_lease_refuses_switch_and_removal(self, tmp_path):
        from claude_switcher.config import add_account, load_accounts

        config_path = tmp_path / "accounts.json"
        add_account(AccountInfo("a@test.com", "pro", "", True, "a"), config_path)
        add_account(AccountInfo("b@test.com", "pro", "", False, "b"), config_path)
        login_started = threading.Event()
        release_login = threading.Event()
        errors = []

        def blocked_login():
            login_started.set()
            assert release_login.wait(timeout=2)
            return False

        with patch.object(core_mod, "keychain") as mock_kc, patch.object(
            core_mod, "run_auth_logout"
        ), patch.object(core_mod, "run_auth_login", side_effect=blocked_login):
            snapshot = ("live-a", "password-a")
            mock_kc.snapshot_credentials.return_value = snapshot
            mock_kc._single_line.side_effect = lambda value: value
            mock_kc.delete_credentials.return_value = False

            def add_in_thread():
                try:
                    add_new_account(config_path)
                except BaseException as exc:
                    errors.append(exc)

            thread = threading.Thread(target=add_in_thread)
            thread.start()
            assert login_started.wait(timeout=2)

            with pytest.raises(RuntimeError, match="add.*progress"):
                switch_account("b@test.com", config_path)
            with pytest.raises(RuntimeError, match="add.*progress"):
                remove_saved_account("b@test.com", config_path)

            release_login.set()
            thread.join(timeout=2)

        assert not errors
        assert not thread.is_alive()
        assert core_mod.get_active_account(config_path).email == "a@test.com"
        assert {account.email for account in load_accounts(config_path)} == {
            "a@test.com", "b@test.com"
        }
        mock_kc.restore_credentials.assert_called_once_with(
            core_mod.CLAUDE_SERVICE, snapshot
        )


class TestRemoveSavedAccount:
    @patch("claude_switcher.core.keychain")
    def test_remove_account(self, mock_kc, tmp_path):
        config_path = tmp_path / "accounts.json"
        from claude_switcher.config import add_account, AccountInfo
        add_account(AccountInfo("a@test.com", "pro", "Org", False, "u"), config_path)

        remove_saved_account("a@test.com", config_path)

        mock_kc.delete_credentials.assert_called_once_with("claude-switcher:a@test.com")
        from claude_switcher.config import load_accounts
        assert len(load_accounts(config_path)) == 0

    @patch("claude_switcher.core.remove_account", side_effect=OSError("config write failed"))
    @patch("claude_switcher.core.keychain")
    def test_remove_restores_snapshot_when_config_update_fails(
        self, mock_kc, mock_remove, tmp_path
    ):
        snapshot = ("saved-account", "saved-password")
        mock_kc.snapshot_credentials.return_value = snapshot
        mock_kc._single_line.side_effect = lambda value: value

        with pytest.raises(OSError, match="config write failed"):
            remove_saved_account("a@test.com", tmp_path / "accounts.json")

        mock_kc.restore_credentials.assert_called_once_with(
            "claude-switcher:a@test.com", snapshot
        )


class TestCoreWithMixedProviders:
    @patch("claude_switcher.core._write_oauth_account")
    @patch("claude_switcher.core._read_oauth_account")
    @patch("claude_switcher.core.keychain")
    def test_switch_claude_ignores_codex_account_with_same_email(
        self, mock_kc, mock_read_oauth, mock_write_oauth, tmp_path
    ):
        config_path = tmp_path / "accounts.json"
        from claude_switcher.config import add_account, load_accounts

        add_account(
            AccountInfo("user@test.com", "pro", "", True, "claude-user", provider="claude"),
            config_path,
        )
        add_account(
            AccountInfo(
                "other@test.com",
                "pro",
                "",
                False,
                "claude-other",
                oauth_account={"emailAddress": "other@test.com"},
                provider="claude",
            ),
            config_path,
        )
        add_account(
            AccountInfo("user@test.com", "plus", "", True, "codex-user", provider="codex"),
            config_path,
        )
        mock_kc.read_credentials.side_effect = ['{"token":"current"}', '{"token":"target"}']
        mock_read_oauth.return_value = {"emailAddress": "user@test.com"}

        switch_account("other@test.com", config_path)

        accounts = load_accounts(config_path)
        codex = next(a for a in accounts if a.provider == "codex")
        claude_target = next(a for a in accounts if a.email == "other@test.com")
        assert codex.active is True
        assert claude_target.active is True
        mock_write_oauth.assert_called_once_with({"emailAddress": "other@test.com"})


class TestClaudeSwitchIdentityGuard:
    @patch("claude_switcher.core.set_active_account")
    @patch("claude_switcher.core._write_oauth_account")
    @patch("claude_switcher.core._read_oauth_account")
    @patch("claude_switcher.core.keychain")
    def test_switch_does_not_backup_when_live_oauth_differs(
        self, mock_kc, mock_read_oauth, mock_write_oauth, mock_set_active, tmp_path
    ):
        from claude_switcher.config import add_account, AccountInfo
        config_path = tmp_path / "accounts.json"
        add_account(AccountInfo("A@test.com", "pro", "Org", True, "uA", oauth_account={"emailAddress": "A@test.com"}), config_path)
        add_account(AccountInfo("C@test.com", "pro", "Org", False, "uC", oauth_account={"emailAddress": "C@test.com"}), config_path)
        # config active = A, but live ~/.claude.json oauthAccount is B
        mock_read_oauth.return_value = {"emailAddress": "B@test.com"}
        mock_kc.read_credentials.return_value = '{"claudeAiOauth":{"accessToken":"tok"}}'

        switch_account("C@test.com", config_path)

        for call in mock_kc.write_credentials.call_args_list:
            assert call.args[0] != "claude-switcher:A@test.com", (
                "clobbered A's backup with a non-A credential"
            )
