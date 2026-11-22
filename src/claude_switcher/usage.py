"""Fetch Claude API usage stats via the OAuth usage endpoint."""

import json
import math
import threading
import time
import logging
import urllib.request
import urllib.error
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from claude_switcher import core, keychain
from claude_switcher.common import _format_countdown
from claude_switcher.config import DEFAULT_CONFIG_PATH, load_accounts
from claude_switcher.usage_state import UsageState, UsageWindow

logger = logging.getLogger(__name__)

USAGE_URL = "https://api.anthropic.com/oauth/usage"
MESSAGES_URL = "https://api.anthropic.com/v1/messages"
# Smallest request that returns the account's rate-limit headers.
_HEADER_PROBE_BODY = json.dumps({
    "model": "claude-haiku-5-5",
    "max_tokens": 1,
    "system": "You are Claude Code, Anthropic's official CLI for Claude.",
    "messages": [{"role": "user", "content": "hi"}],
}).encode()
# time.monotonic() value before which the usage endpoint is not called (after a
# 429). Monotonic, so a wall-clock change cannot end the wait early.
_usage_endpoint_retry_at = 0.0
_usage_endpoint_lock = threading.Lock()
_last_refresh_attempt: dict[str, float] = {}
# Last refresh rejection per email, so a throttled poll repeats "Login required"
# instead of flipping back to "Token expired" for five minutes.
_last_refresh_outcome: dict[str, dict] = {}


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
    return _fetch_usage_once(creds)


def _fetch_usage_once(creds: str) -> dict | None:
    """Fetch with exactly this blob so refresh can compare the original bytes."""
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
    global _usage_endpoint_retry_at
    # The usage endpoint rate limits hard, for every account at once (22 Nov
    # 2026: all 6 accounts got 429 for hours, so no switch ran). Calls go out one
    # at a time, so no call starts while another one's 429 sets the wait. During
    # the wait, read the same numbers from the rate-limit headers instead.
    with _usage_endpoint_lock:
        if time.monotonic() < _usage_endpoint_retry_at:
            rate_limited = True
        else:
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    return json.loads(resp.read())
            except urllib.error.HTTPError as exc:
                if exc.code == 401:
                    return _auth_error(creds)
                if exc.code != 429:
                    return None
                wait = _retry_after_seconds(exc.headers)
                _usage_endpoint_retry_at = max(_usage_endpoint_retry_at,
                                               time.monotonic() + wait)
                rate_limited = True
            except (urllib.error.URLError, json.JSONDecodeError, TimeoutError):
                return None
    if rate_limited:
        return _fetch_usage_from_headers(token, creds)
    return None


def _auth_error(creds: str) -> dict:
    # Claude access tokens last about 8 hours and only Claude Code
    # refreshes the live one. Saved accounts can attempt refresh when
    # expired; an unexpired 401 means a new sign-in is required.
    code = "token_expired" if _token_expired(creds) else "login_required"
    return {"error": {"code": code}}


def _retry_after_seconds(headers, now: float | None = None) -> float:
    """Seconds to wait from a Retry-After header (delay or HTTP date), at least 60."""
    now = time.time() if now is None else now
    try:
        raw = (headers.get("Retry-After") or "").strip()
    except AttributeError:
        raw = ""
    value = 0.0
    try:
        value = float(raw)
    except ValueError:
        try:
            when = parsedate_to_datetime(raw)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)  # HTTP dates are always GMT
            value = when.timestamp() - now
        except (TypeError, ValueError, IndexError, OverflowError):
            value = 0.0
    return max(value, 60.0)


def _fetch_usage_from_headers(token: str, creds: str) -> dict | None:
    """Read usage from the anthropic-ratelimit-unified-* headers of a 1-token request.

    Every Messages API response, also the 429 for a full account, carries the
    account's 5-hour and 7-day utilization (a 0..1 fraction) and reset time
    (epoch seconds). Returns the usage endpoint's shape, an auth error, or None.
    """
    req = urllib.request.Request(
        MESSAGES_URL,
        data=_HEADER_PROBE_BODY,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-version": "2023-06-01",
            "anthropic-beta": "oauth-2025-04-20",
            "content-type": "application/json",
            "User-Agent": "claude-code/2.1.11",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            headers = resp.headers
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return _auth_error(creds)
        headers = exc.headers
    except (urllib.error.URLError, TimeoutError, OSError):
        return None
    return usage_from_ratelimit_headers(headers)


def usage_from_ratelimit_headers(headers) -> dict | None:
    """Convert unified rate-limit headers to {"five_hour": ..., "seven_day": ...}."""
    prefix = "anthropic-ratelimit-unified-"
    values = {k.lower()[len(prefix):]: v for k, v in (headers or {}).items()
              if k.lower().startswith(prefix)}
    usage = {}
    for short, key in (("5h", "five_hour"), ("7d", "seven_day")):
        try:
            # The API can report a little over 1.0 for a full window.
            percent = float(values[f"{short}-utilization"]) * 100
            if not math.isfinite(percent):
                raise ValueError("utilization is not a number")
            percent = min(max(percent, 0.0), 100.0)
        except (KeyError, TypeError, ValueError):
            # A rejected window may omit utilization; it is full.
            if values.get(f"{short}-status") != "rejected":
                continue
            percent = 100.0
        resets_at = None
        try:
            resets_at = datetime.fromtimestamp(
                float(values[f"{short}-reset"]), tz=timezone.utc).isoformat()
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            pass
        usage[key] = {"utilization": percent, "resets_at": resets_at}
    return usage or None


def _token_expired(creds_json: str) -> bool:
    """True when the blob's claudeAiOauth.expiresAt (ms epoch) is in the past."""
    try:
        expires_at = json.loads(creds_json)["claudeAiOauth"]["expiresAt"]
        expires_s = expires_at / 1000 if expires_at > 1e11 else expires_at
        return expires_s < time.time()
    except (ValueError, TypeError, KeyError, AttributeError):
        return False


def fetch_usage_for_account(email: str, config_path=DEFAULT_CONFIG_PATH) -> dict | None:
    """Fetch saved usage, safely refreshing expired credentials before reuse."""
    service = f"claude-switcher:{email}"
    backup = keychain.read_credentials(service)
    if not backup:
        return None
    usage = _fetch_usage_once(backup)
    if usage != {"error": {"code": "token_expired"}}:
        return usage

    with core._CLAUDE_LOCK:
        if core._add_in_progress:
            return usage
        if keychain.read_credentials(service) != backup:
            return usage

        live = keychain.read_credentials(keychain.CLAUDE_SERVICE)
        if live:
            try:
                live_tokens = json.loads(live).get("claudeAiOauth", {})
                saved_tokens = json.loads(backup)["claudeAiOauth"]
                shared = any(
                    saved_tokens.get(field) and saved_tokens[field] == live_tokens.get(field)
                    for field in ("refreshToken", "accessToken")
                )
            except (ValueError, TypeError, KeyError, AttributeError):
                # An unreadable live blob cannot establish that rotation is safe.
                return usage
            if shared:
                logger.info("Claude refresh skipped: shared with live session")
                return usage

        now = time.time()
        last_attempt = _last_refresh_attempt.get(email)
        if last_attempt is not None and now - last_attempt < 300:
            return _last_refresh_outcome.get(email, usage)
        _last_refresh_attempt[email] = now

        try:
            refreshed = core.refresh_claude_credentials(backup)
        except core.ClaudeCredentialsExpiredError:
            _last_refresh_outcome[email] = {"error": {"code": "login_required"}}
            return _last_refresh_outcome[email]
        _last_refresh_outcome.pop(email, None)
        if refreshed is None:
            return None

        account_attr = keychain.read_account_attribute(service)
        if account_attr is None:
            account_attr = next(
                (a.keychain_account for a in load_accounts(config_path)
                 if a.provider == "claude" and a.email == email),
                None,
            )
        if account_attr is None:
            raise RuntimeError("Saved Claude Keychain account attribute is unavailable.")
        keychain.write_credentials(service, account_attr, refreshed)

    return _fetch_usage_once(refreshed)


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
        return fetch_usage_for_account(active.email, config_path or DEFAULT_CONFIG_PATH)
    core.sync_current_account_credentials(config_path or DEFAULT_CONFIG_PATH)
    return fetch_usage(keychain.CLAUDE_SERVICE)


def _parse_reset_timestamp(resets_at: str | None) -> float | None:
    """Convert an ISO reset time to epoch seconds, or None if invalid."""
    try:
        reset_dt = datetime.fromisoformat(resets_at.replace("Z", "+00:00"))
        if reset_dt.tzinfo is None:
            return None
        return reset_dt.timestamp()
    except (ValueError, TypeError, AttributeError, OverflowError, OSError):
        return None


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
        return UsageState(available=False, display="Usage unavailable")
    error = usage.get("error") if isinstance(usage, dict) else None
    if isinstance(error, dict):
        if error.get("code") == "token_expired":
            return UsageState(available=False, display="Token expired (switch to refresh)")
        if error.get("code") == "login_required":
            return UsageState(available=False, display="Login required")

    parts = []
    windows = []
    five_h = usage.get("five_hour", {})
    seven_d = usage.get("seven_day", {})

    for label, window in (("5h", five_h), ("7d", seven_d)):
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
        windows.append(UsageWindow(label=label, percent=percent, resets_in=reset,
                                   resets_at=_parse_reset_timestamp(resets_at)))

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
        windows.append(UsageWindow(label=model, percent=percent, resets_in=reset, scoped=True,
                                   resets_at=_parse_reset_timestamp(resets_at)))

    if not parts:
        return UsageState(available=False, display="Usage unavailable")

    return UsageState(available=True, display=" | ".join(parts), windows=tuple(windows))


def format_usage(usage: dict | None) -> str:
    """Format usage data into a readable string."""
    return claude_usage_state(usage).display
