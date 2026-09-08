"""Start at login via a per-user launch agent file.

Enabling writes ~/Library/LaunchAgents/<LABEL>.plist; disabling removes it. macOS loads
LaunchAgents at login, so both take effect at the next login. Nothing here calls
launchctl on purpose: bootstrapping the agent while the app is running starts a second
copy of the app (RunAtLoad), and booting it out would quit the app when it was started by
launchd. Enabled state is simply whether the file exists.
"""

import plistlib
from pathlib import Path

LABEL = "com.emilejouannet.claude-switcher"


def plist_path(home: Path = Path.home()) -> Path:
    return home / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def is_enabled(home: Path = Path.home()) -> bool:
    return plist_path(home).exists()


def plist_contents(bundle_path: Path) -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": [str(Path(bundle_path) / "Contents" / "MacOS" / "Claude Switcher")],
        "RunAtLoad": True,
        "KeepAlive": False,
        "ProcessType": "Interactive",
    }


def enable(bundle_path: Path, home: Path = Path.home()) -> None:
    """Write the launch agent for the given bundle. Takes effect at the next login."""
    path = plist_path(home)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as file:
            plistlib.dump(plist_contents(bundle_path), file)
    except OSError as exc:
        raise RuntimeError(f"Could not write the launch agent: {exc}") from exc


def disable(home: Path = Path.home()) -> None:
    """Remove the launch agent. Takes effect at the next login; the running app is untouched."""
    path = plist_path(home)
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise RuntimeError(f"Could not remove the launch agent: {exc}") from exc
