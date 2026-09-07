"""Business logic for account management."""

import json
import logging
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.client import IncompleteRead
from pathlib import Path

from claude_switcher import keychain
from claude_switcher.common import _find_binary, _validate_email
from claude_switcher.config import (
    AccountInfo,
    add_account,
    get_active_account,
    load_accounts,
    remove_account,
    set_active_account,
    _atomic_write,
    DEFAULT_CONFIG_PATH,
)

CLAUDE_SERVICE = keychain.CLAUDE_SERVICE
CLAUDE_STATE_FILE = Path.home() / ".claude.json"

_CLAUDE_LOCK = threading.Lock()
_add_in_progress = False

logger = logging.getLogger(__name__)
CLAUDE_OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLAUDE_OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"


class ClaudeCredentialsExpiredError(RuntimeError):
    """Raised when a saved Claude session needs a new sign-in."""


def refresh_claude_credentials(creds_json: str) -> str | None:
    """Refresh a saved Claude OAuth blob, preserving unrelated credential fields."""
    try:
        data = json.loads(creds_json)
        tokens = data["claudeAiOauth"]
        refresh_token = tokens["refreshToken"]
        if not isinstance(refresh_token, str) or not refresh_token:
            raise ValueError("Missing refresh token")
    except (ValueError, TypeError, KeyError):
        logger.info("Claude refresh outcome=invalid_credentials")
        return None

    req = urllib.request.Request(
        CLAUDE_OAUTH_TOKEN_URL,
        method="POST",
        data=json.dumps({
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": CLAUDE_OAUTH_CLIENT_ID,
        }).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "claude-code/2.1.11",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status != 200:
                logger.info("Claude refresh outcome=unexpected_status")
                return None
            refreshed = json.loads(resp.read())
        access_token = refreshed["access_token"]
        expires_in = refreshed["expires_in"]
        new_refresh_token = refreshed.get("refresh_token", refresh_token)
        if (not isinstance(access_token, str) or not access_token
                or type(expires_in) is not int
                or not isinstance(new_refresh_token, str) or not new_refresh_token
                or ("scope" in refreshed and not isinstance(refreshed["scope"], str))):
            raise ValueError("Invalid refresh response")
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 401):
            logger.info("Claude refresh outcome=login_required")
            raise ClaudeCredentialsExpiredError("This saved Claude session needs a new sign-in.") from None
        logger.info("Claude refresh outcome=http_error")
        return None
    except (urllib.error.URLError, OSError, IncompleteRead):
        logger.info("Claude refresh outcome=network_error")
        return None
    except (ValueError, TypeError, KeyError):
        logger.info("Claude refresh outcome=invalid_response")
        return None

    tokens["accessToken"] = access_token
    tokens["refreshToken"] = new_refresh_token
    tokens["expiresAt"] = int(time.time() * 1000) + expires_in * 1000
    if "scope" in refreshed:
        tokens["scopes"] = refreshed["scope"].split()
    logger.info("Claude refresh outcome=success")
    return json.dumps(data, separators=(",", ":"))


def _read_oauth_account() -> dict | None:
    """Read the oauthAccount object from ~/.claude.json."""
    try:
        data = json.loads(CLAUDE_STATE_FILE.read_text(encoding="utf-8"))
        return data.get("oauthAccount")
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _write_oauth_account(oauth_account: dict) -> None:
    """Write the oauthAccount object into ~/.claude.json (merge, not overwrite)."""
    try:
        data = json.loads(CLAUDE_STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return
    data["oauthAccount"] = oauth_account
    _atomic_write(CLAUDE_STATE_FILE, json.dumps(data))


def check_claude_cli() -> bool:
    """Check if the claude CLI is available."""
    return _find_binary("claude") is not None


def _claude_cmd() -> str:
    """Return the path to the claude binary, or 'claude' as fallback."""
    return _find_binary("claude") or "claude"


def get_auth_status() -> dict | None:
    """Run `claude auth status --json` and return parsed JSON, or None on failure."""
    result = subprocess.run(
        [_claude_cmd(), "auth", "status", "--json"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def run_auth_logout() -> None:
    """Run `claude auth logout`."""
    subprocess.run([_claude_cmd(), "auth", "logout"], capture_output=True, text=True)


CLAUDE_LOGIN_TIMEOUT_SECONDS = 300
_login_cancel = threading.Event()
_login_proc: subprocess.Popen | None = None
_login_proc_lock = threading.Lock()


def cancel_login() -> None:
    """Abort an in-progress Claude sign-in by killing `claude auth login`.

    run_auth_login then returns False and the add flow restores the previous
    credential through its normal cancelled path.
    """
    _login_cancel.set()
    with _login_proc_lock:
        proc = _login_proc
    if proc is not None and proc.poll() is None:
        proc.kill()


def run_auth_login(timeout: int = CLAUDE_LOGIN_TIMEOUT_SECONDS) -> bool:
    """Run `claude auth login`, bounded by a timeout and cancellable.

    This used to be a bare subprocess.run with no timeout, so an abandoned
    login (browser tab closed, callback never arrives) held the add-lease
    forever — until the app was restarted. Returns True only on exit code 0.
    """
    global _login_proc
    # Do NOT clear _login_cancel here; it is armed at lease-acquire in
    # add_new_account so a pre-login Cancel is honoured.
    proc = subprocess.Popen([_claude_cmd(), "auth", "login"])
    with _login_proc_lock:
        _login_proc = proc
    try:
        try:
            returncode = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            return False
        if _login_cancel.is_set():
            return False
        return returncode == 0
    finally:
        with _login_proc_lock:
            _login_proc = None


def import_current_account(config_path: Path = DEFAULT_CONFIG_PATH) -> AccountInfo | None:
    """Import the currently logged-in Claude Code account. Returns AccountInfo or None."""
    # Retry keychain read — after login, credentials may not be written yet
    creds = None
    for _ in range(5):
        creds = keychain.read_credentials(CLAUDE_SERVICE)
        if creds:
            break
        time.sleep(1)
    if not creds:
        return None

    acct_attr = keychain.read_account_attribute(CLAUDE_SERVICE) or "unknown"

    status = get_auth_status()
    if status and status.get("email"):
        email = status["email"]
        sub_type = status.get("subscriptionType", "unknown")
        org_name = status.get("orgName", "")
    else:
        try:
            email = json.loads(creds).get("email", "unknown@unknown")
        except (json.JSONDecodeError, AttributeError):
            return None
        sub_type = "unknown"
        org_name = ""

    _validate_email(email)
    keychain.write_credentials(f"claude-switcher:{email}", acct_attr, creds)

    oauth_account = _read_oauth_account()

    account = AccountInfo(
        email=email,
        subscription_type=sub_type,
        org_name=org_name,
        active=True,
        keychain_account=acct_attr,
        oauth_account=oauth_account,
        provider="claude",
    )
    add_account(account, config_path)
    set_active_account(email, config_path, provider="claude")
    return account


def switch_account(target_email: str, config_path: Path = DEFAULT_CONFIG_PATH) -> None:
    """Switch to a different account. Saves current credentials first."""
    with _CLAUDE_LOCK:
        if _add_in_progress:
            raise RuntimeError("A Claude account add is in progress. Try again in a moment.")

        active = get_active_account(config_path)

        if active:
            # Only back up the live credential if it actually belongs to the
            # account config marks active. ~/.claude.json's oauthAccount
            # identifies the live session; if it has drifted from active.email,
            # saving would overwrite a different account's backup. Skip on drift.
            current_oauth = _read_oauth_account()
            live_email = (current_oauth or {}).get("emailAddress")
            if live_email == active.email:
                current_creds = keychain.read_credentials(CLAUDE_SERVICE)
                if current_creds:
                    keychain.write_credentials(
                        f"claude-switcher:{active.email}", active.keychain_account, current_creds
                    )
                if current_oauth:
                    active.oauth_account = current_oauth
                    add_account(active, config_path)

        _validate_email(target_email)
        target_creds = keychain.read_credentials(f"claude-switcher:{target_email}")
        if not target_creds:
            raise RuntimeError(f"Credentials not found in Keychain for {target_email}")

        accounts = load_accounts(config_path)
        target_account = next(
            (a for a in accounts if a.email == target_email and a.provider == "claude"), None
        )
        if not target_account:
            raise RuntimeError(f"Account {target_email} not found in config")

        keychain.write_credentials(CLAUDE_SERVICE, target_account.keychain_account, target_creds)

        # Restore target's oauthAccount into ~/.claude.json
        if target_account.oauth_account:
            _write_oauth_account(target_account.oauth_account)

        set_active_account(target_email, config_path, provider="claude")


def add_new_account(config_path: Path = DEFAULT_CONFIG_PATH) -> AccountInfo | None:
    """Add a new account via claude auth login. Returns AccountInfo or None if cancelled."""
    global _add_in_progress
    with _CLAUDE_LOCK:
        if _add_in_progress:
            raise RuntimeError("A Claude account add is already in progress.")
        _add_in_progress = True
        # Arm cancellation here, at lease-acquire, not inside run_auth_login:
        # a Cancel clicked during the pre-login snapshot/keychain work must
        # still take effect, not be wiped when the login starts.
        _login_cancel.clear()

    try:
        active = get_active_account(config_path)
        snapshot = keychain.snapshot_credentials(CLAUDE_SERVICE)
        if snapshot is not None:
            keychain._single_line(snapshot[1])
        # Back up the outgoing credential under active.email ONLY if the live
        # session actually belongs to that account. Same identity guard as
        # switch_account: on drift, skip rather than clobber a different backup.
        if active and snapshot is not None:
            live_email = (_read_oauth_account() or {}).get("emailAddress")
            if live_email == active.email:
                keychain.write_credentials(
                    f"claude-switcher:{active.email}", snapshot[0], snapshot[1]
                )

        result = None
        try:
            # Do NOT run `claude auth logout` here. It revokes the *previous*
            # account's session server-side, which permanently invalidates the
            # backup we just saved for it — so adding account B would silently
            # kill account A. Clearing the local Keychain slot below is all the
            # fresh login needs; the old account's server session stays valid so
            # it can be switched back to later.
            while keychain.delete_credentials(CLAUDE_SERVICE):
                pass

            if run_auth_login():
                result = import_current_account(config_path)
        except BaseException as original:
            if snapshot is not None:
                try:
                    keychain.restore_credentials(CLAUDE_SERVICE, snapshot)
                except BaseException as restore_error:
                    raise original from restore_error
            raise

        if result is None and snapshot is not None:
            keychain.restore_credentials(CLAUDE_SERVICE, snapshot)
        return result
    finally:
        with _CLAUDE_LOCK:
            _add_in_progress = False


def remove_saved_account(email: str, config_path: Path = DEFAULT_CONFIG_PATH) -> None:
    """Remove a saved account from config and Keychain."""
    with _CLAUDE_LOCK:
        if _add_in_progress:
            raise RuntimeError("A Claude account add is in progress. Try again in a moment.")
        service = f"claude-switcher:{email}"
        snapshot = keychain.snapshot_credentials(service)
        if snapshot is not None:
            keychain._single_line(snapshot[1])
        try:
            keychain.delete_credentials(service)
            remove_account(email, config_path)
        except BaseException as original:
            if snapshot is not None:
                try:
                    keychain.restore_credentials(service, snapshot)
                except BaseException as restore_error:
                    raise original from restore_error
            raise
