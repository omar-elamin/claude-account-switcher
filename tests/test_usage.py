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
        assert "7j 18% (5d 14h)" in result

    def test_returns_unavailable_for_none(self):
        assert format_usage(None) == "Usage indisponible"

    def test_returns_unavailable_for_empty(self):
        assert format_usage({}) == "Usage indisponible"

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
        assert labels == ["5h", "7j", "Fable"]

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
        monkeypatch.setattr(u, "fetch_usage_for_account", lambda e: calls.append(e) or {"saved": e})
        return u, calls

    def test_drift_uses_active_accounts_saved_session(self, monkeypatch):
        u, calls = self._setup(monkeypatch, "other@t", "active@t")
        assert u.fetch_active_usage() == {"saved": "active@t"} and calls == ["active@t"]

    def test_no_drift_uses_live_slot(self, monkeypatch):
        u, calls = self._setup(monkeypatch, "active@t", "active@t")
        assert u.fetch_active_usage() == {"service": "Claude Code-credentials"} and calls == []
