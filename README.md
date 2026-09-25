# Claude Switcher

A macOS menu-bar app for saving Claude Code and Codex CLI accounts, checking their usage, and switching between them. It also shows available usage resets and can spend them with your confirmation or through optional automatic resets. Version 0.4.3.

Claude and Codex have separate accounts and settings. Switching one does not switch the other. The same email can be saved once for each provider. The app has no Dock icon or Cmd-Tab entry.

This is a fork of [Symbioose/claude-account-switcher](https://github.com/Symbioose/claude-account-switcher). Upstream's Homebrew cask and release downloads do not include this fork's changes. Build this fork from source using the steps below.

![Claude Switcher menu from an earlier version](screenshot.png)

This screenshot predates the Claude reset menu and the automatic reset options for both providers. See [The menu](#the-menu) for the current controls.

## Features

- Save and switch Claude Code and Codex CLI accounts without signing in each time.
- Check usage for active and saved accounts.
- Enable automatic account switching separately for each provider.
- View reset balances for each account, confirm a manual reset, or opt into automatic resets.
- Import existing Claude and Codex sign-ins on first launch.
- Keep saved credentials in macOS Keychain.

## Install

### Requirements

- macOS. Upstream lists macOS 12 or later.
- Claude Code CLI for Claude accounts.
- Codex CLI with file-mode credentials for Codex accounts. See [Codex credential storage](#codex-credential-storage).
- Python 3.10 or later to build from source. This fork has been built and tested with Python 3.13.

Build and install this fork:

```bash
git clone https://github.com/omar-elamin/claude-account-switcher.git
cd claude-account-switcher
./build_local.sh --install
```

Open `/Applications/Claude Switcher.app`. Its icon appears in the menu bar.

To build without installing, run `./build_local.sh`. The output is `dist/Claude Switcher.app`, which you can drag to `/Applications`.

The app is not notarized. If macOS blocks the first launch, open **System Settings → Privacy & Security** and click **Open Anyway**.

## Usage

### First launch

With no config file yet, the app imports the currently signed-in Claude and Codex accounts. A failed Codex import produces a notification and does not stop startup.

### The menu

Each provider has a section, `── Claude Code ──` or `── Codex CLI ──`, with one row per saved account. Rows show `email (plan)`, mark the active account, and show usage underneath. Click an account to switch to it. A Codex row marked `Login required` opens sign-in instead.

The remaining controls are:

| Control | What it does |
| --- | --- |
| `Auto-switch` | Separate `Claude Code` and `Codex CLI` toggles. Both start off. `Use expiring quota first` allows early switching when auto-switch is enabled. |
| `Auto-switch → Route based on` | Under the `Claude Code` heading, choose `Fable usage` or `Weekly usage`. One choice is active at a time; the heading itself is not selectable. |
| `Auto-switch → Codex gateway (switch running sessions)` | Lets running Codex sessions follow account switches through a local gateway. |
| `Auto-reset` | Separate `Claude Code` and `Codex CLI` toggles. Both start off. Enabling one allows resets without a confirmation dialog when the automatic reset conditions are met. |
| `✚ Add Claude account...` / `✚ Add Codex account...` | Starts a sign-in for that provider. |
| `↻ Refresh usage` | Refreshes usage and reset availability. |
| `− Remove account` | Removes a saved account. Switch away from an active account before removing it. |
| `↺ Reset Claude usage` / `↺ Reset Codex usage` | Shows every saved account for that provider and its reset balance. A submenu appears when that provider has saved accounts. |
| `Start at login` | Creates or removes a per-user launch agent. Install the app in `/Applications` first so the launch path stays stable. |
| `⏻ Quit` | Closes the app. |

Enabled toggles have a checkmark. Changing an automatic switch or reset setting also produces an Enabled or Disabled notification.

### Usage display

A Claude usage row can look like this:

```text
5h 40% (2h 1m) | 7d 20% (1d 5h) | Fable 32% (1d 5h)
```

The percentages show usage consumed. Parentheses show the time until each window resets. Model-specific weekly windows use the names returned by the provider. The display always shows all reported windows. Which windows guide automatic switching and resets depends on the Claude routing choice described below.

A Codex row can look like this:

```text
7d 100% (3h 24m) · 2 resets
```

Codex window labels use the lengths returned by the provider, such as `5h` or `7d`. When the account has reset credits, the row also shows the count.

Usage refreshes at launch, every five minutes, after adding or switching accounts, and when you select `↻ Refresh usage`. Unavailable rows get up to three quick retries, six seconds apart. Rows show `•••` before the first result, `Checking…` during a quick retry, and `Usage unavailable` if the retries do not recover the result.

macOS closes the menu when you click an item. Refreshing produces notifications; reopen the menu to see the updated rows.

### Adding an account

Choose the provider's Add option:

- Claude runs `claude auth login` and opens browser sign-in.
- Codex opens Terminal and runs `codex login -c 'cli_auth_credentials_store="file"'`.

Before sign-in, the app saves the outgoing session and clears the local credential slot. It avoids either CLI's logout command because logout could revoke the session being saved.

For Claude, a backup requires a nonempty access token and a matching email in `~/.claude.json`. Cleared-login markers and malformed credentials do not replace an existing backup. Codex uses the email in `~/.codex/auth.json` and imports it if needed.

Sign-in times out after five minutes. On cancellation or failure, the app restores the previous credential snapshot if one was saved. Clicking Add again cancels the pending sign-in and starts a fresh one. Switching or removing accounts during sign-in reports the provider as busy.

### Switching accounts

For Claude, the app saves the outgoing credentials when the local account identity matches, copies the selected backup into `Claude Code-credentials`, and updates `oauthAccount` in `~/.claude.json`. For Codex, it saves the outgoing session and writes the selected session to `~/.codex/auth.json` with `0600` permissions.

A cleared Claude login does not replace an existing saved credential backup. Switching rejects a saved credential without a nonempty access token. These are checks of the stored credential structure; they do not prove the provider will accept the token. Clicking the account already marked active does nothing. Use Add to sign in again if that account needs recovery.

To check which account each CLI is using:

```bash
claude auth status
codex login status
```

### Expired or changed sign-ins

The app reads each saved account's usage with that account's credentials. It can refresh saved Claude tokens, but leaves Claude Code's live credential entry to the CLI. It skips refreshes while a Claude sign-in is in progress or when the saved and live sessions share a token pair, and limits attempts to once per account every five minutes.

`Token expired (switch to refresh)` means the saved Claude token needs attention from the CLI. Switch to the account so Claude Code can attempt a refresh. `Login required` means a new sign-in is needed; use `✚ Add Claude account...`. Expired access tokens can remain in backups so the CLI has a chance to refresh them.

If a Claude sign-in outside the app changes the live account, the app avoids saving that session over a different account's backup. If the intended account is already marked active, use Add to sign in again. When an external sign-in matches the active account, the app can update its backup after rechecking the local credentials and identity.

Codex tokens are refreshed when needed and written back to the account's Keychain backup. A saved Codex session with a revoked or rotated refresh token shows `Login required`. Click it or use Add to sign in again.

### Auto-switch

Auto-switch is off by default for each provider. When enabled, it checks usage after each refresh and chooses among accounts with saved credentials and known room below the usage limit. The default limit threshold is 100%.

For Claude, open `Auto-switch → Route based on` and choose:

- **Fable usage:** the default, including for existing installs. All reported windows count toward an account's limit. The target window is `Fable`, with a fallback to `7d`.
- **Weekly usage:** use the overall `7d` window as the target. Model-specific windows are excluded from checks of available quota and account ranking.

Both choices respect the overall `5h` and `7d` limits. Codex uses its first reported window as the target. The Claude choice is saved as `claude_route_based_on` in the config, with a value of `fable` or `weekly`.

With `Use expiring quota first` on, the app may switch before the active account is exhausted. It prefers the account whose target window resets soonest. Reset times within an hour count as a tie. The current account wins a tie; otherwise the account with the most quota left in that window wins.

When no usable account's target window expires within the next day, the app can select an unused account whose reset clock appears not to have started. With `Use expiring quota first` off, it waits until the active account reaches its limit.

Automatic switching stays within one provider, waits at least 60 seconds between attempts, and respects an account you selected manually until it reaches its limit. If the active account is exhausted and no account has known room, it can try a saved account whose usage is unknown.

### Usage resets

Both providers have a reset submenu. Each lists every saved account, including accounts that cannot reset right now:

| Row text after the email | Meaning |
| --- | --- |
| `checking resets…` | The app is waiting for a balance check. |
| `could not check` | The balance is unknown. |
| `0 resets left` | The provider reported no remaining resets. |
| `N resets left, unavailable now` | Resets remain, but none can be used now. |
| `N resets left, available` | A reset is currently available. |

Only available rows can be selected. An unknown balance is kept distinct from zero. Reset availability comes from the provider; having a balance alone does not make an account eligible.

#### Manual resets

Open `↺ Reset Claude usage` or `↺ Reset Codex usage`, then select an available account. The app checks again before showing **Reset usage?** The dialog names the provider, account, affected limits, and reset balance. It also shows the offer's end date when supplied and explains that spending a reset cannot be undone.

Choose **Cancel** to leave without sending a reset request. Choose **Reset** to spend one reset. Before sending the request, the app checks the account identity, current eligibility, and the details used for confirmation. Changed details stop the request and require another check.

A notification reports the result, then usage and reset balances refresh. If the result is uncertain, the app says: “We could not confirm what happened. Check your usage before you try again.” A lost response can follow a successful reset, so check usage and the remaining balance before starting another attempt.

#### Automatic resets

`Auto-reset → Claude Code` and `Auto-reset → Codex CLI` are independent and off by default. Enabling either permits automatic credit use for that provider without a confirmation dialog.

After a usage refresh, an enabled provider can reset only when its active account is exhausted and there is no other account to switch to. A saved account with unknown usage also counts as a possible switching fallback and blocks automatic resets. This check applies even when Auto-switch is off.

Claude uses the selected `Route based on` policy to check exhaustion, compare switching alternatives, and recheck the target before a reset. In `Weekly usage` mode, a model-specific limit alone does not trigger an automatic reset. Choosing a routing mode does not enable Auto-reset. Its separate toggle must be on.

The app prefers an available reset on the active account, then on another exhausted account of the same provider. It reads the target's usage and eligibility again, then rechecks the triggering active account, setting, and switching alternatives before sending the reset request. An account switch or opt-out during those checks stops the pending automatic reset.

Manual and automatic requests share a guard so the same provider/account cannot reset concurrently. Automatic attempts have a one-minute cooldown per provider and a one-hour cooldown per account, including uncertain outcomes. These cooldowns last for the running app session.

Automatic resets do not switch accounts themselves. If Auto-switch is enabled, a later refresh can move to an account whose usage was reset. Each automatic attempt reports its result and refreshes usage and balances. Settings are stored per provider under `auto_reset` in the config file.

### Codex gateway

Turn on `Auto-switch → Codex gateway (switch running sessions)` to let running Codex sessions follow account switches. Codex normally keeps its token in memory, so replacing its auth file does not change an open session. The gateway sends requests with the active account's token.

It listens on `127.0.0.1`, port `8790` by default, over HTTPS. The app adds this managed block at the top of `~/.codex/config.toml`:

```toml
# managed by Claude Switcher: Codex gateway
openai_base_url = "https://127.0.0.1:8790/backend-api/codex"
chatgpt_base_url = "https://127.0.0.1:8790/backend-api/"
```

A one-time backup is saved at `~/.codex/config.toml.claude-switcher.bak`. Turning the gateway off removes the managed block. Restart open Codex CLI and desktop sessions whenever you turn it on or off so they read the new settings. Keep Claude Switcher running while the gateway is enabled.

On first use, the app creates a local certificate authority (CA) and server certificate in `~/Library/Application Support/Claude Switcher/codex-gateway-tls`. macOS asks for permission to trust the CA in your login keychain. The CA is constrained to `127.0.0.1`; the app deletes its private key after signing the server certificate.

To remove that trust, open Keychain Access and delete the login-keychain certificate whose name starts with `Claude Switcher Local CA`. Or find its SHA-1 fingerprint and remove it in Terminal:

```sh
security find-certificate -a -c "Claude Switcher Local CA" -Z ~/Library/Keychains/login.keychain-db
security delete-certificate -Z SHA1_FINGERPRINT -t ~/Library/Keychains/login.keychain-db
```

With Codex auto-switch enabled, a usage-limit response lets the gateway switch to an available account and retry the request once. Other rate-limit responses pass through. If no account is available, it returns the original usage-limit response. The gateway also adds credentials to plugin calls that arrive without them.

The gateway supports HTTP/1.1 and rejects WebSocket upgrades so Codex can use streaming HTTP. **The local port has no client authentication: any local process can use it with the active account's credentials.**

## Codex credential storage

Only Codex file-mode credentials are supported. For keyring mode, the app asks you to change the setting and sign in again:

```toml
# ~/.codex/config.toml
cli_auth_credentials_store = "file"
```

Then run:

```bash
codex login
```

## How it works

The app stores one Keychain backup per provider and email. Switching copies the selected backup into the CLI's live credential location.

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

Claude usage is read from `https://api.anthropic.com/oauth/usage`. Saved Claude tokens are refreshed through `https://platform.claude.com/v1/oauth/token`. Refreshes recheck the saved credentials before writing and write new tokens only to the app's backup. Codex uses the ChatGPT backend for usage and reset requests.

Both providers share the reset menus, confirmation flow, and automatic reset policy. Provider adapters keep their own authentication and reset contracts. Claude checks the grant, organization, credentials, and confirmed details, and does not automatically retry a reset POST. Codex binds confirmation to the account ID and balance, and retries selected network failures once using the same request ID. Neither path treats an uncertain response as proof that no credit was spent.

See [Usage reset architecture](docs/reset-architecture.md) for the contracts and test coverage.

## Security

- Saved credentials live in macOS Keychain. The config holds account metadata and settings, with `0600` file permissions in a `0700` directory. Config writes are atomic; corrupt files are backed up.
- Keychain writes pass credentials to `security add-generic-password` as hex through `-X`. **Hex is reversible and briefly visible in process arguments.** The tool's interactive prompt is avoided because it truncates long credentials.
- Emails are validated before use in Keychain service names. Subprocess calls do not use `shell=True`.
- Keychain commands have five-second timeouts. Usage checks run in background threads with network timeouts.
- Claude backup and switch paths check local account identity and credential structure to avoid replacing saved sessions with cleared-login markers or another account's credentials.
- Saved Claude token refreshes write only the app's backups and skip backups that share a token pair with the live session.
- Enabling the Codex gateway grants local processes access to the active account through its loopback port. Review [Codex gateway](#codex-gateway) before enabling it.

## Development

`build_local.sh` creates a `.venv`, installs the package in editable mode, runs py2app, copies required `@rpath` libraries into the bundle, and signs the result ad hoc. The library-copy step includes libffi, libssl, libcrypto, and their dependencies. `build_app.sh` runs only the py2app step and omits that packaging work.

To run from source:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
claude-switcher
```

Run tests:

```bash
pytest tests/ -q
```

Tests cover account and credential handling, usage, reset confirmation and automatic reset rules, native Cocoa menus, and the gateway. Reset tests use synthetic provider responses and do not need live reset credits. Gateway transport tests use a fake upstream on loopback and skip where local socket binding is blocked. Tests that call the real macOS `security` tool use a temporary keychain and skip where one cannot be created.

## License

MIT, as declared in `pyproject.toml`. The repository does not yet include a LICENSE file.
