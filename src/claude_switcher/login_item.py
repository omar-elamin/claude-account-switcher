"""Manage the per-user launchd agent for starting at login."""

import os
import plistlib
import subprocess
from pathlib import Path


LABEL = "com.emilejouannet.claude-switcher"


def plist_path(home: Path = Path.home()) -> Path:
    return home / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def is_enabled(home: Path = Path.home()) -> bool:
    return plist_path(home).exists()


def enable(bundle_path: Path, home: Path = Path.home(), run=subprocess.run) -> None:
    path = plist_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as file:
        plistlib.dump({
            "Label": LABEL,
            "ProgramArguments": [str(bundle_path / "Contents" / "MacOS" / "Claude Switcher")],
            "RunAtLoad": True,
            "KeepAlive": False,
            "ProcessType": "Interactive",
        }, file)

    domain = f"gui/{os.getuid()}"
    try:
        result = run(["launchctl", "bootstrap", domain, str(path)], capture_output=True, text=True)
        if result.returncode and (result.returncode == 5 or "already" in (result.stderr or "").lower()):
            result = run(["launchctl", "enable", f"{domain}/{LABEL}"], capture_output=True, text=True)
    except OSError as exc:
        raise RuntimeError(f"Could not enable start at login: {exc}") from exc
    if result.returncode:
        raise RuntimeError(f"Could not enable start at login: {(result.stderr or '').strip() or 'launchctl failed.'}")


def disable(home: Path = Path.home(), run=subprocess.run) -> None:
    path = plist_path(home)
    if not path.exists():
        return
    try:
        run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True, text=True)
    except OSError:
        pass
    path.unlink(missing_ok=True)
