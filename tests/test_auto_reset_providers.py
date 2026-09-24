"""Opt-in automatic resets through the shared app path; synthetic providers only."""
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from tests.test_app import app_module, _reset_app, ResetMenuItem
from tests.test_reset_architecture import two_providers
from claude_switcher import codex_core, codex_usage, reset_service
from claude_switcher.config import load_settings, save_settings, save_accounts, AccountInfo, AppSettings
from claude_switcher.usage_state import UsageState, UsageWindow


@pytest.fixture
def automatic(two_providers, monkeypatch):
    t=two_providers
    t.accounts=[replace(a,active=True) for a in t.accounts if a.email=='ready@test.com']
    save_accounts(t.accounts,t.app.config_path)
    monkeypatch.setattr(codex_core,'_read_codex_credentials_for_import_raw',
                        lambda: codex_usage.keychain.read_credentials('codex-switcher:ready@test.com'))
    monkeypatch.setattr(codex_usage,'fetch_active_codex_usage',
                        lambda path: codex_usage.fetch_codex_usage_for_account('ready@test.com',path))
    # Exercise real saved credential/HTTP/parser paths without touching personal
    # live login files. Active/live Codex binding has its own backend regressions.
    def fetch(account, active):
        entry=t.module.PROVIDERS[account.provider]
        return entry['usage_state'](entry['fetch_usage'](account.email,t.app.config_path))
    t.app._fetch_usage_state=fetch
    return t


def run_refresh(t):
    t.module.ClaudeSwitcherApp._fetch_all_usage(t.app)


def posts(t):
    return [(p,e) for p,e,r in t.requests if r.get_method()=='POST']


def test_both_options_exist_and_start_off(app_module,tmp_path):
    app=_reset_app(app_module,tmp_path)
    with patch.object(app_module.rumps,'MenuItem',ResetMenuItem):
        app._add_auto_reset_menu()
    rows=app.menu.add.call_args.args[0].children
    assert [(r.title,r._provider,r.state) for r in rows]==[
        ('Claude Code','claude',0),('Codex CLI','codex',0)]
    assert load_settings(app.config_path).auto_reset=={'claude':False,'codex':False}


@pytest.mark.parametrize('provider',['claude','codex'])
def test_toggle_then_periodic_refresh_redeems_only_opted_in_provider(automatic,provider):
    t=automatic
    run_refresh(t)
    assert posts(t)==[]
    t.app._on_toggle_auto_reset(SimpleNamespace(_provider=provider))
    assert load_settings(t.app.config_path).auto_reset[provider] is True
    assert posts(t)==[]
    run_refresh(t)
    assert posts(t)==[(provider,'ready@test.com')]
    assert t.module.rumps.alert.call_count==0
    # Repeated polls cannot burn another grant/credit inside the cooldown.
    run_refresh(t)
    assert posts(t)==[(provider,'ready@test.com')]
    t.app._on_toggle_auto_reset(SimpleNamespace(_provider=provider))
    assert load_settings(t.app.config_path).auto_reset[provider] is False


def test_both_providers_same_email_have_independent_cooldowns_and_notifications(automatic):
    t=automatic
    save_settings(AppSettings(auto_reset={'claude':True,'codex':True}),t.app.config_path)
    run_refresh(t)
    assert posts(t)==[('claude','ready@test.com'),('codex','ready@test.com')]
    notices=[c.kwargs for c in t.module.rumps.notification.call_args_list]
    assert len(notices)==2
    assert {n['subtitle'] for n in notices}=={'Auto-reset Claude Code','Auto-reset Codex CLI'}
    assert t.app._fetch_all_usage.call_count==1
    run_refresh(t)
    assert len(posts(t))==2


@pytest.mark.parametrize('provider',['claude','codex'])
@pytest.mark.parametrize('guard',['disabled','healthy','no_credit','pending','alternative','unknown'])
def test_common_auto_guards_never_post(automatic,provider,guard):
    t=automatic
    save_settings(AppSettings(auto_reset={provider:guard!='disabled'}),t.app.config_path)
    if guard=='no_credit': t.balances['ready@test.com']=0
    if guard=='pending': t.app._reset_in_progress={(provider,'ready@test.com')}
    if guard=='alternative':
        t.accounts.append(AccountInfo('other@test.com','pro','',False,'other',provider=provider))
        save_accounts(t.accounts,t.app.config_path)
    real=t.app._fetch_usage_state
    def fetch(account,active):
        if account.email=='other@test.com': return UsageState(True,'10%',(UsageWindow('5h',10),))
        if account.provider==provider and guard=='unknown': return UsageState(False,'unknown')
        state=real(account,active)
        return replace(state,windows=(UsageWindow('5h',10),)) if account.provider==provider and guard=='healthy' else state
    t.app._fetch_usage_state=fetch
    run_refresh(t)
    assert posts(t)==[]


@pytest.mark.parametrize('provider',['claude','codex'])
def test_auto_rechecks_current_usage_before_spending(automatic,provider):
    t=automatic
    save_settings(AppSettings(auto_reset={provider:True}),t.app.config_path)
    real=t.app._fetch_usage_state
    calls=0
    def fetch(account,active):
        nonlocal calls
        state=real(account,active)
        if account.provider==provider:
            calls+=1
            if calls>1: return replace(state,windows=(UsageWindow('5h',0),))
        return state
    t.app._fetch_usage_state=fetch
    run_refresh(t)
    assert calls>=2 and posts(t)==[]


@pytest.mark.parametrize('provider',['claude','codex'])
def test_overlapping_auto_and_manual_reset_share_one_operation_guard(app_module,tmp_path,provider):
    app=_reset_app(app_module,tmp_path)
    email='same@test.com'
    account=AccountInfo(email,'pro','',True,email,provider=provider)
    save_accounts([account],app.config_path)
    save_settings(AppSettings(auto_reset={provider:True}),app.config_path)
    app._usage_state_cache={(provider,email):UsageState(True,'100%',(UsageWindow('5h',100),),1,1,True)}
    app._reset_cache={(provider,email):reset_service.ResetStatus(1,object())}
    entered,release=threading.Event(),threading.Event()
    def consume(*args):
        entered.set()
        assert release.wait(5)
        return {'provider':provider,'email':email,'credits':1,'code':'reset'}
    adapter=app_module.PROVIDERS[provider]['reset']
    with patch.object(app,'_consume_reset',side_effect=consume) as consume_mock, \
         patch.object(adapter,'prepare') as prepare:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(app._attempt_auto_reset,provider)
            try:
                assert entered.wait(5)
                app._on_reset_usage(SimpleNamespace(_provider=provider,_email=email))
                assert app._attempt_auto_reset(provider) is None
            finally:
                release.set()
            assert future.result(timeout=5)['code']=='reset'
        consume_mock.assert_called_once()
        prepare.assert_not_called()
    assert not app._reset_in_progress
