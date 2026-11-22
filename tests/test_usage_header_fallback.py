"""Usage fallback when the usage endpoint answers 429 (22 Nov 2026 outage)."""

from email.message import Message

from claude_switcher import usage


def _headers(**values):
    msg = Message()
    for key, value in values.items():
        msg[key.replace("_", "-")] = value
    return msg


def test_retry_after_numeric_is_not_capped():
    assert usage._retry_after_seconds(_headers(Retry_After="3600"), now=0) == 3600
    assert usage._retry_after_seconds(_headers(Retry_After="172800"), now=0) == 172800


def test_retry_after_http_date():
    now = 1_800_000_000  # 2027-01-15 08:00:00 GMT
    hdr = _headers(Retry_After="Fri, 15 Jan 2027 09:00:00 GMT")
    assert usage._retry_after_seconds(hdr, now=now) == 3600


def test_retry_after_asctime_date_is_gmt():
    now = 1_800_000_000  # 2027-01-15 08:00:00 GMT
    hdr = _headers(Retry_After="Fri Jan 15 09:00:00 2027")
    assert usage._retry_after_seconds(hdr, now=now) == 3600


def test_retry_after_missing_or_bad_waits_one_minute():
    assert usage._retry_after_seconds(_headers(), now=0) == 60
    assert usage._retry_after_seconds(_headers(Retry_After="soon"), now=0) == 60


def test_headers_give_weekly_and_five_hour_percent():
    hdr = _headers(**{
        "anthropic-ratelimit-unified-5h-utilization": "1.02",
        "anthropic-ratelimit-unified-5h-reset": "1795352400",
        "anthropic-ratelimit-unified-7d-utilization": "0.15",
        "anthropic-ratelimit-unified-7d-reset": "1795903200",
        "anthropic-ratelimit-unified-status": "rejected",
    })
    data = usage.usage_from_ratelimit_headers(hdr)
    assert data["five_hour"]["utilization"] == 100.0
    assert round(data["seven_day"]["utilization"]) == 15
    state = usage.claude_usage_state(data)
    assert state.available and state.is_exhausted(95)


def test_rejected_window_without_utilization_is_full():
    hdr = _headers(**{"anthropic-ratelimit-unified-7d-status": "rejected"})
    assert usage.usage_from_ratelimit_headers(hdr)["seven_day"]["utilization"] == 100.0


def test_no_ratelimit_headers_is_unavailable():
    assert usage.usage_from_ratelimit_headers(_headers(Date="x")) is None


import io
import json
import urllib.error

import pytest


@pytest.fixture
def fresh_endpoint_block(monkeypatch):
    # monkeypatch restores the value this test found, so no state leaks.
    monkeypatch.setattr(usage, "_usage_endpoint_retry_at", 0.0)


def test_usage_429_falls_back_to_headers_and_waits(monkeypatch, fresh_endpoint_block):
    blob = json.dumps({"claudeAiOauth": {"accessToken": "t", "expiresAt": 9_999_999_999_999}})
    calls = []

    class Resp:
        headers = _headers(**{"anthropic-ratelimit-unified-7d-utilization": "0.95",
                              "anthropic-ratelimit-unified-7d-reset": "1795424400"})
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def urlopen(req, timeout):
        calls.append(req.full_url)
        if req.full_url == usage.USAGE_URL:
            raise urllib.error.HTTPError(req.full_url, 429, "rate", _headers(Retry_After="221"),
                                         io.BytesIO(b""))
        assert req.full_url == usage.MESSAGES_URL
        return Resp()

    monkeypatch.setattr(usage.urllib.request, "urlopen", urlopen)
    first = usage._fetch_usage_once(blob)
    assert round(first["seven_day"]["utilization"]) == 95
    assert calls == [usage.USAGE_URL, usage.MESSAGES_URL]
    # During Retry-After the usage endpoint is not asked again.
    usage._fetch_usage_once(blob)
    assert calls[2:] == [usage.MESSAGES_URL]


def test_header_probe_401_keeps_token_expired_signal(monkeypatch, fresh_endpoint_block):
    blob = json.dumps({"claudeAiOauth": {"accessToken": "t", "expiresAt": 1000}})
    usage._usage_endpoint_retry_at = 9_999_999_999.0

    def urlopen(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 401, "x", _headers(), io.BytesIO(b""))

    monkeypatch.setattr(usage.urllib.request, "urlopen", urlopen)
    assert usage._fetch_usage_once(blob) == {"error": {"code": "token_expired"}}




def test_later_short_retry_after_does_not_shorten_the_wait(monkeypatch, fresh_endpoint_block):
    """A 429 that says 60 s must not cut a 3600 s wait set earlier."""
    blob = json.dumps({"claudeAiOauth": {"accessToken": "t", "expiresAt": 9_999_999_999_999}})
    monkeypatch.setattr(usage, "_usage_endpoint_retry_at", 4600.0)
    clock = iter([4600.0, 1000.0])  # the wait check, then the time the 429 arrives
    monkeypatch.setattr(usage.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(usage, "_fetch_usage_from_headers", lambda token, creds: None)

    def urlopen(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 429, "rate", _headers(Retry_After="60"),
                                     io.BytesIO(b""))

    monkeypatch.setattr(usage.urllib.request, "urlopen", urlopen)
    usage._fetch_usage_once(blob)
    assert usage._usage_endpoint_retry_at == 4600.0


def test_concurrent_caller_waits_for_the_429_and_does_not_call(monkeypatch, fresh_endpoint_block):
    """While one usage call is open, a second caller must not call the endpoint too."""
    import threading
    blob = json.dumps({"claudeAiOauth": {"accessToken": "t", "expiresAt": 9_999_999_999_999}})
    monkeypatch.setattr(usage, "_fetch_usage_from_headers", lambda token, creds: {"ok": True})
    first_open = threading.Event()
    release = threading.Event()
    usage_calls = []

    def urlopen(req, timeout):
        usage_calls.append(req.full_url)
        first_open.set()
        release.wait(5)
        raise urllib.error.HTTPError(req.full_url, 429, "rate", _headers(Retry_After="3600"),
                                     io.BytesIO(b""))

    monkeypatch.setattr(usage.urllib.request, "urlopen", urlopen)
    results = []
    first = threading.Thread(target=lambda: results.append(usage._fetch_usage_once(blob)))
    first.start()
    assert first_open.wait(5)
    second = threading.Thread(target=lambda: results.append(usage._fetch_usage_once(blob)))
    second.start()
    second.join(0.2)               # the second caller is blocked, not calling
    assert len(usage_calls) == 1
    release.set()
    first.join(5)
    second.join(5)
    assert usage_calls == [usage.USAGE_URL]
    assert results == [{"ok": True}, {"ok": True}]
