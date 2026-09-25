"""Claude routing choices through persisted settings and the application boundary."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from tests.test_app import app_module, _reset_app, ResetMenuItem
from claude_switcher import config
from claude_switcher.usage import claude_usage_state
from claude_switcher.usage_state import UsageState, UsageWindow


def usage(session=0, weekly=80, fable=100, weekly_reset='2030-01-03T00:00:00Z', fable_reset='2030-01-02T00:00:00Z'):
    return claude_usage_state({'five_hour': {'utilization': session},
        'seven_day': {'utilization': weekly, 'resets_at': weekly_reset},
        'limits': [{'kind': 'weekly_scoped', 'percent': fable, 'resets_at': fable_reset,
                    'scope': {'model': {'display_name': 'Fable'}}}]})


def setup_app(module, tmp_path, basis, states, proactive=False):
    app = _reset_app(module, tmp_path)
    accounts = [config.AccountInfo(email, 'max', '', i == 0, email) for i, email in enumerate(states)]
    config.save_accounts(accounts, app.config_path)
    # Write the public persisted contract, avoiding dependence on a new helper.
    raw = json.loads(app.config_path.read_text())
    raw['settings'].update(claude_route_based_on=basis, auto_switch={'claude': True}, proactive_switch=proactive)
    app.config_path.write_text(json.dumps(raw))
    app._usage_state_cache = {('claude', email): state for email, state in states.items()}
    app._has_credentials = lambda account: True
    return app


@pytest.mark.parametrize('basis,expected', [('weekly', 'switched'), ('fable', 'no_target')])
def test_exhausted_session_can_route_to_full_fable_account(app_module, tmp_path, monkeypatch, basis, expected):
    app = setup_app(app_module, tmp_path, basis, {'active': usage(session=100, fable=0), 'spare': usage()})
    switch = Mock()
    monkeypatch.setitem(app_module.PROVIDERS['claude'], 'switch', switch)
    result = app._attempt_auto_switch('claude')
    assert result['status'] == expected
    if expected == 'switched':
        switch.assert_called_once_with('spare', app.config_path)
    else:
        switch.assert_not_called()
    assert app._usage_state_cache[('claude', 'spare')].is_exhausted()


@pytest.mark.parametrize('basis', ['fable', 'weekly'])
@pytest.mark.parametrize('session,weekly', [(100, 10), (10, 100)])
def test_both_modes_respect_account_limits(app_module, tmp_path, basis, session, weekly):
    app = setup_app(app_module, tmp_path, basis, {'active': usage(session=100), 'spare': usage(session, weekly, 0)})
    assert app._attempt_auto_switch('claude')['status'] == 'no_target'


def test_weekly_does_not_switch_healthy_account_for_fable_limit(app_module, tmp_path):
    app = setup_app(app_module, tmp_path, 'weekly', {'active': usage(), 'spare': usage(fable=0)})
    assert app._attempt_auto_switch('claude') is None


@pytest.mark.parametrize('basis,target', [('fable', 'fable-first'), ('weekly', 'weekly-first')])
def test_proactive_ranking_uses_selected_window(app_module, tmp_path, monkeypatch, basis, target):
    app = setup_app(app_module, tmp_path, basis, {
        'active': usage(weekly=50, fable=50, weekly_reset='2030-01-10T00:00:00Z', fable_reset='2030-01-10T00:00:00Z'),
        'fable-first': usage(weekly=50, fable=50),
        'weekly-first': usage(weekly=50, fable=50, weekly_reset='2030-01-01T00:00:00Z', fable_reset='2030-01-09T00:00:00Z'),
    }, proactive=True)
    monkeypatch.setitem(app_module.PROVIDERS['claude'], 'switch', Mock())
    assert app._attempt_auto_switch('claude')['email'] == target


def test_weekly_blocks_auto_reset_when_only_fable_full(app_module, tmp_path):
    app = setup_app(app_module, tmp_path, 'weekly', {'active': usage()})
    config.set_auto_reset_enabled('claude', True, app.config_path)
    app._reset_cache = {('claude', 'active'): app_module.reset_service.ResetStatus(1, object())}
    app._consume_reset = Mock()
    assert app._attempt_auto_reset('claude') is None
    assert app._auto_reset_authorized('claude', 'active') is False
    app._consume_reset.assert_not_called()


def test_weekly_spare_blocks_auto_reset(app_module, tmp_path):
    app = setup_app(app_module, tmp_path, 'weekly', {'active': usage(session=100), 'spare': usage()})
    config.set_auto_reset_enabled('claude', True, app.config_path)
    app._reset_cache = {('claude', 'active'): app_module.reset_service.ResetStatus(1, object())}
    app._consume_reset = Mock()
    assert app._attempt_auto_reset('claude') is None
    assert not app._auto_reset_authorized('claude', 'active')
    app._consume_reset.assert_not_called()


def test_fresh_target_usage_uses_weekly_policy(app_module, tmp_path, monkeypatch):
    app = setup_app(app_module, tmp_path, 'weekly', {'active': usage(session=100)})
    config.set_auto_reset_enabled('claude', True, app.config_path)
    app._fetch_usage_state = lambda *args: usage()
    adapter = app_module.PROVIDERS['claude']['reset']
    prepare = Mock(return_value=app_module.reset_service.ResetStatus(1, object()))
    redeem = Mock(return_value='reset')
    monkeypatch.setattr(adapter, 'prepare', prepare)
    monkeypatch.setattr(adapter, 'redeem', redeem)
    assert app._consume_reset('claude', 'active', 1, 'active')['code'] == 'unavailable'
    prepare.assert_not_called()
    redeem.assert_not_called()


def test_mode_change_during_reset_read_cancels_pending_redemption(app_module, tmp_path, monkeypatch):
    app = setup_app(app_module, tmp_path, 'fable', {'active': usage(session=100), 'target': usage()})
    config.set_auto_reset_enabled('claude', True, app.config_path)
    app._fetch_usage_state = lambda *args: usage()
    adapter = app_module.PROVIDERS['claude']['reset']
    def prepare(*args):
        raw = json.loads(app.config_path.read_text())
        raw['settings']['claude_route_based_on'] = 'weekly'
        app.config_path.write_text(json.dumps(raw))
        return app_module.reset_service.ResetStatus(1, object())
    redeem = Mock(return_value='reset')
    monkeypatch.setattr(adapter, 'prepare', prepare)
    monkeypatch.setattr(adapter, 'redeem', redeem)
    assert app._consume_reset('claude', 'target', 1, 'active')['code'] == 'unavailable'
    redeem.assert_not_called()


def test_menu_selection_persists_without_enabling_resets(app_module, tmp_path, monkeypatch):
    app = _reset_app(app_module, tmp_path)
    monkeypatch.setattr(app_module.rumps, 'MenuItem', ResetMenuItem)
    app._add_auto_switch_menu()
    menu = next(x for x in app.menu.add.call_args.args[0].children if getattr(x, 'title', '') == 'Route based on')
    choices = [x for x in menu.children if x.callback]
    assert [(x.title, x.state) for x in choices] == [('Fable usage', 1), ('Weekly usage', 0)]
    choices[1].callback(choices[1])
    settings = config.load_settings(app.config_path)
    assert settings.claude_route_based_on == 'weekly'
    assert settings.auto_reset == {'claude': False, 'codex': False}
    app._rebuild_menu.assert_called_once()
    app._fetch_all_usage.assert_called_once()
    app._add_auto_switch_menu()
    menu = next(x for x in app.menu.add.call_args.args[0].children if getattr(x, 'title', '') == 'Route based on')
    assert [(x.title, x.state) for x in menu.children if x.callback] == [('Fable usage', 0), ('Weekly usage', 1)]


@pytest.mark.parametrize('value', [None, [], {}, 'invalid', 1])
def test_invalid_saved_basis_preserves_legacy_default(tmp_path, value):
    path = tmp_path / 'accounts.json'
    path.write_text(json.dumps({'settings': {'claude_route_based_on': value}}))
    assert config.load_settings(path).claude_route_based_on == 'fable'


def test_setting_write_overlaps_account_and_toggle_without_lost_updates(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    path = tmp_path / 'accounts.json'
    setter = config.set_claude_route_based_on
    entered, release, attempted = threading.Event(), threading.Event(), threading.Event()
    original = config._write_config_data
    def write(data, target):
        if threading.current_thread().name.startswith('routing'):
            entered.set()
            assert release.wait(5)
        original(data, target)
    monkeypatch.setattr(config, '_write_config_data', write)
    def other_writes():
        attempted.set()
        config.add_account(config.AccountInfo('new', 'max', '', True, 'new'), path)
        config.set_auto_switch_enabled('codex', True, path)
    with ThreadPoolExecutor(1, thread_name_prefix='routing') as first, ThreadPoolExecutor(1) as second:
        f = first.submit(setter, 'weekly', path)
        try:
            assert entered.wait(5)
            g = second.submit(other_writes)
            assert attempted.wait(5)
        finally:
            release.set()
        f.result(timeout=5)
        g.result(timeout=5)
    assert config.load_settings(path).claude_route_based_on == 'weekly'
    assert config.load_settings(path).auto_switch['codex']
    assert [a.email for a in config.load_accounts(path)] == ['new']
