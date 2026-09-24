"""Manual Claude reset contract. Every HTTP response and credential is synthetic."""
import copy
import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from claude_switcher import claude_reset as reset
from claude_switcher.config import AccountInfo, save_accounts

EMAIL = 'claude@example.test'
ORG = '11111111-1111-4111-8111-111111111111'


def eligible():
    return {'cedar_ember': {'eligible': True, 'at_limit': True,
        'next_grant_id': 'launch', 'cooldown_until': None, 'grants': [{
        'id': 'launch', 'label': 'Launch reset', 'resets_total': 1, 'resets_left': 1,
        'starts_at': '2020-01-01T00:00:00+00:00', 'ends_at': '2099-01-01T00:00:00+00:00',
        'clears': ['five_hour', 'seven_day'], 'paused': False,
        'usable_now': True, 'use_requires_limit': True}]}}


@pytest.fixture
def setup_reset(tmp_path, monkeypatch):
    path = tmp_path / 'accounts.json'
    accounts = [AccountInfo(EMAIL, 'max', 'test', False, EMAIL,
                {'emailAddress': EMAIL, 'organizationUuid': ORG}),
                AccountInfo(EMAIL, 'pro', '', True, EMAIL, provider='codex')]
    save_accounts(accounts, path)
    creds = {'value': json.dumps({'claudeAiOauth': {'accessToken': 'synthetic-claude'}})}
    services, requests = [], []
    def read(service):
        services.append(service)
        assert service == 'claude-switcher:' + EMAIL
        return creds['value']
    monkeypatch.setattr(reset.keychain, 'read_credentials', read)
    body = {'get': eligible(), 'post': {'result': 'reset', 'resets_left': 0}}
    def send(request, timeout):
        requests.append(request)
        data = body['post' if request.get_method() == 'POST' else 'get']
        if isinstance(data, Exception):
            raise data
        return io.BytesIO(json.dumps(data).encode())
    monkeypatch.setattr(reset, '_open', send)
    reset._request_ids.clear()
    return SimpleNamespace(path=path, accounts=accounts, creds=creds, body=body,
                           requests=requests, services=services)


def test_check_only_reads_and_confirmed_redemption_is_bound_to_target(setup_reset):
    s = setup_reset
    status = reset.prepare_reset(EMAIL, s.path)
    assert status.remaining == 1 and status.offer.email == EMAIL
    assert [r.get_method() for r in s.requests] == ['GET']
    assert s.requests[0].full_url == 'https://api.anthropic.com/api/oauth/usage?cedar_ember=1&skip_spend=1'
    assert reset.redeem_reset(status.offer, s.path) == 'reset'
    assert [r.get_method() for r in s.requests] == ['GET', 'GET', 'POST']
    request = s.requests[-1]
    assert request.full_url == f'https://api.anthropic.com/api/organizations/{ORG}/reset_rate_limits'
    assert request.get_header('Authorization') == 'Bearer synthetic-claude'
    assert json.loads(request.data) == {'program': 'cedar_ember', 'grant_id': 'launch',
                                     'request_id': status.offer.request_id}
    assert 'synthetic-claude' not in repr(status)


@pytest.mark.parametrize('change', ['missing', 'ineligible', 'exhausted', 'paused', 'not_usable',
    'not_limited', 'expired', 'future', 'unknown_window', 'bad_count', 'bad_bool', 'missing_limit', 'no_next', 'cooldown'])
def test_unavailable_or_malformed_grants_never_offer_a_reset(setup_reset, change):
    s = setup_reset
    state = s.body['get']['cedar_ember']; grant = state['grants'][0]
    if change == 'missing': s.body['get'] = {}
    elif change == 'ineligible': state['eligible'] = False
    elif change == 'exhausted': grant['resets_left'] = 0
    elif change == 'paused': grant['paused'] = True
    elif change == 'not_usable': grant['usable_now'] = False
    elif change == 'not_limited': state['at_limit'] = False
    elif change == 'expired': grant['ends_at'] = '2000-01-01T00:00:00Z'
    elif change == 'future': grant['starts_at'] = '2090-01-01T00:00:00Z'
    elif change == 'unknown_window': grant['clears'] = ['unknown']
    elif change == 'bad_count': grant['resets_left'] = True
    elif change == 'bad_bool': grant['usable_now'] = 'false'
    elif change == 'missing_limit': del grant['use_requires_limit']
    elif change == 'no_next': state['next_grant_id'] = None
    elif change == 'cooldown': state['cooldown_until'] = '2099-01-01T00:00:00Z'
    assert reset.prepare_reset(EMAIL, s.path).offer is None
    assert all(r.get_method() == 'GET' for r in s.requests)


@pytest.mark.parametrize('change', ['grant', 'windows', 'count', 'organization', 'credentials', 'removed'])
def test_stale_confirmation_does_not_post(setup_reset, change):
    s = setup_reset; offer = reset.prepare_reset(EMAIL, s.path).offer
    if change == 'grant': s.body['get']['cedar_ember']['next_grant_id'] = 'other'
    elif change == 'windows': s.body['get']['cedar_ember']['grants'][0]['clears'] = ['five_hour']
    elif change == 'count': s.body['get']['cedar_ember']['grants'][0]['resets_left'] = 0
    elif change == 'organization':
        s.accounts[0].oauth_account['organizationUuid'] = '22222222-2222-4222-8222-222222222222'
        save_accounts(s.accounts, s.path)
    elif change == 'credentials': s.creds['value'] = json.dumps({'claudeAiOauth': {'accessToken': 'changed'}})
    elif change == 'removed': save_accounts(s.accounts[1:], s.path)
    assert reset.redeem_reset(offer, s.path) == 'changed'
    assert all(r.get_method() == 'GET' for r in s.requests)


@pytest.mark.parametrize('result', ['reset', 'already_used', 'not_limited', 'cooldown', 'ineligible', 'unavailable'])
def test_provider_results_are_reported_without_retry(setup_reset, result):
    s = setup_reset; offer = reset.prepare_reset(EMAIL, s.path).offer
    s.body['post'] = {'result': result}
    assert reset.redeem_reset(offer, s.path) == result
    assert sum(r.get_method() == 'POST' for r in s.requests) == 1


@pytest.mark.parametrize('response', [TimeoutError('synthetic-claude'), {}, {'result': 'future_result'},
                                     HTTPError('unused', 500, 'synthetic-claude', {}, None)])
def test_ambiguous_result_is_not_retried_and_reuses_request_id_on_explicit_retry(setup_reset, response):
    s = setup_reset; offer = reset.prepare_reset(EMAIL, s.path).offer
    s.body['post'] = response
    assert reset.redeem_reset(offer, s.path) == 'unknown'
    assert sum(r.get_method() == 'POST' for r in s.requests) == 1
    again = reset.prepare_reset(EMAIL, s.path).offer
    assert again.request_id == offer.request_id


def test_overlapping_redemptions_send_only_one_request_for_same_offer(setup_reset):
    s = setup_reset; offer = reset.prepare_reset(EMAIL, s.path).offer
    barrier = threading.Barrier(2)
    def run():
        barrier.wait()
        return reset.redeem_reset(offer, s.path)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: run(), range(2)))
    assert results.count('reset') == 1
    assert sum(r.get_method() == 'POST' for r in s.requests) == 1


def test_redirect_handler_never_follows_even_same_host(setup_reset):
    import urllib.request
    handler = reset._NoRedirect()
    request = urllib.request.Request(reset.ELIGIBILITY_URL)
    assert handler.redirect_request(request, None, 302, '', {}, 'https://api.anthropic.com/elsewhere') is None


def test_failed_eligibility_never_exposes_credentials(setup_reset):
    s = setup_reset; s.body['get'] = TimeoutError('synthetic-claude')
    status = reset.prepare_reset(EMAIL, s.path)
    assert status.offer is None and status.remaining is None
    assert 'synthetic-claude' not in repr(status)
