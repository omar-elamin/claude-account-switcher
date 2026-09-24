"""Shared provider workflow, with real clients and synthetic HTTP responses."""
import io
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tests.test_app import app_module, _reset_app, ResetMenuItem, ImmediateThread
from tests.test_claude_reset import eligible, ORG
from claude_switcher import claude_reset, codex_usage, codex_core
from claude_switcher.config import AccountInfo, save_accounts


@pytest.fixture
def two_providers(app_module, tmp_path, monkeypatch):
    app = _reset_app(app_module, tmp_path)
    accounts = [AccountInfo(email, 'pro', '', False, email,
        {'emailAddress':email, 'organizationUuid':ORG} if provider=='claude' else None,
        provider=provider) for provider in ('claude','codex')
        for email in ('ready@test.com','empty@test.com','unknown@test.com')]
    save_accounts(accounts, app.config_path)
    requests=[]
    balances={'ready@test.com':1,'empty@test.com':0}
    identities={email:'account-'+email for email in balances}
    def read(service):
        prefix,email=service.split(':',1)
        if prefix=='claude-switcher':
            return json.dumps({'claudeAiOauth':{'accessToken':email}})
        assert prefix=='codex-switcher'
        return json.dumps({'email':email, 'tokens':{'access_token':email,
            'account_id':identities.get(email,'unknown-account')}})
    class Response(io.BytesIO):
        status=200
    def send(request,timeout):
        provider='claude' if 'anthropic.com' in request.full_url else 'codex'
        email=request.get_header('Authorization').removeprefix('Bearer ')
        requests.append((provider, email, request))
        if email=='unknown@test.com':
            raise TimeoutError('synthetic')
        if request.get_method()=='POST':
            response={'result':'reset'} if provider=='claude' else {'code':'reset'}
        elif provider=='claude':
            response=eligible()
            response['five_hour']={'utilization':100,'resets_at':None}
            response['cedar_ember']['grants'][0]['resets_left']=balances[email]
        else:
            response={'rate_limit':{'primary_window':{'used_percent':100,'limit_window_seconds':18000}},
                      'rate_limit_reset_credits':{'available_count':balances[email],
                                                 'applicable_available_count':balances[email]}}
        return Response(json.dumps(response).encode())
    monkeypatch.setattr(claude_reset.keychain,'read_credentials',read)
    monkeypatch.setattr(claude_reset,'_open',send)
    monkeypatch.setattr(codex_usage,'urlopen',send)
    monkeypatch.setattr(codex_usage,'refresh_codex_credentials',lambda blob:None)
    import urllib.request
    monkeypatch.setattr(urllib.request,'urlopen',send)
    # The old app test fixture isolates eligibility; restore the real provider
    # client only within this fixture, where both HTTP and credentials are fake.
    from tests.test_app import _REAL_PREPARE_RESET
    with patch.object(claude_reset,'prepare_reset',_REAL_PREPARE_RESET), \
         patch.object(app_module.rumps,'MenuItem',ResetMenuItem), \
         patch.object(app_module.threading,'Thread',ImmediateThread), \
         patch.object(app_module,'_on_main_thread',side_effect=lambda fn:fn()), \
         patch.object(app,'_attempt_auto_switch',return_value=None):
        yield SimpleNamespace(app=app, module=app_module, accounts=accounts, requests=requests,
                              balances=balances, identities=identities)


def refresh(t):
    t.app._add_reset_menus(t.accounts)
    t.module.ClaudeSwitcherApp._fetch_all_usage(t.app)
    menus=[c.args[0] for c in t.app.menu.add.call_args_list]
    return {menu._provider: menu for menu in menus}


def test_both_providers_show_all_accounts_with_same_balance_and_usability_rules(two_providers):
    t=two_providers
    menus=refresh(t)
    assert set(menus)=={'claude','codex'}
    for provider,menu in menus.items():
        assert [row.title for row in menu.children]==[
            'ready@test.com (1 reset left, available)',
            'empty@test.com (0 resets left)',
            'unknown@test.com (could not check)']
        assert [row.callback is not None for row in menu.children]==[True,False,False]
        assert all(row._provider==provider for row in menu.children)
    assert all(req.get_method()=='GET' for _,_,req in t.requests)
    # Codex reset availability comes from the same request as ordinary usage.
    assert sum(p=='codex' and e=='ready@test.com' for p,e,_ in t.requests)==1
    assert sum(p=='claude' and e=='ready@test.com' for p,e,_ in t.requests)==2


@pytest.mark.parametrize('provider',['claude','codex'])
@pytest.mark.parametrize('confirm',[0,1])
def test_same_confirmation_cancel_and_provider_bound_redemption(two_providers, provider, confirm):
    t=two_providers
    row=refresh(t)[provider].children[0]
    t.requests.clear()
    with patch.object(t.module.rumps,'alert',return_value=confirm) as alert:
        row.callback(row)
    text=alert.call_args.kwargs['message']
    assert 'ready@test.com' in text and 'Resets left: 1' in text
    assert ('Claude Code' if provider=='claude' else 'Codex CLI') in text
    assert alert.call_args.kwargs['cancel']=='Cancel'
    posts=[req for p,e,req in t.requests if req.get_method()=='POST']
    assert len(posts)==confirm
    assert all(p==provider and e=='ready@test.com' for p,e,_ in t.requests)
    assert not t.app._reset_in_progress
    if confirm:
        assert ('anthropic.com' if provider=='claude' else 'chatgpt.com') in posts[0].full_url
        t.app._fetch_all_usage.assert_called_once()


@pytest.mark.parametrize('provider',['claude','codex'])
def test_balance_changes_during_confirmation_block_redemption(two_providers, provider):
    t=two_providers
    row=refresh(t)[provider].children[0]
    t.requests.clear()
    def confirm(**kwargs):
        t.balances['ready@test.com']=0
        return 1
    with patch.object(t.module.rumps,'alert',side_effect=confirm):
        row.callback(row)
    assert all(req.get_method()=='GET' for _,_,req in t.requests)


def test_codex_identity_changes_during_confirmation_block_redemption(two_providers):
    t=two_providers
    row=refresh(t)['codex'].children[0]
    t.requests.clear()
    def confirm(**kwargs):
        t.identities['ready@test.com']='other-account'
        return 1
    with patch.object(t.module.rumps,'alert',side_effect=confirm):
        row.callback(row)
    assert all(req.get_method()=='GET' for _,_,req in t.requests)


@pytest.mark.parametrize('provider',['claude','codex'])
def test_same_account_pending_guard_is_shared_and_provider_scoped(app_module,tmp_path,provider):
    app=_reset_app(app_module,tmp_path)
    row=SimpleNamespace(_provider=provider,_email='same@test.com')
    other=SimpleNamespace(_provider='codex' if provider=='claude' else 'claude',_email=row._email)
    with patch.object(app_module.threading,'Thread') as thread:
        app._on_reset_usage(row)
        app._on_reset_usage(row)
        app._on_reset_usage(other)
    assert thread.call_count==2


def test_unknown_codex_reset_metadata_is_not_displayed_as_zero():
    from claude_switcher.reset_service import CodexResetAdapter
    payload={'rate_limit':{'primary_window':{'used_percent':20}}}
    state=codex_usage.codex_usage_state(payload)
    status=CodexResetAdapter().poll('user@test.com',None,state)
    assert status.remaining is None and status.offer is None


def test_pending_manual_codex_confirmation_prevents_automatic_consumption(app_module,tmp_path):
    from claude_switcher.config import AppSettings, save_settings
    from claude_switcher.usage_state import UsageState,UsageWindow
    app=_reset_app(app_module,tmp_path)
    account=AccountInfo('user@test.com','pro','',True,'user@test.com',provider='codex')
    save_accounts([account],app.config_path)
    save_settings(AppSettings(auto_reset={'codex':True}),app.config_path)
    app._usage_state_cache={('codex',account.email):UsageState(True,'100%',(UsageWindow('5h',100),),1,1)}
    app._reset_in_progress={('codex',account.email)}
    with patch.object(codex_usage,'consume_reset_credit') as consume:
        assert app._attempt_auto_reset('codex') is None
    consume.assert_not_called()
