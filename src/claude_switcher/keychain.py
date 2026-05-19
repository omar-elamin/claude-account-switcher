"""Wrapper around macOS `security` CLI for Keychain credential management."""

import re
import subprocess

CLAUDE_SERVICE = "Claude Code-credentials"
KEYCHAIN_TIMEOUT_SECONDS = 5


def read_credentials(service: str) -> str | None:
    """Read the password blob for a Keychain entry. Returns None if not found."""
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
            capture_output=True,
            text=True,
            timeout=KEYCHAIN_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() if result.stdout.strip() else None


def write_credentials(service: str, account: str, password: str) -> None:
    """Write a Keychain entry, replacing all existing entries for that service."""
    # Delete all existing entries for this service (there may be duplicates)
    while True:
        try:
            r = subprocess.run(
                ["security", "delete-generic-password", "-s", service],
                capture_output=True,
                text=True,
                timeout=KEYCHAIN_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("Keychain delete timed out. Check macOS Keychain access permissions.") from exc
        if r.returncode != 0:
            break

    try:
        result = subprocess.run(
            [
                "security", "add-generic-password",
                "-s", service,
                "-a", account,
                "-w", password,
            ],
            capture_output=True,
            text=True,
            timeout=KEYCHAIN_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Keychain write timed out. Check macOS Keychain access permissions.") from exc
    if result.returncode != 0:
        raise RuntimeError("Keychain write failed. Check macOS Keychain access permissions.")


def delete_credentials(service: str) -> bool:
    """Delete a Keychain entry. Returns True if deleted, False if not found."""
    try:
        result = subprocess.run(
            ["security", "delete-generic-password", "-s", service],
            capture_output=True,
            text=True,
            timeout=KEYCHAIN_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return False
    return result.returncode == 0


def read_account_attribute(service: str) -> str | None:
    """Read the account (-a) attribute of a Keychain entry by parsing security output."""
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service],
            capture_output=True,
            text=True,
            timeout=KEYCHAIN_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    match = re.search(r'"acct"<blob>="([^"]*)"', result.stdout)
    return match.group(1) if match else None
