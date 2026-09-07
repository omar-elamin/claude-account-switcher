"""Fetch Claude API usage stats via the OAuth usage endpoint."""

import json
import logging
import urllib.request
import urllib.error
from datetime import datetime, timezone

from claude_switcher import keychain
from claude_switcher.common import _format_countdown
from claude_switcher.usage_state import UsageState, UsageWindow

logger = logging.getLogger(__name__)

USAGE_URL = "https://api.anthropic.com/oauth/usage"


def _extract_token(creds_json: str) -> str | None:
    """Extract the OAuth access token from a credentials JSON blob."""
    try:
        data = json.loads(creds_json)
        return data.get("claudeAiOauth", {}).get("accessToken")
    except (json.JSONDecodeError, AttributeError):
        return None


def fetch_usage(service: str) -> dict | None:
    """Fetch usage for a Keychain service. Returns parsed JSON or None on failure.

    Response format:
        {
            "five_hour": {"utilization": 42.5, "resets_at": "..."},
            "seven_day": {"utilization": 18.3, "resets_at": "..."}
        }
    """
    creds = keychain.read_credentials(service)
    if not creds:
        return None

    token = _extract_token(creds)
    if not token:
        return None

    req = urllib.request.Request(
        USAGE_URL,
        headers={
            "Accept": "application/json",
            "anthropic-beta": "oauth-2025-04-20",
            "Authorization": f"Bearer {token}",
            "User-Agent": "claude-code/2.1.11",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError):
        return None


def fetch_usage_for_account(email: str) -> dict | None:
    """Fetch usage for a saved account by email."""
    return fetch_usage(f"claude-switcher:{email}")


def fetch_active_usage(config_path=None) -> dict | None:
    """Fetch usage for the currently active Claude Code session.

    If ~/.claude.json says the live session belongs to a different account
    than the one config marks active (a sign-in done outside the app), use the
    active account's own saved session instead of attributing the live token's
    usage to it.
    """
    from claude_switcher import core
    from claude_switcher.config import DEFAULT_CONFIG_PATH, get_active_account

    active = get_active_account(config_path or DEFAULT_CONFIG_PATH, provider="claude")
    live_email = (core._read_oauth_account() or {}).get("emailAddress")
    if active and live_email and active.email != live_email:
        logger.warning(
            "Claude live session is %s but config marks %s active; using the saved session",
            live_email, active.email,
        )
        return fetch_usage_for_account(active.email)
    return fetch_usage(keychain.CLAUDE_SERVICE)


def _format_reset_delta(resets_at: str) -> str:
    """Convert an ISO 8601 resets_at timestamp to a human-readable relative time."""
    try:
        # Strip fractional seconds for simpler parsing
        cleaned = resets_at.replace("Z", "+00:00")
        reset_dt = datetime.fromisoformat(cleaned)
        now = datetime.now(timezone.utc)
        diff = int((reset_dt - now).total_seconds())

        return _format_countdown(diff)
    except (ValueError, TypeError, AttributeError):
        return "?"


def claude_usage_state(usage: dict | None) -> UsageState:
    """Convert Claude usage data into a normalized usage state."""
    if not usage:
        return UsageState(available=False, display="Usage indisponible")

    parts = []
    windows = []
    five_h = usage.get("five_hour", {})
    seven_d = usage.get("seven_day", {})

    for label, window in (("5h", five_h), ("7j", seven_d)):
        if not isinstance(window, dict) or "utilization" not in window:
            continue
        try:
            percent = float(window["utilization"])
        except (TypeError, ValueError):
            continue

        # The API returns resets_at: null when nothing is scheduled (e.g. a freshly
        # logged-in account with no usage). Key presence is not enough; check the value.
        resets_at = window.get("resets_at")
        reset = _format_reset_delta(resets_at) if resets_at else None
        reset_suffix = f" ({reset})" if reset else ""
        parts.append(f"{label} {percent:.0f}%{reset_suffix}")
        windows.append(UsageWindow(label=label, percent=percent, resets_in=reset))

    # Model-scoped weekly limits (e.g. "Fable") arrive in the `limits` array,
    # self-described by scope.model.display_name, with `percent` rather than
    # `utilization`. Show each by its own name after the account-wide windows.
    for entry in usage.get("limits") or []:
        if not isinstance(entry, dict) or entry.get("kind") != "weekly_scoped":
            continue
        model = ((entry.get("scope") or {}).get("model") or {}).get("display_name")
        if not model:
            continue
        try:
            percent = float(entry["percent"])
        except (KeyError, TypeError, ValueError):
            continue
        resets_at = entry.get("resets_at")
        reset = _format_reset_delta(resets_at) if resets_at else None
        reset_suffix = f" ({reset})" if reset else ""
        parts.append(f"{model} {percent:.0f}%{reset_suffix}")
        windows.append(UsageWindow(label=model, percent=percent, resets_in=reset, scoped=True))

    if not parts:
        return UsageState(available=False, display="Usage indisponible")

    return UsageState(available=True, display=" | ".join(parts), windows=tuple(windows))


def format_usage(usage: dict | None) -> str:
    """Format usage data into a readable string."""
    return claude_usage_state(usage).display
