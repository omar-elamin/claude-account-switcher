"""Business logic for Codex CLI account management."""

import base64
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised only on Python 3.10
    import tomli as tomllib

from claude_switcher import keychain
from claude_switcher.config import (
    AccountInfo,
    add_account,
    get_active_account,
    load_accounts,
    remove_account,
    save_accounts,
    set_active_account,
    DEFAULT_CONFIG_PATH,
)

CODEX_HOME = Path.home() / ".codex"
CODEX_AUTH_FILE = CODEX_HOME / "auth.json"
CODEX_CONFIG_FILE = CODEX_HOME / "config.toml"
CODEX_KEYCHAIN_PREFIX = "codex-switcher:"
CODEX_LOGIN_TIMEOUT_SECONDS = 300
CODEX_KEYRING_UNSUPPORTED_MESSAGE = (
    "Codex keyring credential storage is not supported yet. "
    'Set cli_auth_credentials_store = "file" in ~/.codex/config.toml and run codex login.'
)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_EXTRA_PATHS = [
    Path.home() / ".local" / "bin",
    Path("/usr/local/bin"),
    Path("/opt/homebrew/bin"),
]


def _find_codex() -> str | None:
    """Find the codex binary, checking common install locations beyond PATH."""
    found = shutil.which("codex")
    if found:
        return found
    for directory in _EXTRA_PATHS:
        candidate = directory / "codex"
        if candidate.is_file():
            return str(candidate)
    return None


def check_codex_cli() -> bool:
    """Check if the Codex CLI is available."""
    return _find_codex() is not None


def _codex_cmd() -> str:
    """Return the path to the Codex binary, or 'codex' as fallback."""
    return _find_codex() or "codex"


def get_codex_auth_status() -> dict | None:
    """Run `codex login status`. Returns a simple dict on success."""
    result = subprocess.run(
        [_codex_cmd(), "login", "status"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return {"loggedIn": True, "message": result.stdout.strip() or result.stderr.strip()}


def _validate_email(email: str) -> str:
    """Validate email before using it in Keychain service names."""
    if not _EMAIL_RE.match(email) or len(email) > 254:
        raise RuntimeError(f"Invalid email format: {email}")
    return email


def _codex_credentials_store() -> str:
    """Read the configured Codex credential store."""
    try:
        data = tomllib.loads(CODEX_CONFIG_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, tomllib.TOMLDecodeError):
        return "auto"
    store = data.get("cli_auth_credentials_store", "auto")
    return store if store in {"file", "keyring", "auto"} else "auto"


def _read_codex_credentials_from_file() -> str | None:
    try:
        content = CODEX_AUTH_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return content or None


def read_codex_credentials() -> str | None:
    """Read Codex credentials from ~/.codex/auth.json."""
    store = _codex_credentials_store()
    if store == "keyring":
        raise RuntimeError(CODEX_KEYRING_UNSUPPORTED_MESSAGE)

    creds = _read_codex_credentials_from_file()
    if not creds and store == "auto":
        raise RuntimeError(CODEX_KEYRING_UNSUPPORTED_MESSAGE)
    return creds


def _read_codex_credentials_for_import() -> str | None:
    """Read file-mode credentials without treating a missing file as a hard failure."""
    store = _codex_credentials_store()
    if store == "keyring":
        raise RuntimeError(CODEX_KEYRING_UNSUPPORTED_MESSAGE)
    return _read_codex_credentials_from_file()


def _decode_jwt_payload(token: str) -> dict | None:
    """Decode a JWT payload without verification."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        payload = parts[1]
        payload += "=" * ((4 - len(payload) % 4) % 4)
        decoded = base64.urlsafe_b64decode(payload)
        return json.loads(decoded)
    except Exception:
        return None


def _credentials_data(creds_json: str | None = None) -> dict | None:
    try:
        raw = creds_json if creds_json is not None else read_codex_credentials()
        return json.loads(raw) if raw else None
    except (json.JSONDecodeError, RuntimeError):
        return None


def _codex_email_from_credentials(creds_json: str | None = None) -> str | None:
    """Extract email from Codex credentials."""
    data = _credentials_data(creds_json)
    if not data:
        return None

    tokens = data.get("tokens", {})
    if isinstance(tokens, dict):
        id_token = tokens.get("id_token")
        if id_token:
            payload = _decode_jwt_payload(id_token)
            if payload and isinstance(payload.get("email"), str):
                return payload["email"]

    for key in ("email", "user", "account"):
        value = data.get(key)
        if isinstance(value, str) and _EMAIL_RE.match(value):
            return value
    return None


def _codex_plan_from_credentials(creds_json: str | None = None) -> str:
    """Extract ChatGPT plan type from Codex credentials."""
    data = _credentials_data(creds_json)
    if not data:
        return "chatgpt"

    tokens = data.get("tokens", {})
    id_token = tokens.get("id_token") if isinstance(tokens, dict) else None
    if id_token:
        payload = _decode_jwt_payload(id_token)
        if payload:
            auth_info = payload.get("https://api.openai.com/auth", {})
            if isinstance(auth_info, dict) and auth_info.get("chatgpt_plan_type"):
                return str(auth_info["chatgpt_plan_type"])
    return str(data.get("auth_mode") or "chatgpt")


def _saved_codex_account(email: str, config_path: Path) -> AccountInfo | None:
    return next(
        (a for a in load_accounts(config_path) if a.provider == "codex" and a.email == email),
        None,
    )


def import_current_codex_account(config_path: Path = DEFAULT_CONFIG_PATH) -> AccountInfo | None:
    """Import the currently logged-in Codex account."""
    creds = _read_codex_credentials_for_import()
    if not creds or get_codex_auth_status() is None:
        return None

    email = _codex_email_from_credentials(creds)
    if not email:
        return None
    _validate_email(email)

    keychain.write_credentials(f"{CODEX_KEYCHAIN_PREFIX}{email}", email, creds)

    account = AccountInfo(
        email=email,
        subscription_type=_codex_plan_from_credentials(creds),
        org_name="",
        active=True,
        keychain_account=email,
        provider="codex",
    )
    add_account(account, config_path)
    set_active_account(email, config_path, provider="codex")
    return account


def _write_codex_credentials(creds: str) -> None:
    """Write credentials back to ~/.codex/auth.json."""
    CODEX_AUTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    CODEX_AUTH_FILE.write_text(creds, encoding="utf-8")
    CODEX_AUTH_FILE.chmod(0o600)


def run_codex_logout() -> None:
    """Run `codex logout`."""
    subprocess.run([_codex_cmd(), "logout"], capture_output=True, text=True)


def _clear_codex_credentials_file() -> None:
    """Remove the active file-mode Codex credentials before a fresh login."""
    try:
        CODEX_AUTH_FILE.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _launch_codex_login_terminal() -> None:
    """Open Terminal.app and run an interactive Codex login command."""
    script_path = Path(tempfile.gettempdir()) / f"claude-switcher-codex-login-{os.getpid()}.command"
    codex_cmd = shlex.quote(_codex_cmd())
    script = f"""#!/bin/zsh
echo "Claude Switcher - Codex login"
echo ""
echo "Complete the Codex login flow in this Terminal window."
echo "When login succeeds, return to Claude Switcher."
echo ""
{codex_cmd} login -c 'cli_auth_credentials_store="file"'
status=$?
echo ""
if [ $status -eq 0 ]; then
  echo "Codex login completed. You can close this window."
else
  echo "Codex login failed or was cancelled. You can close this window."
fi
echo ""
read -k 1 "?Press any key to close..."
exit $status
"""
    script_path.write_text(script, encoding="utf-8")
    script_path.chmod(0o700)

    result = subprocess.run(
        ["open", str(script_path)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError("Could not open Terminal for Codex login.")


def run_codex_login(timeout: int = CODEX_LOGIN_TIMEOUT_SECONDS) -> bool:
    """Open a visible Codex login flow and wait for file credentials."""
    _launch_codex_login_terminal()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        creds = _read_codex_credentials_from_file()
        if creds and _codex_email_from_credentials(creds):
            return True
        time.sleep(2)
    return False


def switch_codex_account(target_email: str, config_path: Path = DEFAULT_CONFIG_PATH) -> None:
    """Switch to a different Codex account, saving current credentials first."""
    active = get_active_account(config_path, provider="codex")

    if active:
        current_creds = _read_codex_credentials_for_import()
        if current_creds:
            keychain.write_credentials(
                f"{CODEX_KEYCHAIN_PREFIX}{active.email}",
                active.keychain_account,
                current_creds,
            )

    _validate_email(target_email)
    target_creds = keychain.read_credentials(f"{CODEX_KEYCHAIN_PREFIX}{target_email}")
    if not target_creds:
        raise RuntimeError(f"Credentials not found for Codex account {target_email}")

    accounts = load_accounts(config_path)
    target_account = next(
        (a for a in accounts if a.email == target_email and a.provider == "codex"),
        None,
    )
    if not target_account:
        raise RuntimeError(f"Codex account {target_email} not found in config")

    _write_codex_credentials(target_creds)
    for account in accounts:
        if account.provider == "codex":
            account.active = account.email == target_email
    save_accounts(accounts, config_path)


def add_new_codex_account(config_path: Path = DEFAULT_CONFIG_PATH) -> AccountInfo | None:
    """Add a Codex account via `codex login`."""
    active = get_active_account(config_path, provider="codex")
    current_creds = _read_codex_credentials_for_import()
    current_email = _codex_email_from_credentials(current_creds)

    if current_creds and current_email:
        if not _saved_codex_account(current_email, config_path):
            return import_current_codex_account(config_path)
        keychain.write_credentials(
            f"{CODEX_KEYCHAIN_PREFIX}{current_email}",
            current_email,
            current_creds,
        )
    elif active:
        current_creds = keychain.read_credentials(f"{CODEX_KEYCHAIN_PREFIX}{active.email}")

    run_codex_logout()
    _clear_codex_credentials_file()

    if not run_codex_login():
        if current_creds:
            _write_codex_credentials(current_creds)
        return None

    try:
        return import_current_codex_account(config_path)
    except Exception:
        if current_creds:
            _write_codex_credentials(current_creds)
        return None


def remove_codex_account(email: str, config_path: Path = DEFAULT_CONFIG_PATH) -> None:
    """Remove a saved Codex account from config and Keychain."""
    keychain.delete_credentials(f"{CODEX_KEYCHAIN_PREFIX}{email}")
    remove_account(email, config_path, provider="codex")
