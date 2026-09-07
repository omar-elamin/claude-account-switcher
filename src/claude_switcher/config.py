"""Account list persistence in ~/.config/claude-switcher/accounts.json."""

import json
import os
import tempfile
import threading
from dataclasses import dataclass, asdict, field
from pathlib import Path

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "claude-switcher" / "accounts.json"
CONFIG_VERSION = 2
DEFAULT_PROVIDERS = ("claude", "codex")
_LOCK = threading.RLock()


@dataclass
class AccountInfo:
    email: str
    subscription_type: str
    org_name: str
    active: bool
    keychain_account: str
    oauth_account: dict | None = None
    provider: str = "claude"


@dataclass
class AppSettings:
    auto_switch: dict[str, bool] = field(
        default_factory=lambda: {provider: False for provider in DEFAULT_PROVIDERS}
    )
    auto_switch_threshold: float = 100.0
    auto_reset: dict[str, bool] = field(default_factory=lambda: {"codex": False})
    proactive_switch: bool = True


def _default_settings_dict() -> dict:
    return asdict(AppSettings())


def _read_config_data(path: Path = DEFAULT_CONFIG_PATH) -> dict:
    """Read raw config JSON. Returns a valid empty config shape on failure."""
    if not path.exists():
        return {"version": CONFIG_VERSION, "settings": _default_settings_dict(), "accounts": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, OSError):
        return {"version": CONFIG_VERSION, "settings": _default_settings_dict(), "accounts": []}
    if not isinstance(data, dict):
        return {"version": CONFIG_VERSION, "settings": _default_settings_dict(), "accounts": []}
    return data


def _atomic_write(path: Path, content: str | bytes, mode: int = 0o600) -> None:
    """Atomically replace a file through a private temp file beside its referent."""
    target = Path(os.path.realpath(path))
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    raw = content.encode("utf-8") if isinstance(content, str) else content
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", delete=False, dir=target.parent
        ) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(raw)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.chmod(temp_path, mode)
        os.replace(temp_path, target)
        temp_path = None
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass


def _backup_corrupt_config(path: Path) -> None:
    """Preserve damaged config bytes once before replacing them."""
    try:
        original = path.read_bytes()
    except FileNotFoundError:
        return

    corrupt = False
    try:
        raw_data = json.loads(original.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        corrupt = True
    else:
        if not isinstance(raw_data, dict):
            corrupt = True
        elif "accounts" in raw_data and not isinstance(raw_data["accounts"], list):
            corrupt = True
        elif isinstance(raw_data.get("accounts"), list) and len(load_accounts(path)) < len(
            raw_data["accounts"]
        ):
            corrupt = True

    if not corrupt:
        return

    backup = path.with_name(f"{path.name}.corrupt")
    suffix = 1
    while os.path.lexists(backup):
        backup = path.with_name(f"{path.name}.corrupt.{suffix}")
        suffix += 1
    _atomic_write(backup, original)


def _write_config_data(data: dict, path: Path = DEFAULT_CONFIG_PATH) -> None:
    """Write raw config JSON with private file permissions."""
    _backup_corrupt_config(path)
    data.setdefault("version", CONFIG_VERSION)
    data.setdefault("settings", _default_settings_dict())
    data.setdefault("accounts", [])
    _atomic_write(path, json.dumps(data, indent=2))


def _settings_from_dict(data: dict | None) -> AppSettings:
    defaults = AppSettings()
    if not isinstance(data, dict):
        return defaults

    auto_switch = dict(defaults.auto_switch)
    raw_auto_switch = data.get("auto_switch")
    if isinstance(raw_auto_switch, dict):
        for provider, enabled in raw_auto_switch.items():
            if isinstance(provider, str):
                auto_switch[provider] = bool(enabled)

    threshold = defaults.auto_switch_threshold
    try:
        threshold = float(data.get("auto_switch_threshold", threshold))
    except (TypeError, ValueError):
        threshold = defaults.auto_switch_threshold

    auto_reset = dict(defaults.auto_reset)
    raw_auto_reset = data.get("auto_reset")
    if isinstance(raw_auto_reset, dict):
        for provider, enabled in raw_auto_reset.items():
            if isinstance(provider, str):
                auto_reset[provider] = bool(enabled)

    proactive_switch = data.get("proactive_switch", defaults.proactive_switch)
    if not isinstance(proactive_switch, (bool, int, float, str)):
        proactive_switch = defaults.proactive_switch

    return AppSettings(auto_switch=auto_switch, auto_switch_threshold=threshold,
                       auto_reset=auto_reset, proactive_switch=bool(proactive_switch))


def load_accounts(path: Path = DEFAULT_CONFIG_PATH) -> list[AccountInfo]:
    """Load accounts from JSON file. Returns empty list if file doesn't exist."""
    data = _read_config_data(path)
    accounts = data.get("accounts", [])
    if not isinstance(accounts, list):
        return []

    loaded = []
    for acc in accounts:
        if not isinstance(acc, dict):
            continue
        fields = {k: v for k, v in acc.items() if k in AccountInfo.__dataclass_fields__}
        try:
            loaded.append(AccountInfo(**fields))
        except TypeError:
            continue
    return loaded


def save_accounts(accounts: list[AccountInfo], path: Path = DEFAULT_CONFIG_PATH) -> None:
    """Save accounts to JSON file, preserving app settings."""
    with _LOCK:
        data = _read_config_data(path)
        data["version"] = CONFIG_VERSION
        data["settings"] = asdict(_settings_from_dict(data.get("settings")))
        data["accounts"] = [asdict(acc) for acc in accounts]
        _write_config_data(data, path)


def add_account(account: AccountInfo, path: Path = DEFAULT_CONFIG_PATH) -> None:
    """Add or update an account, matched by email and provider."""
    with _LOCK:
        accounts = load_accounts(path)
        accounts = [
            a for a in accounts
            if not (a.email == account.email and a.provider == account.provider)
        ]
        accounts.append(account)
        save_accounts(accounts, path)


def remove_account(email: str, path: Path = DEFAULT_CONFIG_PATH, provider: str = "claude") -> None:
    """Remove an account by email and provider."""
    with _LOCK:
        accounts = load_accounts(path)
        accounts = [a for a in accounts if not (a.email == email and a.provider == provider)]
        save_accounts(accounts, path)


def get_active_account(
    path: Path = DEFAULT_CONFIG_PATH, provider: str = "claude"
) -> AccountInfo | None:
    """Return the active account for a provider, or None."""
    for acc in load_accounts(path):
        if acc.active and acc.provider == provider:
            return acc
    return None


def set_active_account(
    email: str, path: Path = DEFAULT_CONFIG_PATH, provider: str = "claude"
) -> None:
    """Set an account active within a provider, deactivating only that provider."""
    with _LOCK:
        accounts = load_accounts(path)
        for acc in accounts:
            if acc.provider == provider:
                acc.active = (acc.email == email)
        save_accounts(accounts, path)


def load_settings(path: Path = DEFAULT_CONFIG_PATH) -> AppSettings:
    """Load persisted app settings with defaults for missing fields."""
    data = _read_config_data(path)
    return _settings_from_dict(data.get("settings"))


def save_settings(settings: AppSettings, path: Path = DEFAULT_CONFIG_PATH) -> None:
    """Persist app settings without modifying accounts."""
    with _LOCK:
        data = _read_config_data(path)
        data["version"] = CONFIG_VERSION
        data["settings"] = asdict(settings)
        data["accounts"] = data.get("accounts", [])
        _write_config_data(data, path)


def is_auto_switch_enabled(provider: str, path: Path = DEFAULT_CONFIG_PATH) -> bool:
    """Return whether auto-switch is enabled for a provider."""
    settings = load_settings(path)
    return bool(settings.auto_switch.get(provider, False))


def set_auto_switch_enabled(
    provider: str, enabled: bool, path: Path = DEFAULT_CONFIG_PATH
) -> None:
    """Enable or disable auto-switch for one provider."""
    with _LOCK:
        settings = load_settings(path)
        settings.auto_switch[provider] = bool(enabled)
        save_settings(settings, path)


def is_auto_reset_enabled(provider: str, path: Path = DEFAULT_CONFIG_PATH) -> bool:
    """Return whether auto-reset is enabled for a provider."""
    return bool(load_settings(path).auto_reset.get(provider, False))


def set_auto_reset_enabled(
    provider: str, enabled: bool, path: Path = DEFAULT_CONFIG_PATH
) -> None:
    """Enable or disable auto-reset for one provider."""
    with _LOCK:
        settings = load_settings(path)
        settings.auto_reset[provider] = bool(enabled)
        save_settings(settings, path)


def set_proactive_switch_enabled(
    enabled: bool, path: Path = DEFAULT_CONFIG_PATH
) -> None:
    """Enable or disable switching before the active account is exhausted."""
    with _LOCK:
        settings = load_settings(path)
        settings.proactive_switch = bool(enabled)
        save_settings(settings, path)
