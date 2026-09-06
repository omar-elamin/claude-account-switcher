"""Shared credential and countdown helpers."""

import base64
import json
import re
import shutil
from pathlib import Path

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _validate_email(email: str) -> str:
    """Validate email before using it in Keychain service names."""
    if not _EMAIL_RE.match(email) or len(email) > 254:
        raise RuntimeError(f"Invalid email format: {email}")
    return email


_EXTRA_PATHS = [
    Path.home() / ".local" / "bin",
    Path("/usr/local/bin"),
    Path("/opt/homebrew/bin"),
]


def _find_binary(name: str) -> str | None:
    """Find a CLI binary, checking common install locations beyond PATH."""
    found = shutil.which(name)
    if found:
        return found
    for d in _EXTRA_PATHS:
        candidate = d / name
        if candidate.is_file():
            return str(candidate)
    return None


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


def _format_countdown(total_seconds: int) -> str:
    """Render whole seconds as days, hours, minutes, or now."""
    if total_seconds <= 0:
        return "now"
    days = total_seconds // 86400
    hours = (total_seconds % 86400) // 3600
    minutes = (total_seconds % 3600) // 60
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"
