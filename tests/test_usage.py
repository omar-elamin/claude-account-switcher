"""Tests for the usage module."""

import json

import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone, timedelta

from claude_switcher.usage import (
    _extract_token,
    _format_reset_delta,
    format_usage,
    fetch_usage_for_account,
    claude_usage_state,
)


class TestExtractToken:
    def test_extracts_oauth_token(self):
        creds = json.dumps({"claudeAiOauth": {"accessToken": "tok_123"}})
        assert _extract_token(creds) == "tok_123"

    def test_returns_none_for_missing_oauth(self):
        creds = json.dumps({"other": "data"})
        assert _extract_token(creds) is None

    def test_returns_none_for_invalid_json(self):
        assert _extract_token("not json") is None


class TestFormatResetDelta:
    def test_days_and_hours(self):
        future = datetime.now(timezone.utc) + timedelta(days=5, hours=13)
        result = _format_reset_delta(future.isoformat())
        assert result.startswith("5d 1")  # 5d 13h or 5d 12h depending on timing

    def test_hours_and_minutes(self):
        future = datetime.now(timezone.utc) + timedelta(hours=2, minutes=30)
        result = _format_reset_delta(future.isoformat())
        assert result.startswith("2h ")

    def test_minutes_only(self):
        future = datetime.now(timezone.utc) + timedelta(minutes=45)
        result = _format_reset_delta(future.isoformat())
        assert result.endswith("m")
        assert "h" not in result

    def test_past_returns_now(self):
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        assert _format_reset_delta(past.isoformat()) == "now"

    def test_z_suffix(self):
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        ts = future.strftime("%Y-%m-%dT%H:%M:%SZ")
        result = _format_reset_delta(ts)
        assert "h" in result or "m" in result


class TestFormatUsage:
    @patch("claude_switcher.usage.datetime")
    def test_formats_both_periods(self, mock_dt):
        now = datetime(2026, 3, 19, 10, 0, 0, tzinfo=timezone.utc)
        mock_dt.now.return_value = now
        mock_dt.fromisoformat = datetime.fromisoformat
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        usage = {
            "five_hour": {"utilization": 42.7, "resets_at": "2026-03-19T12:00:00+00:00"},
            "seven_day": {"utilization": 18.3, "resets_at": "2026-03-25T00:00:00+00:00"},
        }
        result = format_usage(usage)
        assert "5h 43% (2h 0m)" in result
        assert "7d 18% (5d 14h)" in result

    def test_returns_unavailable_for_none(self):
        assert format_usage(None) == "Usage unavailable"

    def test_returns_unavailable_for_empty(self):
        assert format_usage({}) == "Usage unavailable"

    def test_usage_state_marks_exhausted_at_100(self):
        usage = {
            "five_hour": {"utilization": 100.0},
            "seven_day": {"utilization": 12.0},
        }
        state = claude_usage_state(usage)
        assert state.available is True
        assert state.is_exhausted() is True
        assert state.max_percent == 100.0

    def test_usage_state_does_not_mark_99_9_exhausted(self):
        state = claude_usage_state({"five_hour": {"utilization": 99.9}})
        assert state.is_exhausted() is False

    def test_usage_state_unavailable_without_windows(self):
        state = claude_usage_state({"five_hour": {}, "seven_day": {}})
        assert state.available is False


class TestFetchUsageForAccount:
    @patch("claude_switcher.usage.urllib.request.urlopen")
    @patch("claude_switcher.usage.keychain.read_credentials")
    def test_fetches_and_parses(self, mock_read, mock_urlopen):
        mock_read.return_value = json.dumps({"claudeAiOauth": {"accessToken": "tok"}})
        response_data = json.dumps({
            "five_hour": {"utilization": 50.0, "resets_at": "2026-03-19T12:00:00Z"},
            "seven_day": {"utilization": 20.0, "resets_at": "2026-03-25T00:00:00Z"},
        }).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = response_data
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        result = fetch_usage_for_account("test@test.com")
        assert result["five_hour"]["utilization"] == 50.0

    @patch("claude_switcher.usage.keychain.read_credentials")
    def test_returns_none_when_no_creds(self, mock_read):
        mock_read.return_value = None
        assert fetch_usage_for_account("test@test.com") is None


class TestNullResetsAt:
    """Regression: the API returns resets_at: null for an account with no
    scheduled reset. That crashed the Claude parser (None.replace) and the
    whole row showed 'Usage unavailable' instead of the percentage."""

    def test_null_resets_at_does_not_crash_and_keeps_percent(self):
        u = {"five_hour": {"utilization": 0.0, "resets_at": None},
             "seven_day": {"utilization": 3.0, "resets_at": None}}
        st = claude_usage_state(u)
        assert st.available is True
        assert "0%" in st.display and "3%" in st.display
        assert "?" not in st.display          # no bogus countdown either

    def test_format_reset_delta_tolerates_non_string(self):
        assert _format_reset_delta(None) == "?"
        assert _format_reset_delta(12345) == "?"


@pytest.mark.parametrize("value, expected", [
    ("2026-03-19T12:00:00Z", "2h 0m"),
    (1773921600, "?"),
    ("1773921600", "?"),
    (None, "?"),
    ({}, "?"),
    ([], "?"),
    (12345, "?"),
    ("12345", "?"),
])
def test_reset_adapter_input_contract(value, expected):
    from claude_switcher.usage import _format_reset_delta

    with patch("claude_switcher.usage.datetime", wraps=datetime) as clock:
        clock.now.return_value = datetime(2026, 3, 19, 10, tzinfo=timezone.utc)
        assert _format_reset_delta(value) == expected


@pytest.mark.parametrize("seconds, expected", [
    (-1, "now"), (0, "now"), (1, "0m"), (59, "0m"), (60, "1m"),
    (3599, "59m"), (3600, "1h 0m"), (86399, "23h 59m"),
    (86400, "1d 0h"), (133200, "1d 13h"),
])
def test_reset_adapter_countdown_boundaries(seconds, expected):
    from claude_switcher.usage import _format_reset_delta

    now = datetime(2026, 3, 19, 10, tzinfo=timezone.utc)
    target = now + timedelta(seconds=seconds)
    with patch("claude_switcher.usage.datetime", wraps=datetime) as clock:
        clock.now.return_value = now
        assert _format_reset_delta(target.isoformat()) == expected


class TestModelScopedWeeklyLimit:
    """The Anthropic usage API reports model-scoped weekly limits (e.g. Fable)
    in the `limits` array, self-described by scope.model.display_name."""

    def _usage(self, fable_pct=32, five=40.0, seven=20.0):
        return {
            "five_hour": {"utilization": five, "resets_at": "2099-01-01T00:00:00Z"},
            "seven_day": {"utilization": seven, "resets_at": "2099-01-02T00:00:00Z"},
            "limits": [
                {"kind": "session", "percent": five, "resets_at": "2099-01-01T00:00:00Z", "scope": None},
                {"kind": "weekly_all", "percent": seven, "resets_at": "2099-01-02T00:00:00Z", "scope": None},
                {"kind": "weekly_scoped", "percent": fable_pct, "resets_at": "2099-01-02T00:00:00Z",
                 "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None}},
            ],
        }

    def test_fable_window_is_shown_by_its_own_name(self):
        st = claude_usage_state(self._usage())
        assert st.available
        assert "Fable 32%" in st.display
        assert st.display.startswith("5h 40%")          # account-wide windows come first
        labels = [w.label for w in st.windows]
        assert labels == ["5h", "7d", "Fable"]

    def test_scoped_window_does_not_trigger_exhaustion(self):
        # Fable at 100% but the account's own windows have room: not exhausted.
        st = claude_usage_state(self._usage(fable_pct=100))
        assert st.is_exhausted(100.0) is False
        assert st.max_percent == 40.0                     # scoped window excluded

    def test_account_window_still_triggers_exhaustion(self):
        st = claude_usage_state(self._usage(five=100.0))
        assert st.is_exhausted(100.0) is True

    def test_missing_or_malformed_limits_are_ignored(self):
        u = self._usage(); del u["limits"]
        assert "Fable" not in claude_usage_state(u).display
        u = self._usage(); u["limits"] = None
        assert "Fable" not in claude_usage_state(u).display
        u = self._usage(); u["limits"] = [{"kind": "weekly_scoped", "percent": "x", "scope": {"model": {"display_name": "Fable"}}}]
        assert "Fable" not in claude_usage_state(u).display  # bad percent skipped, no crash
        u = self._usage(); u["limits"][2]["scope"] = None
        assert "Fable" not in claude_usage_state(u).display  # no model name -> skipped


class TestClaudeActiveRowIdentityDrift:
    def _setup(self, monkeypatch, live_email, active_email):
        import claude_switcher.usage as u
        import claude_switcher.core as core
        import claude_switcher.config as config
        from types import SimpleNamespace
        monkeypatch.setattr(core, "_read_oauth_account", lambda: {"emailAddress": live_email})
        monkeypatch.setattr(config, "get_active_account", lambda *a, **k: SimpleNamespace(email=active_email))
        monkeypatch.setattr(u, "fetch_usage", lambda service: {"service": service})
        calls = []
        monkeypatch.setattr(u, "fetch_usage_for_account", lambda e, config_path=None: calls.append(e) or {"saved": e})
        return u, calls

    def test_drift_uses_active_accounts_saved_session(self, monkeypatch):
        u, calls = self._setup(monkeypatch, "other@t", "active@t")
        assert u.fetch_active_usage() == {"saved": "active@t"} and calls == ["active@t"]

    def test_no_drift_uses_live_slot(self, monkeypatch):
        u, calls = self._setup(monkeypatch, "active@t", "active@t")
        assert u.fetch_active_usage() == {"service": "Claude Code-credentials"} and calls == []


class TestExpiredSavedToken:
    """A 401 on a saved Claude token is reported as expired or revoked, not 'unavailable'."""

    def _fetch(self, monkeypatch, expires_delta_s, http_code=401):
        import io, json, time, urllib.error
        import claude_switcher.usage as u
        blob = json.dumps({"claudeAiOauth": {"accessToken": "t", "refreshToken": "r", "expiresAt": int((time.time() + expires_delta_s) * 1000)}})
        monkeypatch.setattr(u.keychain, "read_credentials", lambda s: blob)
        def boom(req, timeout=5): raise urllib.error.HTTPError(req.full_url, http_code, "x", {}, io.BytesIO(b""))
        monkeypatch.setattr(u.urllib.request, "urlopen", boom)
        return u, u.fetch_usage("claude-switcher:a@t")

    def test_expired_token_401_reads_token_expired(self, monkeypatch):
        u, data = self._fetch(monkeypatch, -3600)
        assert u.claude_usage_state(data).display == "Token expired (switch to refresh)"
        assert u.claude_usage_state(data).available is False

    def test_unexpired_token_401_reads_login_required(self, monkeypatch):
        u, data = self._fetch(monkeypatch, +3600)
        assert u.claude_usage_state(data).display == "Login required"

    def test_other_http_error_is_plain_unavailable(self, monkeypatch):
        u, data = self._fetch(monkeypatch, -3600, http_code=500)
        assert data is None and u.claude_usage_state(data).display == "Usage unavailable"


@pytest.fixture
def saved_refresh(monkeypatch, tmp_path):
    """Real fetch/refresh functions, with in-memory Keychain and HTTP boundaries."""
    import io
    import urllib.error
    from types import SimpleNamespace
    from claude_switcher import core, usage, config

    now = 1_800_000_000
    email = "saved@example.test"
    service = f"claude-switcher:{email}"
    old = json.dumps({"claudeAiOauth": {"accessToken": "old-access", "refreshToken": "old-refresh",
                                      "expiresAt": (now - 3600) * 1000}})
    live = json.dumps({"claudeAiOauth": {"accessToken": "live-access", "refreshToken": "live-refresh"}})
    state = SimpleNamespace(email=email, service=service, old=old, now=now,
                            store={service: old, usage.keychain.CLAUDE_SERVICE: live},
                            events=[], post_error=None, write_error=None,
                            path=tmp_path / "accounts.json", result={"five_hour": {"utilization": 42}})
    config.save_accounts([config.AccountInfo(email, "max", "", False, "saved-attribute")], state.path)
    monkeypatch.setattr(core, "_add_in_progress", False)
    monkeypatch.setattr(usage, "_last_refresh_attempt", {})
    monkeypatch.setattr(usage.time, "time", lambda: state.now)

    def read(service):
        state.events.append(("read", service))
        return state.store.get(service)

    def write(service, account, blob):
        assert core._CLAUDE_LOCK.locked()
        assert service == state.service  # Never write the live slot.
        state.events.append(("write", service, account))
        if state.write_error:
            raise state.write_error
        state.store[service] = blob

    def urlopen(req, timeout):
        method = req.get_method()
        state.events.append((method, req.get_header("Authorization")))
        if method == "POST":
            assert core._CLAUDE_LOCK.locked()
            assert timeout == 10
            if state.post_error:
                raise state.post_error
            data = {"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 28800}
        elif req.get_header("Authorization") == "Bearer new-access":
            assert json.loads(state.store[state.service])["claudeAiOauth"]["accessToken"] == "new-access"
            data = state.result
        else:
            raise urllib.error.HTTPError(req.full_url, 401, "unauthorized", {}, io.BytesIO(b""))
        resp = MagicMock()
        resp.__enter__.return_value = resp
        resp.status = 200
        resp.read.return_value = json.dumps(data).encode()
        return resp

    monkeypatch.setattr(usage.keychain, "read_credentials", read)
    monkeypatch.setattr(usage.keychain, "read_account_attribute", lambda service: "keychain-attribute")
    monkeypatch.setattr(usage.keychain, "write_credentials", write)
    monkeypatch.setattr(usage.urllib.request, "urlopen", urlopen)
    state.fetch = lambda: usage.fetch_usage_for_account(email, state.path)
    return state


def _refresh_actions(state):
    return [e[0] for e in state.events if e[0] != "read"]


def test_saved_refresh_persists_before_using_new_token(saved_refresh):
    from claude_switcher import usage

    s = saved_refresh
    live_before = dict(s.store)
    assert usage.fetch_usage_for_account(s.email) == s.result
    assert _refresh_actions(s) == ["GET", "POST", "write", "GET"]
    assert ("GET", "Bearer old-access") in s.events
    assert ("GET", "Bearer new-access") in s.events
    assert ("write", s.service, "keychain-attribute") in s.events
    assert json.loads(s.store[s.service])["claudeAiOauth"]["refreshToken"] == "new-refresh"
    assert all(s.store[k] == v for k, v in live_before.items() if k != s.service)
    s.events.clear()
    assert s.fetch() == s.result
    assert _refresh_actions(s) == ["GET"]  # Successful fast path never refreshes.


@pytest.mark.parametrize("field", ["refreshToken", "accessToken"])
@pytest.mark.parametrize("active", [True, False])
def test_saved_refresh_blocks_shared_live_tokens(saved_refresh, field, active, caplog):
    import logging
    from claude_switcher import usage, config

    s = saved_refresh
    accounts = config.load_accounts(s.path)
    accounts[0].active = active
    config.save_accounts(accounts, s.path)
    live = json.loads(s.store[usage.keychain.CLAUDE_SERVICE])
    live["claudeAiOauth"][field] = json.loads(s.old)["claudeAiOauth"][field]
    s.store[usage.keychain.CLAUDE_SERVICE] = json.dumps(live)
    before = dict(s.store)
    caplog.set_level(logging.INFO)
    assert s.fetch() == {"error": {"code": "token_expired"}}
    assert _refresh_actions(s) == ["GET"]
    assert s.store == before
    assert "shared with live session" in caplog.text


def test_saved_refresh_blocks_add_in_progress(saved_refresh, monkeypatch):
    from claude_switcher import core

    monkeypatch.setattr(core, "_add_in_progress", True)
    assert saved_refresh.fetch() == {"error": {"code": "token_expired"}}
    assert _refresh_actions(saved_refresh) == ["GET"]


def test_saved_refresh_blocks_changed_backup(saved_refresh, monkeypatch):
    from claude_switcher import usage

    s = saved_refresh
    original_read = usage.keychain.read_credentials

    def changed_read(service):
        blob = original_read(service)
        if service == s.service and ("GET", "Bearer old-access") in s.events:
            return blob + " "  # Even a byte-only difference must block rotation.
        return blob

    monkeypatch.setattr(usage.keychain, "read_credentials", changed_read)
    assert s.fetch() == {"error": {"code": "token_expired"}}
    assert _refresh_actions(s) == ["GET"]


def test_saved_refresh_throttle_expires_after_300_seconds(saved_refresh):
    from urllib.error import URLError

    s = saved_refresh
    s.post_error = URLError("offline")
    assert s.fetch() is None
    s.events.clear()
    s.now += 299
    assert s.fetch() == {"error": {"code": "token_expired"}}
    assert _refresh_actions(s) == ["GET"]
    s.events.clear()
    s.now += 1
    s.post_error = None
    assert s.fetch() == s.result
    assert _refresh_actions(s) == ["GET", "POST", "write", "GET"]


@pytest.mark.parametrize("status", [400, 401])
def test_saved_refresh_invalid_grant_requires_login_without_write(saved_refresh, status):
    import io
    from urllib.error import HTTPError

    s = saved_refresh
    s.post_error = HTTPError("https://platform.claude.com/v1/oauth/token", status, "invalid_grant", {},
                             io.BytesIO(b'{"error":"invalid_grant"}'))
    assert s.fetch() == {"error": {"code": "login_required"}}
    assert _refresh_actions(s) == ["GET", "POST"]
    assert s.store[s.service] == s.old


def test_saved_refresh_network_failure_is_unavailable_without_write(saved_refresh):
    from urllib.error import URLError

    s = saved_refresh
    s.post_error = URLError("offline")
    assert s.fetch() is None
    assert _refresh_actions(s) == ["GET", "POST"]
    assert s.store[s.service] == s.old


def test_saved_refresh_write_failure_propagates_without_using_new_token(saved_refresh):
    s = saved_refresh
    s.write_error = RuntimeError("Keychain write failed")
    with pytest.raises(RuntimeError, match="Keychain write failed"):
        s.fetch()
    assert _refresh_actions(s) == ["GET", "POST", "write"]
    assert s.store[s.service] == s.old


def test_saved_unexpired_401_does_not_refresh(saved_refresh):
    s = saved_refresh
    blob = json.loads(s.old)
    blob["claudeAiOauth"]["expiresAt"] = (s.now + 3600) * 1000
    s.store[s.service] = json.dumps(blob)
    assert s.fetch() == {"error": {"code": "login_required"}}
    assert _refresh_actions(s) == ["GET"]


def test_saved_refresh_uses_existing_config_attribute_as_fallback(saved_refresh, monkeypatch):
    from claude_switcher import usage

    monkeypatch.setattr(usage.keychain, "read_account_attribute", lambda service: None)
    assert saved_refresh.fetch() == saved_refresh.result
    assert ("write", saved_refresh.service, "saved-attribute") in saved_refresh.events


def test_active_drift_refreshes_saved_account_with_its_config(saved_refresh, monkeypatch):
    from claude_switcher import core, config, usage

    s = saved_refresh
    config.set_active_account(s.email, s.path)
    monkeypatch.setattr(core, "_read_oauth_account", lambda: {"emailAddress": "other@example.test"})
    monkeypatch.setattr(usage.keychain, "read_account_attribute", lambda service: None)
    assert usage.fetch_active_usage(s.path) == s.result
    assert ("write", s.service, "saved-attribute") in s.events


def test_overlapping_saved_fetches_rotate_once_and_preserve_saved_pair(saved_refresh, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from claude_switcher import usage

    s = saved_refresh
    both_using_old_token = Barrier(2)
    original_urlopen = usage.urllib.request.urlopen

    def overlapping_urlopen(req, timeout):
        if req.get_method() == "GET" and req.get_header("Authorization") == "Bearer old-access":
            both_using_old_token.wait(timeout=5)
        return original_urlopen(req, timeout)

    monkeypatch.setattr(usage.urllib.request, "urlopen", overlapping_urlopen)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(s.fetch) for _ in range(2)]
        results = [f.result(timeout=10) for f in futures]
    assert s.result in results
    assert {"error": {"code": "token_expired"}} in results
    actions = _refresh_actions(s)
    assert actions.count("POST") == actions.count("write") == 1
    saved = json.loads(s.store[s.service])["claudeAiOauth"]
    assert saved["accessToken"] == "new-access" and saved["refreshToken"] == "new-refresh"


def test_saved_refresh_never_invents_missing_account_attribute(saved_refresh, monkeypatch):
    from claude_switcher import usage, config

    s = saved_refresh
    config.save_accounts([], s.path)
    monkeypatch.setattr(usage.keychain, "read_account_attribute", lambda service: None)
    with pytest.raises(RuntimeError, match="account attribute"):
        s.fetch()
    assert _refresh_actions(s) == ["GET", "POST"]
    assert s.store[s.service] == s.old
