"""Shared test guards."""

import urllib.request

import pytest

import claude_switcher.codex_core as codex_core
import claude_switcher.codex_usage as codex_usage


NETWORK_ATTEMPTS: list[str] = []


@pytest.fixture(autouse=True)
def block_network_access(monkeypatch):
    """Fail every test that reaches a real bound urlopen."""
    NETWORK_ATTEMPTS.clear()

    def blocked_urlopen(request, *args, **kwargs):
        url = getattr(request, "full_url", str(request))
        NETWORK_ATTEMPTS.append(url)
        raise AssertionError(f"network access attempted: {url}")

    monkeypatch.setattr(codex_core, "urlopen", blocked_urlopen)
    monkeypatch.setattr(codex_usage, "urlopen", blocked_urlopen)
    monkeypatch.setattr(urllib.request, "urlopen", blocked_urlopen)
    yield

    attempts = list(NETWORK_ATTEMPTS)
    NETWORK_ATTEMPTS.clear()
    assert not attempts, f"network access attempted: {', '.join(attempts)}"


@pytest.fixture(autouse=True)
def _reset_login_cancel_events():
    """The cancel events are armed at add-lease acquire, not inside run_*_login,
    so a test that sets one must not poison the next test."""
    from claude_switcher import codex_core, core
    core._login_cancel.clear(); codex_core._login_cancel.clear()
    yield
    core._login_cancel.clear(); codex_core._login_cancel.clear()
