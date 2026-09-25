"""Exercise real Cocoa menu actions and persistence in an isolated native process."""
import os
from pathlib import Path
import subprocess
import sys
import pytest

@pytest.mark.skipif(sys.platform != 'darwin', reason='native macOS menu')
def test_native_routing_menu_selection_survives_rebuild(tmp_path):
    source = r'''
import sys
from pathlib import Path
import rumps
from AppKit import NSApplication
from claude_switcher.app import ClaudeSwitcherApp
from claude_switcher import config, keychain, claude_reset

def forbidden(*args, **kwargs):
    raise AssertionError('No live credentials or reset requests allowed')
keychain.read_credentials = forbidden
claude_reset._open = forbidden
app = object.__new__(ClaudeSwitcherApp)
app._menu = rumps.rumps.Menu()
app.config_path = Path(sys.argv[1])
app._rebuild_menu = lambda: None
app._fetch_all_usage = lambda: None
native = NSApplication.sharedApplication()
app._add_auto_switch_menu()
menu = app.menu['Auto-switch']['Route based on']
assert list(menu) == ['Claude Code', 'Fable usage', 'Weekly usage']
assert menu['Claude Code'].callback is None
assert menu['Fable usage'].state == 1
item = menu['Weekly usage']._menuitem
assert item.isEnabled()
assert native.sendAction_to_from_(item.action(), item.target(), item)
assert config.load_settings(app.config_path).claude_route_based_on == 'weekly'
assert config.load_settings(app.config_path).auto_reset == {'claude': False, 'codex': False}
app._menu = rumps.rumps.Menu()
app._add_auto_switch_menu()
menu = app.menu['Auto-switch']['Route based on']
assert menu['Weekly usage'].state == 1 and menu['Fable usage'].state == 0
item = menu['Fable usage']._menuitem
assert native.sendAction_to_from_(item.action(), item.target(), item)
assert config.load_settings(app.config_path).claude_route_based_on == 'fable'
print('Native routing selection and persisted restart state PASS')
'''
    import claude_switcher
    env = dict(os.environ, PYTHONPATH=str(Path(claude_switcher.__file__).parent.parent))
    result = subprocess.run([sys.executable, '-c', source, str(tmp_path / 'accounts.json')],
                            capture_output=True, text=True, timeout=30, env=env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'persisted restart state PASS' in result.stdout
