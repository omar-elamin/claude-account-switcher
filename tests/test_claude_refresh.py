"""Claude refresh wire contract and credential preservation."""

import io
import json
import logging
from unittest.mock import MagicMock
from urllib.error import HTTPError, URLError

import pytest

from claude_switcher import core


TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
NOW = 1_800_000_000
OLD = {"claudeAiOauth": {
    "accessToken": "old-access-secret", "refreshToken": "old-refresh-secret",
    "expiresAt": (NOW - 3600) * 1000, "scopes": ["old-scope"],
    "subscriptionType": "max", "rateLimitTier": "tier", "future": {"keep": True},
}, "other": {"preserve": "me"}}
RESPONSE = {"access_token": "new-access-secret", "refresh_token": "new-refresh-secret",
            "expires_in": 28800, "scope": "user:profile user:inference", "token_type": "Bearer"}


def response(body):
    resp = MagicMock()
    resp.__enter__.return_value = resp
    resp.status = 200
    resp.read.return_value = body if isinstance(body, bytes) else json.dumps(body).encode()
    return resp


@pytest.mark.parametrize("optional_fields", [True, False])
def test_refresh_wire_contract_and_preserved_blob(monkeypatch, caplog, optional_fields):
    import urllib.request

    caplog.set_level(logging.INFO, logger=core.__name__)
    payload = dict(RESPONSE)
    if not optional_fields:
        del payload["refresh_token"], payload["scope"]
    calls = []

    def urlopen(req, timeout):
        calls.append(req)
        assert req.full_url == TOKEN_URL
        assert req.get_method() == "POST"
        assert json.loads(req.data) == {
            "grant_type": "refresh_token", "refresh_token": OLD["claudeAiOauth"]["refreshToken"],
            "client_id": "9d1c250a-e61b-44d9-88ed-5944d1962f5e",
        }
        assert dict(req.header_items()) == {
            "Content-type": "application/json", "Accept": "application/json",
            "User-agent": "claude-code/2.1.11",
        }
        assert timeout == 10
        return response(payload)

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(core.time, "time", lambda: NOW)
    blob = core.refresh_claude_credentials(json.dumps(OLD, indent=2))
    expected = json.loads(json.dumps(OLD))
    expected["claudeAiOauth"].update(accessToken=RESPONSE["access_token"],
                                   expiresAt=(NOW + 28800) * 1000)
    if optional_fields:
        expected["claudeAiOauth"].update(refreshToken=RESPONSE["refresh_token"],
                                       scopes=["user:profile", "user:inference"])
    assert json.loads(blob) == expected
    assert "\n" not in blob and "\r" not in blob
    assert len(calls) == 1
    assert caplog.records and all(r.levelno == logging.INFO for r in caplog.records)
    for secret in ("old-access-secret", "old-refresh-secret", "new-access-secret", "new-refresh-secret"):
        assert secret not in caplog.text


@pytest.mark.parametrize("failure", [400, 401, 500, "network", "timeout", "bad_json", "bad_encoding"])
def test_refresh_failures_are_classified_without_logging_tokens(monkeypatch, caplog, failure):
    import urllib.request

    caplog.set_level(logging.INFO, logger=core.__name__)
    secret = "old-refresh-secret"

    def urlopen(req, timeout):
        if isinstance(failure, int):
            raise HTTPError(req.full_url, failure, secret, {},
                            io.BytesIO(json.dumps({"error": "invalid_grant", "detail": secret}).encode()))
        if failure == "network":
            raise URLError(secret)
        if failure == "timeout":
            raise TimeoutError(secret)
        return response(b"\xff" if failure == "bad_encoding" else secret.encode())

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    if failure in (400, 401):
        with pytest.raises(core.ClaudeCredentialsExpiredError):
            core.refresh_claude_credentials(json.dumps(OLD))
    else:
        assert core.refresh_claude_credentials(json.dumps(OLD)) is None
    assert caplog.records and all(r.levelno == logging.INFO for r in caplog.records)
    assert secret not in caplog.text


@pytest.mark.parametrize("blob", ["not json", "null", "[]", "{}", '{"claudeAiOauth":null}',
                                  '{"claudeAiOauth":{"accessToken":"access"}}'])
def test_invalid_saved_credentials_do_not_post(blob):
    assert core.refresh_claude_credentials(blob) is None


@pytest.mark.parametrize("payload", [None, [], {}, {"access_token": "new", "expires_in": "bad"}])
def test_invalid_refresh_payload_is_unavailable(monkeypatch, payload):
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: response(payload))
    assert core.refresh_claude_credentials(json.dumps(OLD)) is None
