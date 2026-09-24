"""Native Cocoa menu construction, isolated from login, timers, and live APIs."""
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.skipif(sys.platform != 'darwin', reason='native macOS menu')
def test_native_menu_contains_enabled_claude_account_action(tmp_path):
    source = r'''
import sys
from pathlib import Path
import rumps
from claude_switcher.app import ClaudeSwitcherApp
from claude_switcher import claude_reset, reset_service, keychain, login_item
from claude_switcher.config import AccountInfo, save_accounts

def forbidden(*args, **kwargs):
    raise AssertionError('Native construction must not access credentials or reset HTTP')
claude_reset._open = forbidden
keychain.read_credentials = forbidden
login_item.is_enabled = lambda: False
app = object.__new__(ClaudeSwitcherApp)
app._menu = rumps.rumps.Menu()
app.config_path = Path(sys.argv[1])
app._usage_state_cache = {}
app._usage_cache = {}
app._has_credentials = lambda account: False
accounts = [AccountInfo('native@example.test', 'max', '', False, 'test', provider=p)
            for p in ('claude', 'codex')]
save_accounts(accounts, app.config_path)
app._rebuild_menu()
for provider, label in [('claude', 'Claude'), ('codex', 'Codex')]:
    menu = app.menu[f'↺ Reset {label} usage']
    assert menu._menu.numberOfItems() == 1
    item = next(iter(menu.values()))
    assert item._email == 'native@example.test' and item._provider == provider
    assert item.callback is None
    assert item._menuitem.title() == 'native@example.test (checking resets…)'
    app._reset_cache = {(provider, item._email): reset_service.ResetStatus(2, object())}
    app._update_reset_labels()
    assert item.callback == app._on_reset_usage
    assert item._menuitem.action() is not None
    assert item._menuitem.title() == 'native@example.test (2 resets left, available)'
    assert item._menuitem.isEnabled()
    app._reset_cache = {(provider, item._email): reset_service.ResetStatus(0)}
    app._update_reset_labels()
    assert item.callback is None
    assert item._menuitem.title() == 'native@example.test (0 resets left)'
assert 'Claude Code' not in app.menu['Auto-reset']
print('Native NSMenu and callback PASS; no credentials or HTTP accessed')
'''
    env = os.environ.copy()
    # Preserve the package path selected by the parent, including bundle checks.
    import claude_switcher
    env['PYTHONPATH'] = str(Path(claude_switcher.__file__).parent.parent)
    result = subprocess.run([sys.executable, '-c', source, str(tmp_path / 'accounts.json')],
                            capture_output=True, text=True, timeout=30, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'Native NSMenu and callback PASS' in result.stdout
