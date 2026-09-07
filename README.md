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
- Show, spend, and optionally auto-spend the banked Codex rate-limit resets an account holds
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
- Under each header, one row per saved account, shown as `email (plan)`, with the active account marked and a usage line underneath. Click a row to switch to that account. A Codex row whose saved session has expired shows `Login required`; clicking it opens the Codex login instead of switching. Rows show `•••` until the first fetch finishes, `Checking…` on rows that came back unavailable while a quick retry is pending, and `Usage unavailable` when retries are exhausted.
- `Auto-switch` submenu with one item per provider, labelled `Claude Code` and `Codex CLI`, with a checkmark when enabled. Clicking one toggles it, and a notification says "Enabled" or "Disabled".
- `Auto-reset` submenu with one item, `Codex CLI`, with a checkmark when enabled. Clicking it toggles the setting, and a notification says "Enabled" or "Disabled".
- `✚ Add Claude account...` and `✚ Add Codex account...`
- `↻ Refresh usage`
- `− Remove account` submenu. It lists every saved account as `[Claude] email` or `[Codex] email`, including the active one. Choosing the active account shows an alert instead of removing it: "You cannot remove the active Claude Code account. Switch first." (or "… active Codex CLI account …").
- `↺ Reset Codex usage` submenu. It lists the Codex accounts that can apply a reset right now, as `email (N available)`. If none can, it shows one disabled item: `No reset applicable now`.
- `⏻ Quit`

### Usage display

A Claude row looks like this:

```text
5h 40% (2h 1m) | 7d 20% (1d 5h) | Fable 32% (1d 5h)
```

The first segment is the 5-hour window. The second is the 7-day window. After that comes one segment for each model-scoped weekly limit the API reports, labelled with the model's own name (today: `Fable`). The value in parentheses is the time until that window resets. Model-scoped windows are informational only and never trigger auto-switch.

A Codex row looks like this:

```text
7d 100% (3h 24m) · 2 resets
```

There is one segment per rate-limit window the API reports (primary, then secondary), labelled by the window's real length as reported by the API (for example `5h` or `7d`). On the plans seen so far, the primary window is a 7-day window. When an account holds banked resets, the row ends with `· N resets` (`· 1 reset` for one). See [Rate-limit resets](#rate-limit-resets).

Claude usage comes from `https://api.anthropic.com/oauth/usage`, called with each saved account's own token, so every saved account shows its own usage, including inactive ones. Claude access tokens last about 8 hours. Claude Code refreshes the live one. The app refreshes an inactive account's saved token itself when its usage call comes back expired, so inactive rows keep showing real usage. The row reads `Token expired (switch to refresh)` only when the app must not refresh: while an add is in progress, or when the saved token is the same pair the live session holds, or for up to 5 minutes after a refresh attempt failed (rotating it would log Claude Code out). Switching to that account then refreshes it. A `Login required` row means the refresh was rejected, so the saved session was revoked and needs a new sign-in. If the CLI's live session belongs to a different account than the one marked active (for example after a sign-in done outside the app), the active row shows that account's own saved-session usage rather than the live token's, and the log notes the drift. Click the account to re-sync the live session. Codex usage comes from the chatgpt.com backend usage endpoint. A saved Codex token that needs refreshing is refreshed, and the refreshed token is written back to that account's Keychain backup.

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

### Rate-limit resets

A banked reset is a one-time Codex usage reset that OpenAI grants to a ChatGPT account (Go, Plus, Pro, and Business plans). It is stored on the account and expires 30 days after it is granted. Using one resets both the 5-hour and the weekly window of that account.

Only an account that is currently at a limit can apply a reset. The app reads this from the usage API, which reports both how many resets the account holds and how many it can apply now.

To use one by hand, open `↺ Reset Codex usage` and click the account. A dialog asks: "Use 1 of N banked resets for {email}? This resets that account's Codex 5-hour and weekly windows and cannot be undone." with **Reset** and **Cancel**. After you confirm, a notification reports the result: "Reset applied", "Nothing to reset", "No reset credit available", "Already redeemed", or the error. Usage then refreshes.

Auto-reset is off by default and exists for Codex only. When it is on, after each usage refresh the app checks whether the active Codex account is at its limit and no other saved Codex account has room. Only then does it spend one reset: on the active account if it can apply one, otherwise on another exhausted account that can. It works whether or not Auto-switch is on. Two guards apply: at least 60 seconds between attempts, and never the same account twice within an hour, so a reset that did not take effect cannot burn a second one. Auto-reset never switches accounts by itself. If Auto-switch is on, the next refresh can move to the account that now has room.

The setting is stored as `auto_reset` in the config file, next to `auto_switch`.

Claude Code has no reset feature. Nothing changes for Claude accounts.

### First launch

With no config file yet, the app imports the currently signed-in Claude and Codex accounts. If the Codex import fails (for example, unsupported credential storage), the app notifies you and continues.

## How it works

Claude Code stores its OAuth credentials in macOS Keychain under `Claude Code-credentials` and account metadata in `~/.claude.json`. Codex CLI stores its ChatGPT session in `~/.codex/auth.json` when `cli_auth_credentials_store = "file"` is set. Claude Switcher keeps one Keychain backup per saved account and copies the selected backup into the CLI's live slot on switch.

A Codex reset is sent to `https://chatgpt.com/backend-api/wham/rate-limit-reset-credits/consume`, the same endpoint the Codex CLI uses, with the same headers as the usage call and an idempotency key (a UUID). The app never sends it for an account that cannot apply a reset, and on a network timeout it retries once with the same key, so a reset is never applied twice.

A saved Claude token is refreshed with `https://platform.claude.com/v1/oauth/token`, the same endpoint and client id Claude Code uses. The app refreshes only its own backups (`claude-switcher:{email}`), never the live `Claude Code-credentials` entry. Before refreshing, it checks that the backup is not the same token pair as the live session, re-reads the backup to make sure nothing changed it meanwhile, and tries at most once per account every 5 minutes. The new tokens are written to the backup before they are used. Saved Codex tokens were already refreshed the same way.

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
- Token refreshes never write Claude Code's live credential entry. They touch only the app's own backups, and a backup that shares its token pair with the live session is never refreshed.

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

There are 405 tests. The tests that drive the real macOS `security` tool use a temporary keychain and skip where one cannot be created. They never touch the real Claude Code entry.

The app is not notarized. On first launch macOS may block it. Open **System Settings → Privacy & Security** and click **Open Anyway**.

## License

MIT, as declared in `pyproject.toml`. The repository does not yet include a LICENSE file.
