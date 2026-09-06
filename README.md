# Claude Switcher

A macOS menu-bar app that keeps several Claude Code and Codex CLI accounts signed in at once and switches the active one from the menu bar. Version 0.4.3. MIT license.

The app lives in the menu bar only. It has no Dock icon and no Cmd-Tab entry. Claude and Codex are handled independently: switching one never changes the other. The same email can exist once for Claude and once for Codex.

This repository is a fork of [Symbioose/claude-account-switcher](https://github.com/Symbioose/claude-account-switcher). Upstream publishes a Homebrew cask (`brew install --cask Symbioose/tap/claude-switcher`) and zip releases. Those are upstream builds and do not contain this fork's fixes. This fork is built from source with `./build_local.sh` (see [Build from source](#build-from-source)).

![Claude Switcher screenshot](screenshot.png)

## Features

- Switch the active Claude Code account without logging out and back in
- Switch the active Codex CLI account the same way
- Live usage for every saved account, shown under each row in the menu
- Optional auto-switch, per provider, when the active account reaches its limit
- First launch imports the Claude and Codex accounts that are already signed in
- Saved credentials live in macOS Keychain, not in a config file

## Install

There are no releases for this fork. Build it from source:

```bash
git clone https://github.com/omar-elamin/claude-account-switcher.git
cd claude-account-switcher
./build_local.sh
# Output: dist/Claude Switcher.app
```

Drag `dist/Claude Switcher.app` to `/Applications` and launch it. It appears as a menu bar icon.

The app is not notarized, so macOS may block it on first launch. Open **System Settings → Privacy & Security** and click **Open Anyway**.

## Usage

### The menu

From top to bottom:

- A header per provider: `── Claude Code ──` and `── Codex CLI ──`.
- Under each header, one row per saved account, shown as `email (plan)`, with the active account marked and a usage line underneath. Click a row to switch to that account. A Codex row whose saved session has expired shows `Login required`; clicking it opens the Codex login instead of switching. Rows show `•••` until the first fetch finishes, `Checking…` on rows that came back unavailable while a quick retry is pending, and `Usage unavailable` when retries are exhausted. (The Claude row says `Usage indisponible`, a French leftover.)
- `Auto-switch` submenu with one item per provider, labelled `Claude Code` and `Codex CLI`, with a checkmark when enabled. Clicking one toggles it, and a notification says "Enabled" or "Disabled".
- `✚ Add Claude account...` and `✚ Add Codex account...`
- `↻ Refresh usage`
- `− Remove account` submenu. It lists every saved account as `[Claude] email` or `[Codex] email`, including the active one. Choosing the active account shows an alert instead of removing it: "You cannot remove the active Claude Code account. Switch first." (or "… active Codex CLI account …").
- `⏻ Quit`

### Usage display

A Claude row looks like this:

```text
5h 40% (2h 1m) | 7j 20% (1d 5h) | Fable 32% (1d 5h)
```

The first segment is the 5-hour window. The second is the 7-day window, labelled `7j`. After that comes one segment for each model-scoped weekly limit the API reports, labelled with the model's own name (today: `Fable`). The value in parentheses is the time until that window resets. Model-scoped windows are informational only and never trigger auto-switch.

A Codex row looks like this:

```text
7d 39% (2d 11h)
```

There is one segment per rate-limit window the API reports (primary, then secondary), labelled by the window's real length as reported by the API (for example `5h` or `7d`). On the plans seen so far, the primary window is a 7-day window.

Claude usage comes from `https://api.anthropic.com/oauth/usage`, called with each saved account's own token, so every saved account shows its own usage, including inactive ones. Codex usage comes from the chatgpt.com backend usage endpoint. A saved Codex token that needs refreshing is refreshed, and the refreshed token is written back to that account's Keychain backup.

Usage refreshes at launch, every 5 minutes, after adding or switching an account, and when you click `↻ Refresh usage`. If any row is unavailable, the app retries quickly up to 3 times, 6 seconds apart.

macOS closes the menu when you click any item, so `↻ Refresh usage` shows a "Refreshing usage…" notification, then "Usage updated" with the numbers when done. Reopen the menu to see the rows.

### Adding an account

- Claude: the app runs `claude auth login`. Sign in in the browser window that appears.
- Codex: the app opens a Terminal window running `codex login -c 'cli_auth_credentials_store="file"'`. Sign in there.

Before the login starts, the app backs up the current session to Keychain:

- Claude: the app backs up the current session under the active account only if the `oauthAccount` email in `~/.claude.json` matches it. If they differ, it skips the backup rather than overwrite another account's backup.
- Codex: the app backs up the current session under the email embedded in the live `~/.codex/auth.json`. If that email was not saved yet, it is imported as a new saved account.

In both cases the app then clears the live credential slot.

The app does not run `claude auth logout` or `codex logout`. Those commands revoke the previous account's session on the server, which would make its backup unusable. The previous account's server session stays valid, so you can switch back later.

A login times out after 5 minutes. If a login is cancelled, fails, or times out, the previous session is restored.

Clicking Add while a sign-in for that provider is still open cancels it and starts a fresh one ("Restarting … login"). Only one add per provider runs at a time. Switching or removing during an add reports the account as busy; try again in a moment.

### Switching

- Claude: the current live token is backed up to Keychain under `claude-switcher:{email}` (with the same identity check as above). The target's backup is written into Claude Code's credential slot `Claude Code-credentials`, and the `oauthAccount` object in `~/.claude.json` is swapped.
- Codex: the current `~/.codex/auth.json` is backed up under `codex-switcher:{email}`. The target's saved session is written to `~/.codex/auth.json` with `0600` permissions.

Verify after switching:

```bash
claude auth status
codex login status
```

### Auto-switch

Auto-switch is off by default and is set per provider. When it is on for a provider and the active account's own window reaches 100% (Claude: the 5-hour or 7-day window; Codex: its rate-limit windows), the app switches to another saved account of the same provider that still has room. It prefers accounts whose usage is known over accounts whose usage is unknown. It never crosses providers. There is a 60-second cooldown between auto-switch attempts per provider, including attempts that find no target.

The threshold is `auto_switch_threshold` in the config file (default 100).

### First launch

With no config file yet, the app imports the currently signed-in Claude and Codex accounts. If the Codex import fails (for example, unsupported credential storage), the app notifies you and continues.

## How it works

Claude Code stores its OAuth credentials in macOS Keychain under `Claude Code-credentials` and account metadata in `~/.claude.json`. Codex CLI stores its ChatGPT session in `~/.codex/auth.json` when `cli_auth_credentials_store = "file"` is set. Claude Switcher keeps one Keychain backup per saved account and copies the selected backup into the CLI's live slot on switch.

```text
macOS Keychain
├── Claude Code-credentials       active Claude token
├── claude-switcher:{email}       saved Claude accounts
└── codex-switcher:{email}        saved Codex sessions

~/.claude.json
└── oauthAccount                  swapped on Claude switch

~/.codex/auth.json
└── tokens                        swapped on Codex switch

~/.config/claude-switcher/accounts.json
└── provider, email, plan, active state, settings
```

## Codex note

Only Codex file-mode credentials are supported. If `cli_auth_credentials_store` is `keyring`, the app reports:

> Codex keyring credential storage is not supported yet. Set cli_auth_credentials_store = "file" in ~/.codex/config.toml and run codex login.

To switch to file mode:

```toml
# ~/.codex/config.toml
cli_auth_credentials_store = "file"
```

Then run:

```bash
codex login
```

If a saved Codex session has expired because its refresh token was already rotated, the app does not restore it. The row shows `Login required`. Click the row (or Add) to sign that account in again.

## Security

- Saved credentials live in macOS Keychain. The config file stores metadata only. It is written atomically with `0600` permissions in a `0700` directory. A corrupt config is backed up rather than overwritten.
- Writes to Keychain pass the secret to `security add-generic-password` as a hex string via `-X`. The plaintext is never on the command line, but the hex is briefly visible in `ps`, so this reduces process-argument exposure rather than eliminating it. The alternative, piping the secret to the tool's prompt, silently truncates at 128 characters and corrupted real credentials, which is why it is not used.
- Emails are validated before being used in Keychain service names.
- Subprocess calls never use `shell=True`.
- Keychain operations time out after 5 seconds. Usage checks run in background threads with timeouts, so the menu never hangs.
- The app never backs up a live credential under an account name it cannot confirm it belongs to.

## Requirements

- macOS (upstream states 12 or later)
- Claude Code CLI, for Claude switching
- Codex CLI with file-mode credentials, for Codex switching
- Python 3.10 or later, to build from source (built and tested here with 3.13)

## Build from source

```bash
git clone https://github.com/omar-elamin/claude-account-switcher.git
cd claude-account-switcher
./build_local.sh
# Output: dist/Claude Switcher.app
```

`build_local.sh` creates a `.venv`, installs the package in editable mode, runs py2app, then copies the `@rpath` dylibs that py2app skips (libffi, libssl, libcrypto and their dependencies) into the bundle and re-signs it ad hoc. Without that step the app either fails to launch (libffi) or cannot make HTTPS calls and shows "Usage unavailable". `build_app.sh` runs the same py2app step on its own. `build_local.sh` does not call it; it repeats that step and adds the editable install and the dylib copying around it.

To run from source instead of building the app:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
claude-switcher
```

Run the tests:

```bash
pytest tests/ -q
```

There are 261 tests. The tests that drive the real macOS `security` tool use a temporary keychain and skip where one cannot be created. They never touch the real Claude Code entry.

The app is not notarized. On first launch macOS may block it. Open **System Settings → Privacy & Security** and click **Open Anyway**.

## License

MIT, as declared in `pyproject.toml`. The repository does not yet include a LICENSE file.
