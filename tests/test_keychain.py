import json
import subprocess
from unittest.mock import patch, MagicMock

import pytest

from claude_switcher.keychain import (
    KEYCHAIN_TIMEOUT_SECONDS,
    _single_line,
    read_credentials,
    restore_credentials,
    snapshot_credentials,
    write_credentials,
    delete_credentials,
)

FAKE_CREDS = json.dumps({"accessToken": "sk-ant-oat01-xxx", "refreshToken": "sk-ant-ort01-xxx"})


class TestReadCredentials:
    @patch("claude_switcher.keychain.subprocess.run")
    def test_read_existing_entry(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout=FAKE_CREDS)
        result = read_credentials("claude-switcher:emile@gmail.com")
        assert result == FAKE_CREDS
        mock_run.assert_called_once_with(
            ["security", "find-generic-password", "-s", "claude-switcher:emile@gmail.com", "-w"],
            capture_output=True,
            text=True,
            timeout=KEYCHAIN_TIMEOUT_SECONDS,
        )

    @patch("claude_switcher.keychain.subprocess.run")
    def test_read_missing_entry_returns_none(self, mock_run):
        mock_run.return_value = MagicMock(returncode=44, stdout="", stderr="not found")
        result = read_credentials("claude-switcher:missing@test.com")
        assert result is None


class TestWriteCredentials:
    @patch("claude_switcher.keychain.subprocess.run")
    def test_write_credentials_deletes_then_adds(self, mock_run):
        # First call: delete returns 0 (entry found), second delete: returncode!=0 (no more),
        # third call: add-generic-password returns 0
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout='"acct"<blob>="emilejouannet"\n'),
            MagicMock(returncode=0, stdout=FAKE_CREDS),
            MagicMock(returncode=0),
            MagicMock(returncode=44),
            MagicMock(returncode=0),
        ]
        write_credentials("claude-switcher:emile@gmail.com", "emilejouannet", FAKE_CREDS)
        assert mock_run.call_count == 5
        # Verify delete calls
        mock_run.assert_any_call(
            ["security", "delete-generic-password", "-s", "claude-switcher:emile@gmail.com"],
            capture_output=True,
            text=True,
            timeout=KEYCHAIN_TIMEOUT_SECONDS,
        )
        # Verify add call
        mock_run.assert_any_call(
            [
                "security", "add-generic-password",
                "-s", "claude-switcher:emile@gmail.com",
                "-a", "emilejouannet",
                "-X", FAKE_CREDS.encode("utf-8").hex(),
            ],
            capture_output=True,
            text=True,
            timeout=KEYCHAIN_TIMEOUT_SECONDS,
        )
        # plaintext secret never appears in any argument list
        assert all(FAKE_CREDS not in call.args[0] for call in mock_run.call_args_list)

    @patch("claude_switcher.keychain.subprocess.run")
    def test_write_no_existing_entry(self, mock_run):
        # No existing entry to delete, then add succeeds
        mock_run.side_effect = [
            MagicMock(returncode=44),
            MagicMock(returncode=44),
            MagicMock(returncode=0),
        ]
        write_credentials("claude-switcher:new@test.com", "newuser", FAKE_CREDS)
        assert mock_run.call_count == 3

    @patch("claude_switcher.keychain.subprocess.run")
    def test_write_failure_raises(self, mock_run):
        mock_run.side_effect = [
            MagicMock(returncode=44),
            MagicMock(returncode=44),
            MagicMock(returncode=1, stderr="permission denied"),
            MagicMock(returncode=44),
        ]
        with pytest.raises(RuntimeError, match="Keychain write failed"):
            write_credentials("claude-switcher:test@test.com", "testuser", FAKE_CREDS)

    @pytest.mark.parametrize("newline", ["\n", "\r\n"])
    @patch("claude_switcher.keychain.subprocess.run")
    def test_multiline_json_snapshot_is_normalized_and_restored(self, mock_run, newline):
        legacy = f'{{{newline}  "email": "josé@example.com"{newline}}}'
        normalized = json.dumps(json.loads(legacy))
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout='"acct"<blob>="legacy"\n'),
            MagicMock(returncode=0, stdout=legacy),
            MagicMock(returncode=0),
            MagicMock(returncode=44),
            MagicMock(returncode=1),
            MagicMock(returncode=44),
            MagicMock(returncode=0),
        ]

        with pytest.raises(RuntimeError, match="Keychain write failed"):
            write_credentials("service", "replacement", '{"new": true}')

        restore_call = mock_run.call_args_list[-1]
        assert restore_call.args[0] == [
            "security", "add-generic-password", "-s", "service",
            "-a", "legacy", "-X", normalized.encode("utf-8").hex(),
        ]
        assert all(legacy not in call.args[0] for call in mock_run.call_args_list)

    @patch("claude_switcher.keychain.subprocess.run")
    def test_add_failure_restores_exact_prior_pair(self, mock_run):
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout='"acct"<blob>="old-account"\n'),
            MagicMock(returncode=0, stdout="old-password"),
            MagicMock(returncode=0),
            MagicMock(returncode=44),
            MagicMock(returncode=1),
            MagicMock(returncode=44),
            MagicMock(returncode=0),
        ]

        with pytest.raises(RuntimeError, match="Keychain write failed"):
            write_credentials("service", "new-account", "new-password")

        assert mock_run.call_args_list[-1].args[0][-3:] == [
            "-a", "old-account", "-X", "old-password".encode("utf-8").hex()
        ][-3:]
        assert mock_run.call_args_list[-1].args[0][-2:] == ["-X", "old-password".encode("utf-8").hex()]

    @patch("claude_switcher.keychain.subprocess.run")
    def test_delete_timeout_after_removal_restores_snapshot(self, mock_run):
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout='"acct"<blob>="old-account"\n'),
            MagicMock(returncode=0, stdout="old-password"),
            subprocess.TimeoutExpired("security", 5),
            MagicMock(returncode=0),
        ]

        with pytest.raises(RuntimeError, match="delete timed out"):
            write_credentials("service", "new-account", "new-password")

        assert mock_run.call_args_list[-1].args[0][-2:] == ["-X", "old-password".encode("utf-8").hex()]

    @patch("claude_switcher.keychain.subprocess.run")
    def test_timed_out_add_without_snapshot_is_reconciled_to_empty(self, mock_run):
        mock_run.side_effect = [
            MagicMock(returncode=44),
            MagicMock(returncode=44),
            subprocess.TimeoutExpired("security", 5),
            MagicMock(returncode=0),
            MagicMock(returncode=44),
        ]

        with pytest.raises(RuntimeError, match="write timed out"):
            write_credentials("service", "new-account", "new-password")

        delete_calls = [
            call for call in mock_run.call_args_list
            if call.args[0][1] == "delete-generic-password"
        ]
        assert len(delete_calls) == 3

    @patch("claude_switcher.keychain.subprocess.run")
    def test_snapshot_failure_aborts_before_delete(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stderr="denied")

        with pytest.raises(RuntimeError, match="snapshot"):
            write_credentials("service", "account", "password")

        assert mock_run.call_count == 1
        assert mock_run.call_args.args[0][1] == "find-generic-password"

    @patch("claude_switcher.keychain.subprocess.run")
    def test_multiline_non_json_rejected_before_delete(self, mock_run):
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout='"acct"<blob>="old"\n'),
            MagicMock(returncode=0, stdout="old-password"),
        ]

        with pytest.raises(ValueError, match="single-line"):
            write_credentials("service", "new", "first\nsecond")

        assert mock_run.call_count == 2

    @patch("claude_switcher.keychain.subprocess.run")
    def test_unrestorable_multiline_snapshot_aborts_before_delete(self, mock_run):
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout='"acct"<blob>="old"\n'),
            MagicMock(returncode=0, stdout="first\nsecond"),
        ]

        with pytest.raises(ValueError, match="single-line"):
            write_credentials("service", "new", "new-password")

        assert mock_run.call_count == 2

    @patch("claude_switcher.keychain.subprocess.run")
    def test_delete_timeout_without_removal_does_not_reconcile_intact_entry(self, mock_run):
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout='"acct"<blob>="old"\n'),
            MagicMock(returncode=0, stdout="old-password"),
            subprocess.TimeoutExpired("security", 5),
            MagicMock(returncode=1, stderr="duplicate"),
        ]

        with pytest.raises(RuntimeError, match="delete timed out"):
            write_credentials("service", "new", "new-password")

        assert mock_run.call_count == 4
        assert sum(
            call.args[0][1] == "delete-generic-password"
            for call in mock_run.call_args_list
        ) == 1

    @patch("claude_switcher.keychain.subprocess.run")
    def test_non_ascii_secret_uses_explicit_utf8_transport(self, mock_run):
        secret = '{"email":"josé@example.com"}'
        mock_run.side_effect = [
            MagicMock(returncode=44),
            MagicMock(returncode=44),
            MagicMock(returncode=0),
        ]

        write_credentials("service", "account", secret)

        add_call = mock_run.call_args_list[-1]
        assert add_call.args[0][-2] == "-X"
        # hex round-trips the UTF-8 bytes; the -X argument is pure ASCII, so a C
        # locale cannot corrupt it, and the plaintext secret never appears.
        assert add_call.args[0][-1] == secret.encode("utf-8").hex()
        assert secret not in add_call.args[0]
        assert bytes.fromhex(add_call.args[0][-1]).decode("utf-8") == secret


class TestSnapshotAndRestore:
    @patch("claude_switcher.keychain.subprocess.run")
    def test_snapshot_reads_exact_account_and_password(self, mock_run):
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout='    "acct"<blob>="live-account"\n'),
            MagicMock(returncode=0, stdout="live-password\n"),
        ]

        assert snapshot_credentials("service") == ("live-account", "live-password")

    @patch("claude_switcher.keychain.subprocess.run")
    def test_restore_is_noop_when_target_pair_is_already_present(self, mock_run):
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout='"acct"<blob>="old"\n'),
            MagicMock(returncode=0, stdout="old-password"),
        ]

        restore_credentials("service", ("old", "old-password"))

        assert mock_run.call_count == 2
        assert all(call.args[0][1] == "find-generic-password" for call in mock_run.call_args_list)

    @patch("claude_switcher.keychain.subprocess.run")
    def test_restore_failure_does_not_replace_original_write_error(self, mock_run):
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout='"acct"<blob>="old"\n'),
            MagicMock(returncode=0, stdout="old-password"),
            MagicMock(returncode=0),
            MagicMock(returncode=44),
            MagicMock(returncode=1),
            MagicMock(returncode=44),
            MagicMock(returncode=1),
        ]

        with pytest.raises(RuntimeError, match="Keychain write failed") as caught:
            write_credentials("service", "new", "new-password")

        assert caught.value.__cause__ is not None


class TestSingleLine:
    def test_multiline_non_json_value_raises(self):
        with pytest.raises(ValueError, match="single-line"):
            _single_line("first\nsecond")


class TestDeleteCredentials:
    @patch("claude_switcher.keychain.subprocess.run")
    def test_delete_existing(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        result = delete_credentials("claude-switcher:emile@gmail.com")
        assert result is True

    @patch("claude_switcher.keychain.subprocess.run")
    def test_delete_missing_returns_false(self, mock_run):
        mock_run.return_value = MagicMock(returncode=44)
        result = delete_credentials("claude-switcher:missing@test.com")
        assert result is False

    @patch("claude_switcher.keychain.subprocess.run")
    def test_delete_non_not_found_failure_raises(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stderr="denied")
        with pytest.raises(RuntimeError, match="delete failed"):
            delete_credentials("service")

    @patch("claude_switcher.keychain.subprocess.run")
    def test_delete_timeout_raises(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired("security", 5)
        with pytest.raises(RuntimeError, match="delete timed out"):
            delete_credentials("service")


class TestReadAccountAttribute:
    @patch("claude_switcher.keychain.subprocess.run")
    def test_read_account_attribute(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='    "acct"<blob>="emilejouannet"\n',
            stderr='',
        )
        from claude_switcher.keychain import read_account_attribute
        result = read_account_attribute("Claude Code-credentials")
        assert result == "emilejouannet"


import os
import shutil
import subprocess as _sp

import pytest as _pytest

_SECURITY = shutil.which("security")


@_pytest.mark.skipif(
    _SECURITY is None or os.uname().sysname != "Darwin",
    reason="requires the real macOS `security` binary",
)
class TestRealSecurityTransport:
    """Integration tests against the ACTUAL `security` binary in an isolated
    keychain. These exist because the original stdin (`-w` prompt) transport
    passed every mock-based test yet truncated real credentials to 128 bytes on
    macOS, corrupting logins. A stub can hide that; the real binary cannot.
    """

    def _mk_keychain(self, tmp_path):
        kc = str(tmp_path / "cs-itest.keychain-db")
        _sp.run([_SECURITY, "create-keychain", "-p", "t", kc], check=True,
                capture_output=True)
        _sp.run([_SECURITY, "unlock-keychain", "-p", "t", kc], check=True,
                capture_output=True)
        return kc

    def test_hex_transport_roundtrips_a_long_blob(self, tmp_path):
        kc = self._mk_keychain(tmp_path)
        # ~3200 bytes: larger than a real Claude Code credential, far over 128.
        blob = '{"claudeAiOauth":{"accessToken":"sk-ant-oat01-%s","refreshToken":"rt-%s"}}' % ("A" * 3000, "B" * 40)
        assert len(blob) > 3000
        try:
            # exactly the argv the code builds, plus an isolated keychain positional
            _sp.run([_SECURITY, "add-generic-password", "-s", "cs-itest", "-a", "me",
                     "-X", blob.encode("utf-8").hex(), kc],
                    check=True, capture_output=True, text=True)
            got = _sp.run([_SECURITY, "find-generic-password", "-s", "cs-itest", "-w", kc],
                          capture_output=True, text=True).stdout.strip()
            assert got == blob, "hex -X transport must round-trip a >3000-byte credential intact"
        finally:
            _sp.run([_SECURITY, "delete-keychain", kc], capture_output=True)

    def test_stdin_prompt_transport_truncates(self, tmp_path):
        # Documents WHY the code does not use the -w prompt: the real binary
        # truncates it. If a future macOS ever fixes this, this test flips and we
        # can revisit; until then it guards against anyone "restoring" the -w path.
        kc = self._mk_keychain(tmp_path)
        blob = "X" * 500
        try:
            _sp.run([_SECURITY, "add-generic-password", "-s", "cs-trunc", "-a", "me", "-w", kc],
                    input="%s\n%s\n" % (blob, blob), capture_output=True, text=True)
            got = _sp.run([_SECURITY, "find-generic-password", "-s", "cs-trunc", "-w", kc],
                          capture_output=True, text=True).stdout.strip()
            assert got != blob, "the -w prompt transport is expected to corrupt a 500-byte secret"
        finally:
            _sp.run([_SECURITY, "delete-keychain", kc], capture_output=True)
