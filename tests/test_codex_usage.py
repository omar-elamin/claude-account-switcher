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


class TestWindowLabelFromLength:
    """Codex windows are labelled by their real length, not by position."""

    def _usage(self, primary_secs, secondary=None):
        rl = {"primary_window": {"used_percent": 39, "limit_window_seconds": primary_secs,
                                 "reset_at": 4102444800}}
        if secondary is not None:
            rl["secondary_window"] = secondary
        return {"rate_limit": rl}

    def test_seven_day_primary_window_is_labelled_7d_not_1h(self):
        # Live API on Plus/Team plans: primary_window.limit_window_seconds == 604800.
        st = codex_usage_state(self._usage(604800))
        assert st.display.startswith("7d 39%")
        assert st.windows[0].label == "7d"

    def test_five_hour_and_one_hour_windows(self):
        assert codex_usage_state(self._usage(18000)).windows[0].label == "5h"
        assert codex_usage_state(self._usage(3600)).windows[0].label == "1h"

    def test_missing_or_bad_length_keeps_positional_fallback(self):
        u = self._usage(604800); del u["rate_limit"]["primary_window"]["limit_window_seconds"]
        assert codex_usage_state(u).windows[0].label == "1h"
        u = self._usage("weekly")
        assert codex_usage_state(u).windows[0].label == "1h"
        u = self._usage(0)
        assert codex_usage_state(u).windows[0].label == "1h"
        st = codex_usage_state(self._usage(3600, secondary={"used_percent": 5}))
        assert [w.label for w in st.windows] == ["1h", "7d"]

    def test_sub_hour_window(self):
        assert codex_usage_state(self._usage(1800)).windows[0].label == "30m"


@pytest.mark.parametrize("count", [0, 1, 4])
def test_reset_credit_fields_and_suffix(count):
    state = codex_usage_state({
        "rate_limit": {"primary_window": {"used_percent": 100, "limit_window_seconds": 604800}},
        "rate_limit_reset_credits": {"available_count": count, "applicable_available_count": count},
    })
    assert (state.reset_credits, state.reset_applicable) == (count, count)
    assert state.display == "7d 100%" + (f" · {count} reset" + ("s" if count != 1 else "") if count else "")


@pytest.mark.parametrize("credits", [None, [], "bad", 3, {},
    {"available_count": "2", "applicable_available_count": 1.5},
    {"available_count": True, "applicable_available_count": False}])
def test_malformed_reset_credits_are_zero(credits):
    state = codex_usage_state({"rate_limit": {"primary_window": {"used_percent": 10}},
                               "rate_limit_reset_credits": credits})
    assert (state.reset_credits, state.reset_applicable) == (0, 0)
    assert state.display == "1h 10%"


@pytest.mark.parametrize("usage", [None, {}, {"error": {"code": "login_required"}}, {"rate_limit": {}}])
def test_unavailable_usage_has_no_reset_credits(usage):
    if usage is not None:
        usage = dict(usage, rate_limit_reset_credits={"available_count": 2, "applicable_available_count": 2})
    state = codex_usage_state(usage)
    assert (state.reset_credits, state.reset_applicable) == (0, 0)
    assert "reset" not in state.display


@pytest.fixture
def reset_transport(tmp_path):
    from types import SimpleNamespace
    config = tmp_path / "accounts.json"
    account = AccountInfo("user@test.com", "plus", "", False, "user", provider="codex")
    save_accounts([account], config)
    usage = {"rate_limit_reset_credits": {"available_count": 3, "applicable_available_count": 2}}
    response = MagicMock()
    response.__enter__.return_value = response
    response.read.return_value = b'{"code":"reset","windows_reset":2}'
    with patch.object(codex_usage_mod, "fetch_codex_usage_for_account", return_value=usage) as saved, \
         patch.object(codex_usage_mod, "fetch_active_codex_usage", return_value=usage) as active, \
         patch.object(codex_usage_mod.keychain, "read_credentials", return_value=FAKE_CREDS_NESTED) as read, \
         patch.object(codex_core_mod, "_read_codex_credentials_for_import_raw", return_value=FAKE_CREDS_NESTED) as live, \
         patch.object(codex_usage_mod, "urlopen", return_value=response) as post:
        yield SimpleNamespace(config=config, account=account, usage=usage, response=response,
                              saved=saved, active=active, read=read, live=live, post=post)


@pytest.mark.parametrize("active", [False, True])
@pytest.mark.parametrize("code", ["reset", "nothing_to_reset", "no_credit", "already_redeemed",
                                  "RESET", "Nothing_To_Reset", "NO_CREDIT", "Already_Redeemed"])
def test_consume_reset_contract(reset_transport, active, code, caplog):
    from uuid import UUID
    t = reset_transport
    t.account.active = active
    save_accounts([t.account], t.config)
    t.response.read.return_value = json.dumps({"code": code, "windows_reset": 2}).encode()
    with caplog.at_level("INFO"):
        assert codex_usage_mod.consume_reset_credit(t.account.email, t.config) == code.lower()
    selected, unused = (t.active, t.saved) if active else (t.saved, t.active)
    selected.assert_called_once_with(*((t.config,) if active else (t.account.email, t.config)))
    unused.assert_not_called()
    (t.live if active else t.read).assert_called_once_with(*(() if active else ("codex-switcher:user@test.com",)))
    req = t.post.call_args.args[0]
    assert req.full_url == "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits/consume"
    assert req.get_method() == "POST"
    assert dict((k.lower(), v) for k, v in req.header_items()) == {
        "authorization": "Bearer sk-test-token", "chatgpt-account-id": "acc-123",
        "accept": "application/json", "user-agent": "claude-switcher/0.4.3", "content-type": "application/json"}
    body = json.loads(req.data)
    assert set(body) == {"redeem_request_id"}
    assert UUID(body["redeem_request_id"]).version == 4
    assert t.post.call_args.kwargs == {"timeout": 10}
    assert t.account.email in caplog.text and body["redeem_request_id"] in caplog.text and code.lower() in caplog.text


@pytest.mark.parametrize("status", [400, 401, 429, 500, 503])
def test_consume_http_errors_never_retry(reset_transport, status):
    t = reset_transport
    t.post.side_effect = urllib.error.HTTPError("url", status, "failure", {}, None)
    with pytest.raises(RuntimeError, match=str(status)):
        codex_usage_mod.consume_reset_credit(t.account.email, t.config)
    assert t.post.call_count == 1


@pytest.mark.parametrize("error", [TimeoutError(), ConnectionResetError(), urllib.error.URLError("connection failed")])
@pytest.mark.parametrize("recovers", [False, True])
def test_consume_network_retry_reuses_key(reset_transport, error, recovers, caplog):
    t = reset_transport
    t.post.side_effect = [error, t.response if recovers else error]
    with caplog.at_level("INFO"):
        if recovers:
            assert codex_usage_mod.consume_reset_credit(t.account.email, t.config) == "reset"
        else:
            with pytest.raises(RuntimeError):
                codex_usage_mod.consume_reset_credit(t.account.email, t.config)
    assert t.post.call_count == 2
    bodies = [json.loads(c.args[0].data) for c in t.post.call_args_list]
    assert bodies[0] == bodies[1]
    assert len([r for r in caplog.records if bodies[0]["redeem_request_id"] in r.message]) == 2


@pytest.mark.parametrize("body", [b'not json', b'\xff', b'[]', b'{}', b'{"code":1}', b'{"code":"unexpected"}'])
def test_consume_invalid_response_never_retries(reset_transport, body):
    t = reset_transport
    t.response.read.return_value = body
    with pytest.raises(RuntimeError):
        codex_usage_mod.consume_reset_credit(t.account.email, t.config)
    assert t.post.call_count == 1


def test_consume_busy_add_does_no_work(reset_transport):
    t = reset_transport
    with patch.object(codex_core_mod, "_add_in_progress", True), pytest.raises(
        RuntimeError, match="A Codex account add is in progress. Try again in a moment."):
        codex_usage_mod.consume_reset_credit(t.account.email, t.config)
    t.post.assert_not_called()
    t.saved.assert_not_called()
    t.active.assert_not_called()


@pytest.mark.parametrize("credits", [None, {}, {"applicable_available_count": 0}, {"applicable_available_count": "1"}])
def test_consume_inapplicable_does_not_post(reset_transport, credits):
    t = reset_transport
    t.usage["rate_limit_reset_credits"] = credits
    assert codex_usage_mod.consume_reset_credit(t.account.email, t.config) == "no_credit"
    t.post.assert_not_called()


@pytest.mark.parametrize("active", [False, True])
def test_consume_uses_real_usage_refresh_writeback(tmp_path, active):
    config = tmp_path / "accounts.json"
    email = "user@test.com"
    save_accounts([AccountInfo(email, "plus", "", active, email, provider="codex")], config)
    stale = json.dumps({"email": email, "tokens": {"access_token": "stale", "account_id": "account", "refresh_token": "refresh"}})
    fresh = json.dumps({"email": email, "tokens": {"access_token": "fresh", "account_id": "account", "refresh_token": "new-refresh"}})
    storage = {"live": stale, "saved": stale}
    requests = []

    def transport(req, timeout):
        requests.append(req)
        assert timeout == 10
        if req.get_method() == "GET" and req.get_header("Authorization") == "Bearer stale":
            raise urllib.error.HTTPError(req.full_url, 401, "expired", {}, None)
        assert req.get_header("Authorization") == "Bearer fresh"
        assert req.get_header("Chatgpt-account-id") == "account"
        response = MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.read.return_value = json.dumps(
            {"rate_limit_reset_credits": {"available_count": 3, "applicable_available_count": 2}}
            if req.get_method() == "GET" else {"code": "reset", "windows_reset": 2}
        ).encode()
        return response

    with patch.object(codex_core_mod, "_read_codex_credentials_for_import_raw", side_effect=lambda: storage["live"]), \
         patch.object(codex_core_mod, "_write_codex_credentials", side_effect=lambda blob: storage.update(live=blob)), \
         patch.object(codex_usage_mod.keychain, "read_credentials", side_effect=lambda service: storage["saved"]), \
         patch.object(codex_usage_mod.keychain, "write_credentials", side_effect=lambda service, email, blob: storage.update(saved=blob)), \
         patch.object(codex_usage_mod, "refresh_codex_credentials", return_value=fresh) as refresh, \
         patch.object(codex_usage_mod, "urlopen", side_effect=transport):
        assert codex_usage_mod.consume_reset_credit(email, config) == "reset"
    refresh.assert_called_once_with(stale)
    assert storage["saved"] == fresh
    assert storage["live"] == (fresh if active else stale)
    assert [r.get_method() for r in requests] == ["GET", "GET", "GET", "POST"]
    assert all(r.full_url in codex_usage_mod.CODEX_USAGE_URLS for r in requests[:-1])


@pytest.mark.parametrize("failure", ["timeout", "truncated"])
def test_consume_response_read_failure_retries_and_reports_status(reset_transport, failure):
    from http.client import IncompleteRead
    t = reset_transport
    t.response.status = 200
    t.response.read.side_effect = TimeoutError() if failure == "timeout" else IncompleteRead(b'{"code":')
    with pytest.raises(RuntimeError, match="HTTP 200"):
        codex_usage_mod.consume_reset_credit(t.account.email, t.config)
    assert t.post.call_count == 2
    assert t.post.call_args_list[0].args[0].data == t.post.call_args_list[1].args[0].data


class TestConsumeResetPrecheckMessages:
    """A missing usage fetch or an expired session must not read as 'no credit'."""

    def _run(self, usage, monkeypatch):
        import claude_switcher.codex_usage as cu
        from types import SimpleNamespace
        monkeypatch.setattr(cu, "get_active_account", lambda *a, **k: SimpleNamespace(email="x@test.com"))
        monkeypatch.setattr(cu, "fetch_active_codex_usage", lambda *a, **k: usage)
        posts = []
        monkeypatch.setattr(cu, "urlopen", lambda *a, **k: posts.append(a) or (_ for _ in ()).throw(AssertionError("must not POST")))
        return cu, posts

    def test_usage_unavailable_raises_not_no_credit(self, monkeypatch):
        import pytest
        cu, posts = self._run(None, monkeypatch)
        with pytest.raises(RuntimeError, match="Usage unavailable"):
            cu.consume_reset_credit("x@test.com")
        assert posts == []

    def test_login_required_raises_not_no_credit(self, monkeypatch):
        import pytest
        cu, posts = self._run({"error": {"code": "login_required"}}, monkeypatch)
        with pytest.raises(RuntimeError, match="Login required"):
            cu.consume_reset_credit("x@test.com")
        assert posts == []

    def test_zero_applicable_still_no_credit_without_post(self, monkeypatch):
        cu, posts = self._run({"rate_limit_reset_credits": {"available_count": 3, "applicable_available_count": 0}}, monkeypatch)
        assert cu.consume_reset_credit("x@test.com") == "no_credit"
        assert posts == []


class TestActiveRowIdentityDrift:
    """The active row must show the active account's usage, not whoever holds the live slot."""

    def _setup(self, monkeypatch, live_email, active_email):
        import claude_switcher.codex_usage as cu
        from types import SimpleNamespace
        monkeypatch.setattr(cu.codex_core, "_read_codex_credentials_for_import_raw", lambda: '{"tokens": {"access_token": "t"}}')
        monkeypatch.setattr(cu, "normalize_codex_credentials_blob", lambda b: b)
        monkeypatch.setattr(cu.codex_core, "_codex_email_from_credentials", lambda c=None: live_email)
        monkeypatch.setattr(cu, "get_active_account", lambda *a, **k: SimpleNamespace(email=active_email))
        monkeypatch.setattr(cu, "_fetch_codex_usage_once", lambda c: {"email": live_email, "live": True})
        calls = []
        monkeypatch.setattr(cu, "fetch_codex_usage_for_account", lambda e, *a, **k: calls.append(e) or {"email": e, "saved": True})
        return cu, calls

    def test_drift_uses_active_accounts_saved_session(self, monkeypatch):
        cu, calls = self._setup(monkeypatch, live_email="gmail@t", active_email="hotmail@t")
        assert cu.fetch_active_codex_usage() == {"email": "hotmail@t", "saved": True}
        assert calls == ["hotmail@t"]

    def test_no_drift_uses_live_session(self, monkeypatch):
        cu, calls = self._setup(monkeypatch, live_email="hotmail@t", active_email="hotmail@t")
        assert cu.fetch_active_codex_usage() == {"email": "hotmail@t", "live": True}
        assert calls == []


@pytest.mark.parametrize("reset_fields, expected", [
    ({"reset_at": 1773921600.5}, 1773921600.5),
    ({"reset_at": "1773921600.5"}, 1773921600.5),
    ({}, None), ({"reset_at": None}, None), ({"reset_at": "invalid"}, None),
])
def test_every_codex_window_has_reset_timestamp(reset_fields, expected):
    state = codex_usage_state({"rate_limit": {
        "primary_window": {"used_percent": 10, **reset_fields},
        "secondary_window": {"used_percent": 20, **reset_fields},
    }})
    assert len(state.windows) == 2
    assert [w.resets_at for w in state.windows] == [expected] * 2
