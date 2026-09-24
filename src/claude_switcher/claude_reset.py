"""Manual Claude Code resets. Reads never redeem; POST requires a fresh offer.

The cedar_ember contract is from Claude Code 2.1.281. Unsupported or changed
responses fail closed. There is no timer, automatic redemption, or HTTP retry.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import time
import urllib.error
import urllib.request
from uuid import UUID, uuid4

from claude_switcher import core, keychain
from claude_switcher.config import DEFAULT_CONFIG_PATH, load_accounts

ELIGIBILITY_URL = 'https://api.anthropic.com/api/oauth/usage?cedar_ember=1&skip_spend=1'
_WINDOWS = frozenset(('five_hour', 'seven_day', 'seven_day_overage_included',
    'seven_day_opus', 'seven_day_sonnet', 'seven_day_cowork',
    'seven_day_omelette', 'seven_day_oauth_apps'))
_RESULTS = frozenset(('reset', 'already_used', 'not_limited', 'cooldown', 'ineligible', 'unavailable'))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open(request, timeout):
    return urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout)


@dataclass(frozen=True)
class ResetOffer:
    email: str
    organization: str
    grant_id: str
    label: str
    resets_left: int
    clears: tuple[str, ...]
    starts_at: str | None
    ends_at: str | None
    credential_digest: str = field(repr=False)
    request_id: str
    confirmation_id: str
    total_remaining: int


@dataclass(frozen=True)
class Availability:
    email: str
    remaining: int | None = None
    offer: ResetOffer | None = None


@dataclass
class _Attempt:
    created: float
    request_id: str
    confirmations: set[str] = field(default_factory=set)


# Retain the idempotency key for explicit retries, matching the CLI's ten-minute
# window. Each confirmation can send at most once, including overlapping calls.
_request_ids: dict[tuple[str, str, str], _Attempt] = {}


def _target(email, path):
    if core._add_in_progress:
        return None
    accounts = [a for a in load_accounts(path) if a.provider == 'claude' and a.email == email]
    if len(accounts) != 1:
        return None
    oauth = accounts[0].oauth_account
    if not isinstance(oauth, dict) or oauth.get('emailAddress') != email:
        return None
    try:
        organization = str(UUID(oauth['organizationUuid']))
        credentials = keychain.read_credentials('claude-switcher:' + email)
        token = json.loads(credentials)['claudeAiOauth']['accessToken']
        if not isinstance(token, str) or not token:
            return None
        return organization, token, hashlib.sha256(credentials.encode()).hexdigest()
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


def _request(token, url, body=None):
    request = urllib.request.Request(url, method='GET' if body is None else 'POST',
        data=None if body is None else json.dumps(body).encode(), headers={
            'Authorization': 'Bearer ' + token,
            'anthropic-beta': 'oauth-2025-04-20',
            'User-Agent': 'claude-cli/2.1.281 (external, cli)',
            'Accept': 'application/json', 'Content-Type': 'application/json',
        })
    with _open(request, timeout=10 if body is None else 25) as response:
        return json.loads(response.read())


def _timestamp(value):
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('missing timezone')
    return parsed.timestamp()


def _grant(raw):
    """Strict validation of fields that control consent or availability."""
    if not isinstance(raw, dict):
        return None
    grant_id = raw.get('id')
    if (not isinstance(grant_id, str) or not 1 <= len(grant_id) <= 40
            or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789_-' for c in grant_id)):
        return None
    if not isinstance(raw.get('label'), str) or not raw['label']:
        return None
    for key in ('resets_total', 'resets_left'):
        if type(raw.get(key)) is not int or raw[key] < 0:
            return None
    if raw['resets_left'] > raw['resets_total']:
        return None
    for key in ('paused', 'usable_now', 'use_requires_limit'):
        if type(raw.get(key)) is not bool:
            return None
    clears = raw.get('clears')
    if (not isinstance(clears, list) or not clears
            or any(not isinstance(w, str) or w not in _WINDOWS for w in clears)):
        return None
    try:
        _timestamp(raw.get('starts_at'))
        _timestamp(raw.get('ends_at'))
    except (ValueError, TypeError, AttributeError, OverflowError):
        return None
    return raw


def _availability(email, target):
    organization, token, digest = target
    response = _request(token, ELIGIBILITY_URL)
    state = response.get('cedar_ember') if isinstance(response, dict) else None
    if not isinstance(state, dict) or not isinstance(state.get('grants'), list):
        return Availability(email)
    grants = [_grant(g) for g in state['grants']]
    if any(g is None for g in grants):
        return Availability(email)
    remaining = sum(g['resets_left'] for g in grants)
    unavailable = Availability(email, remaining)
    if state.get('eligible') is not True or type(state.get('at_limit')) is not bool:
        return unavailable
    now = datetime.now(timezone.utc).timestamp()
    try:
        cooldown = _timestamp(state.get('cooldown_until'))
    except (ValueError, TypeError, AttributeError, OverflowError):
        return unavailable
    if cooldown is not None and cooldown > now:
        return unavailable
    selected = [g for g in grants if g['id'] == state.get('next_grant_id')]
    if len(selected) != 1:
        return unavailable
    grant = selected[0]
    starts, ends = _timestamp(grant.get('starts_at')), _timestamp(grant.get('ends_at'))
    if (not grant['usable_now'] or grant['paused'] or grant['resets_left'] == 0
            or (grant['use_requires_limit'] and not state['at_limit'])
            or (starts is not None and starts > now) or (ends is not None and ends <= now)):
        return unavailable
    key = (email, organization, grant['id'])
    clock = time.monotonic()
    for old in list(_request_ids):
        if clock - _request_ids[old].created >= 600:
            del _request_ids[old]
    attempt = _request_ids.setdefault(key, _Attempt(clock, str(uuid4())))
    offer = ResetOffer(email, organization, grant['id'], grant['label'], grant['resets_left'],
        tuple(grant['clears']), grant.get('starts_at'), grant.get('ends_at'), digest,
        attempt.request_id, str(uuid4()), remaining)
    return Availability(email, remaining, offer)


def prepare_reset(email, config_path=DEFAULT_CONFIG_PATH):
    """GET only. The caller must show this account and offer before redemption."""
    with core._CLAUDE_LOCK:
        try:
            target = _target(email, config_path)
            return _availability(email, target) if target else Availability(email)
        except (OSError, ValueError, TypeError, RuntimeError):
            return Availability(email)


def redeem_reset(offer, config_path=DEFAULT_CONFIG_PATH):
    """Manual confirmation only; revalidate target and grant before one POST."""
    with core._CLAUDE_LOCK:
        key = (offer.email, offer.organization, offer.grant_id)
        attempt = _request_ids.get(key)
        if not attempt or attempt.request_id != offer.request_id:
            return 'changed'
        if offer.confirmation_id in attempt.confirmations:
            return 'busy'
        # Mark before any I/O so even uncertain outcomes cannot retry themselves.
        attempt.confirmations.add(offer.confirmation_id)
        try:
            target = _target(offer.email, config_path)
            if not target or (target[0], target[2]) != (offer.organization, offer.credential_digest):
                return 'changed'
            current = _availability(offer.email, target).offer
            if current is None:
                return 'changed'
            consent_fields = ('email', 'organization', 'grant_id', 'label', 'resets_left',
                              'clears', 'starts_at', 'ends_at', 'credential_digest', 'request_id', 'total_remaining')
            if any(getattr(current, k) != getattr(offer, k) for k in consent_fields):
                return 'changed'
            # Re-read after the GET too. External sign-in/config edits may happen
            # despite the in-process account lock.
            if _target(offer.email, config_path) != target:
                return 'changed'
        except (OSError, ValueError, TypeError, RuntimeError):
            return 'unavailable'
        try:
            response = _request(target[1],
                f'https://api.anthropic.com/api/organizations/{offer.organization}/reset_rate_limits',
                {'program': 'cedar_ember', 'grant_id': offer.grant_id, 'request_id': offer.request_id})
            result = response.get('result') if isinstance(response, dict) else None
            return result if isinstance(result, str) and result in _RESULTS else 'unknown'
        except (OSError, ValueError, TypeError, RuntimeError):
            # A timeout/server failure may follow a successful redemption. Never
            # retry here or claim no credit was consumed. No raw provider errors.
            return 'unknown'
