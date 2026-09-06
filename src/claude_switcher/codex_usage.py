"""Codex CLI usage fetching via ChatGPT backend APIs."""

import json
import logging
from datetime import datetime, timezone
from http.client import IncompleteRead
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from claude_switcher import keychain
from claude_switcher.common import _decode_jwt_payload, _format_countdown
import claude_switcher.codex_core as codex_core
from claude_switcher.codex_core import (
    CodexCredentialsExpiredError,
    normalize_codex_credentials_blob,
    refresh_codex_credentials,
)
from claude_switcher.config import DEFAULT_CONFIG_PATH, get_active_account, load_accounts
from claude_switcher.usage_state import UsageState, UsageWindow

CODEX_USAGE_URLS = (
    "https://chatgpt.com/backend-api/wham/usage",
    "https://chatgpt.com/backend-api/api/codex/usage",
)
CODEX_LOGIN_REQUIRED_USAGE = {"error": {"code": "login_required"}}
CODEX_RESET_CONSUME_URL = "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits/consume"
RESET_CODES = frozenset({"reset", "nothing_to_reset", "no_credit", "already_redeemed"})
logger = logging.getLogger(__name__)


def _extract_codex_token(creds_json: str) -> tuple[str, str] | None:
    """Extract access_token and ChatGPT account id from Codex credentials JSON."""
    try:
        creds_json = normalize_codex_credentials_blob(creds_json) or creds_json
        data = json.loads(creds_json)
        tokens = data.get("tokens", {})
        if not isinstance(tokens, dict):
            tokens = {}
        token = tokens.get("access_token") or data.get("access_token")
        account_id = tokens.get("account_id") or data.get("account_id")
        if not account_id and tokens.get("id_token"):
            payload = _decode_jwt_payload(tokens["id_token"])
            auth_info = payload.get("https://api.openai.com/auth", {}) if payload else {}
            if isinstance(auth_info, dict):
                account_id = auth_info.get("chatgpt_account_id")
        if token and account_id:
            return str(token), str(account_id)
    except (json.JSONDecodeError, AttributeError):
        pass
    return None


def _fetch_codex_usage_once(creds_json: str) -> dict | None:
    """Fetch Codex usage once from the first available ChatGPT backend endpoint."""
    result = _extract_codex_token(creds_json)
    if not result:
        return None
    token, account_id = result

    for url in CODEX_USAGE_URLS:
        req = Request(url)
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("ChatGPT-Account-Id", account_id)
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", "claude-switcher/0.4.3")

        try:
            with urlopen(req, timeout=10) as resp:
                return json.loads(resp.read().decode())
        except HTTPError:
            continue
        except (URLError, TimeoutError, json.JSONDecodeError, OSError):
            continue
    return None


def fetch_codex_usage_with_refresh(creds_json: str) -> tuple[dict | None, str | None]:
    """Fetch usage, refreshing Codex credentials once if the saved access token is stale."""
    normalized = normalize_codex_credentials_blob(creds_json) or creds_json
    usage = _fetch_codex_usage_once(normalized)
    if usage is not None:
        return usage, None

    try:
        refreshed = refresh_codex_credentials(normalized)
    except CodexCredentialsExpiredError:
        return CODEX_LOGIN_REQUIRED_USAGE, None
    if not refreshed:
        return None, None

    return _fetch_codex_usage_once(refreshed), refreshed


def fetch_codex_usage(creds_json: str) -> dict | None:
    """Fetch Codex usage from the first available ChatGPT backend endpoint."""
    usage, _ = fetch_codex_usage_with_refresh(creds_json)
    return usage


def fetch_codex_usage_for_account(
    email: str, config_path=DEFAULT_CONFIG_PATH
) -> dict | None:
    """Fetch usage for a saved Codex account stored in Keychain."""
    service = f"codex-switcher:{email}"
    raw_creds = keychain.read_credentials(service)
    if not raw_creds:
        return None
    creds = normalize_codex_credentials_blob(raw_creds) or raw_creds
    usage = _fetch_codex_usage_once(creds)
    if usage is not None:
        return usage

    with codex_core._CODEX_LOCK:
        if codex_core._add_in_progress:
            return None
        current = keychain.read_credentials(service)
        if current != raw_creds:
            return None
        account = next(
            (
                account for account in load_accounts(config_path)
                if account.provider == "codex" and account.email == email
            ),
            None,
        )
        if account is None or account.active:
            return None
        try:
            refreshed = refresh_codex_credentials(
                normalize_codex_credentials_blob(current) or current
            )
        except CodexCredentialsExpiredError:
            return CODEX_LOGIN_REQUIRED_USAGE
        if not refreshed:
            return None
        keychain.write_credentials(service, email, refreshed)

    return _fetch_codex_usage_once(refreshed)


def fetch_active_codex_usage(config_path=DEFAULT_CONFIG_PATH) -> dict | None:
    """Fetch usage for the currently active Codex session."""
    try:
        raw_creds = codex_core._read_codex_credentials_for_import_raw()
    except RuntimeError:
        return None
    if not raw_creds:
        return None
    creds = normalize_codex_credentials_blob(raw_creds) or raw_creds
    email = codex_core._codex_email_from_credentials(creds)
    usage = _fetch_codex_usage_once(creds)
    if usage is not None:
        return usage

    with codex_core._CODEX_LOCK:
        if codex_core._add_in_progress:
            return None
        current = codex_core._read_codex_credentials_for_import_raw()
        if current != raw_creds:
            return None
        active = get_active_account(config_path, provider="codex")
        if not email or not active or active.email != email:
            return None
        try:
            refreshed = refresh_codex_credentials(
                normalize_codex_credentials_blob(current) or current
            )
        except CodexCredentialsExpiredError:
            return CODEX_LOGIN_REQUIRED_USAGE
        if not refreshed:
            return None
        codex_core._write_codex_credentials(refreshed)
        codex_core._validate_email(email)
        keychain.write_credentials(f"codex-switcher:{email}", email, refreshed)

    return _fetch_codex_usage_once(refreshed)


def _reset_counts(usage: dict | None) -> tuple[int, int]:
    """Read only integer reset counts from the usage payload."""
    credits = usage.get("rate_limit_reset_credits") if isinstance(usage, dict) else None
    if not isinstance(credits, dict):
        return 0, 0
    available = credits.get("available_count")
    applicable = credits.get("applicable_available_count")
    return (
        available if type(available) is int else 0,
        applicable if type(applicable) is int else 0,
    )


def consume_reset_credit(email: str, config_path=DEFAULT_CONFIG_PATH) -> str:
    """Recheck usage, then redeem one reset with an idempotent network retry."""
    key = str(uuid4())
    busy_message = "A Codex account add is in progress. Try again in a moment."
    if codex_core._add_in_progress:
        logger.info("Codex reset email=%s key=%s outcome=add_in_progress", email, key)
        raise RuntimeError(busy_message)

    active = get_active_account(config_path, provider="codex")
    is_active = active is not None and active.email == email
    usage = (
        fetch_active_codex_usage(config_path)
        if is_active else fetch_codex_usage_for_account(email, config_path)
    )
    if not usage:
        logger.info("Codex reset email=%s key=%s outcome=usage_unavailable", email, key)
        raise RuntimeError(f"Usage unavailable for {email}. Cannot confirm a reset can be applied.")
    error = usage.get("error")
    if isinstance(error, dict) and error.get("code") == "login_required":
        logger.info("Codex reset email=%s key=%s outcome=login_required", email, key)
        raise RuntimeError(f"Login required for {email}. Add the account again first.")
    if _reset_counts(usage)[1] <= 0:
        logger.info("Codex reset email=%s key=%s outcome=no_credit", email, key)
        return "no_credit"

    # Read after the existing fetcher's refresh/write-back. Keep a switch or add
    # from replacing the live source between this read and the consumption.
    with codex_core._CODEX_LOCK:
        if codex_core._add_in_progress:
            logger.info("Codex reset email=%s key=%s outcome=add_in_progress", email, key)
            raise RuntimeError(busy_message)
        if is_active:
            current = get_active_account(config_path, provider="codex")
            if current is None or current.email != email:
                logger.info("Codex reset email=%s key=%s outcome=account_changed", email, key)
                raise RuntimeError("The active Codex account changed. Try again.")
            blob = codex_core._read_codex_credentials_for_import_raw()
        else:
            blob = keychain.read_credentials(f"codex-switcher:{email}")
        tokens = _extract_codex_token(blob) if blob else None
        if tokens is None:
            logger.info("Codex reset email=%s key=%s outcome=credentials_unavailable", email, key)
            raise RuntimeError("Codex credentials unavailable. Sign in again.")
        token, account_id = tokens
        req = Request(CODEX_RESET_CONSUME_URL, method="POST",
                      data=json.dumps({"redeem_request_id": key}).encode())
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("ChatGPT-Account-Id", account_id)
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", "claude-switcher/0.4.3")
        req.add_header("Content-Type", "application/json")

        for attempt in range(2):
            status = None
            try:
                with urlopen(req, timeout=10) as resp:
                    status = resp.status
                    payload = json.loads(resp.read().decode())
                code = payload.get("code") if isinstance(payload, dict) else None
                code = code.lower() if isinstance(code, str) else None
                if code not in RESET_CODES:
                    raise ValueError("Unknown reset code")
            except HTTPError as exc:
                logger.info("Codex reset email=%s key=%s outcome=http_%s", email, key, exc.code)
                raise RuntimeError(f"Codex reset failed (HTTP {exc.code}).") from exc
            except (URLError, TimeoutError, ConnectionError, IncompleteRead) as exc:
                status_text = f" (HTTP {status})" if isinstance(status, int) else ""
                logger.info("Codex reset email=%s key=%s outcome=network_error%s", email, key, status_text)
                if attempt == 0:
                    continue
                raise RuntimeError(f"Codex reset failed: connection error or timeout{status_text}.") from exc
            except (ValueError, OSError) as exc:
                status_text = f" (HTTP {status})" if isinstance(status, int) else ""
                logger.info("Codex reset email=%s key=%s outcome=invalid_response%s", email, key, status_text)
                raise RuntimeError(f"Codex reset failed: invalid response{status_text}.") from exc
            logger.info("Codex reset email=%s key=%s outcome=%s", email, key, code)
            return code


def _format_reset_delta(reset_at: float) -> str:
    """Convert a Unix timestamp to a human-readable countdown."""
    try:
        now = datetime.now(timezone.utc)
        target = datetime.fromtimestamp(float(reset_at), tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return "?"

    total_seconds = int((target - now).total_seconds())
    return _format_countdown(total_seconds)


def _window_label(window: dict, fallback: str) -> str:
    """Label a Codex rate-limit window by its real length.

    The API's "primary" window is not always hourly: on the plans seen so far
    it is a 7-day window (limit_window_seconds = 604800), so a fixed "1h"
    label was wrong. Derive the label from limit_window_seconds and fall back
    to the positional label only when the field is missing or malformed.
    """
    try:
        seconds = int(window["limit_window_seconds"])
    except (KeyError, TypeError, ValueError):
        return fallback
    if seconds <= 0:
        return fallback
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    return f"{max(seconds // 60, 1)}m"


def codex_usage_state(usage: dict | None) -> UsageState:
    """Convert Codex usage data into a normalized usage state."""
    if not usage:
        return UsageState(available=False, display="Usage unavailable")

    error = usage.get("error")
    if isinstance(error, dict) and error.get("code") == "login_required":
        return UsageState(available=False, display="Login required")

    rate_limit = usage.get("rate_limit")
    if not isinstance(rate_limit, dict):
        return UsageState(available=False, display="Usage unavailable")

    parts = []
    windows = []
    for fallback, key in (("1h", "primary_window"), ("7d", "secondary_window")):
        window = rate_limit.get(key)
        if not isinstance(window, dict) or "used_percent" not in window:
            continue
        label = _window_label(window, fallback)
        try:
            percent = float(window["used_percent"])
        except (TypeError, ValueError):
            continue
        reset = _format_reset_delta(window["reset_at"]) if "reset_at" in window else None
        reset_suffix = f" ({reset})" if reset else ""
        parts.append(f"{label} {percent:.0f}%{reset_suffix}")
        windows.append(UsageWindow(label=label, percent=percent, resets_in=reset))

    if not parts:
        return UsageState(available=False, display="Usage unavailable")
    available, applicable = _reset_counts(usage)
    suffix = f" · {available} reset{'s' if available != 1 else ''}" if available > 0 else ""
    return UsageState(available=True, display=" | ".join(parts) + suffix, windows=tuple(windows),
                      reset_credits=available, reset_applicable=applicable)


def format_codex_usage(usage: dict | None) -> str:
    """Format Codex usage for menu display."""
    return codex_usage_state(usage).display
