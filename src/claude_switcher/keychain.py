"""Wrapper around macOS `security` CLI for Keychain credential management.

Note on process-argument exposure (audit finding 2): writes deliver the secret
to `security add-generic-password` as a hex string via -X. The plaintext token
is never on the command line, but the hex is, and hex is trivially reversible,
so `ps` exposure is reduced, not eliminated. This is a deliberate trade: the
alternative that fully hides the secret (piping it to the -w prompt) truncates at
128 characters via readpassphrase() and silently corrupts every real credential.
Fully eliminating argument exposure requires the Security framework SecItemAdd
API instead of the `security` CLI, which is a larger change than this fix set.
"""

import json
import re
import subprocess
import threading

CLAUDE_SERVICE = "Claude Code-credentials"
KEYCHAIN_TIMEOUT_SECONDS = 5
_LOCK = threading.RLock()


def _single_line(value: str) -> str:
    """Return a value safe for security's line-delimited stdin protocol."""
    if "\n" not in value and "\r" not in value:
        return value
    try:
        return json.dumps(json.loads(value))
    except json.JSONDecodeError as exc:
        raise ValueError(
            "Keychain credentials must be single-line or valid JSON; refusing to truncate them."
        ) from exc


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


def _add_password(service: str, account: str, value: str, *, error: str, timeout_error: str) -> None:
    """Add one Keychain entry, delivering the secret as hex via -X.

    macOS `security add-generic-password` reads the -w prompt through
    readpassphrase(), which truncates at 128 characters. Piping the secret to
    that prompt therefore silently corrupts any real credential (a Claude/Codex
    blob is thousands of bytes). The -X flag takes the value as a hex string and
    has no such limit, so it round-trips long blobs intact. The secret is not
    passed as cleartext on the command line; the hex is still visible in `ps`,
    so this reduces but does not eliminate process-argument exposure (fully
    eliminating it requires the Security framework SecItemAdd API rather than the
    `security` CLI — see the module note).
    """
    hex_value = value.encode("utf-8").hex()
    try:
        result = subprocess.run(
            [
                "security", "add-generic-password",
                "-s", service,
                "-a", account,
                "-X", hex_value,
            ],
            capture_output=True,
            text=True,
            timeout=KEYCHAIN_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(timeout_error) from exc
    if result.returncode != 0:
        raise RuntimeError(error)


def write_credentials(service: str, account: str, password: str) -> None:
    """Write a Keychain entry, replacing all existing entries for that service."""
    with _LOCK:
        snapshot = snapshot_credentials(service)
        value = _single_line(password)
        snapshot_value = _single_line(snapshot[1]) if snapshot is not None else None
        add_attempted = False

        try:
            while delete_credentials(service):
                pass

            add_attempted = True
            _add_password(
                service, account, value,
                error="Keychain write failed. Check macOS Keychain access permissions.",
                timeout_error="Keychain write timed out. Check macOS Keychain access permissions.",
            )
        except BaseException as original:
            rollback_error = None
            if add_attempted:
                try:
                    while delete_credentials(service):
                        pass
                except BaseException as exc:
                    rollback_error = exc

            if snapshot is not None:
                try:
                    _add_password(
                        service, snapshot[0], snapshot_value,
                        error="Keychain restore failed. Check macOS Keychain access permissions.",
                        timeout_error="Keychain restore timed out. Check macOS Keychain access permissions.",
                    )
                except BaseException as exc:
                    rollback_error = exc

            if rollback_error is not None:
                raise original from rollback_error
            raise


def delete_credentials(service: str) -> bool:
    """Delete a Keychain entry. Returns True if deleted, False if not found."""
    with _LOCK:
        try:
            result = subprocess.run(
                ["security", "delete-generic-password", "-s", service],
                capture_output=True,
                text=True,
                timeout=KEYCHAIN_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                "Keychain delete timed out. Check macOS Keychain access permissions."
            ) from exc
        if result.returncode == 44:
            return False
        if result.returncode != 0:
            raise RuntimeError("Keychain delete failed. Check macOS Keychain access permissions.")
        return True


def snapshot_credentials(service: str) -> tuple[str, str] | None:
    """Strictly read a restorable (account, password) Keychain snapshot."""
    with _LOCK:
        try:
            account_result = subprocess.run(
                ["security", "find-generic-password", "-s", service],
                capture_output=True,
                text=True,
                timeout=KEYCHAIN_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("Keychain snapshot timed out.") from exc
        if account_result.returncode == 44:
            return None
        if account_result.returncode != 0:
            raise RuntimeError("Keychain snapshot failed while reading the account attribute.")
        match = re.search(r'"acct"<blob>="([^"]*)"', account_result.stdout)
        if not match:
            raise RuntimeError("Keychain snapshot failed: account attribute was missing.")

        try:
            password_result = subprocess.run(
                ["security", "find-generic-password", "-s", service, "-w"],
                capture_output=True,
                text=True,
                timeout=KEYCHAIN_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("Keychain snapshot timed out.") from exc
        if password_result.returncode == 44:
            return None
        if password_result.returncode != 0:
            raise RuntimeError("Keychain snapshot failed while reading the password.")
        return match.group(1), password_result.stdout.strip()


def restore_credentials(service: str, snapshot: tuple[str, str] | None) -> None:
    """Idempotently restore a strict Keychain snapshot."""
    if snapshot is None:
        return
    with _LOCK:
        current = snapshot_credentials(service)
        target = (snapshot[0], _single_line(snapshot[1]))
        # Intact already: either exactly the snapshot (legacy multi-line values
        # are stored raw) or its single-line form. Either way, do not delete.
        if current == snapshot or current == target:
            return
        write_credentials(service, target[0], target[1])


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
