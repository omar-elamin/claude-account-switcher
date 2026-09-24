"""One reset contract; provider adapters retain their own HTTP/auth protocols."""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from claude_switcher import claude_reset, codex_usage
from claude_switcher.config import get_active_account
from claude_switcher.usage_state import UsageState


@dataclass(frozen=True)
class ResetOffer:
    provider: str
    email: str
    total_remaining: int
    label: str
    clears: tuple[str, ...]
    ends_at: str | None = None
    # The app presents common fields; only the owning adapter uses this ticket.
    ticket: object = field(default=None, repr=False)


@dataclass(frozen=True)
class ResetStatus:
    remaining: int | None = None
    offer: ResetOffer | None = None


class ResetAdapter(Protocol):
    supports_automatic: bool

    def poll(self, email: str, path: Path, usage: UsageState) -> ResetStatus: ...
    def prepare(self, email: str, path: Path) -> ResetStatus: ...
    def redeem(self, offer: ResetOffer, path: Path) -> str: ...


class ClaudeResetAdapter:
    supports_automatic = False

    def poll(self, email, path, usage):
        # Claude's ordinary usage response does not contain grant eligibility.
        return self.prepare(email, path)

    def prepare(self, email, path):
        status = claude_reset.prepare_reset(email, path)
        native = status.offer
        offer = None if native is None else ResetOffer(
            'claude', email, native.total_remaining, native.label,
            native.clears, native.ends_at, native)
        return ResetStatus(status.remaining, offer)

    def redeem(self, offer, path):
        if offer.provider != 'claude' or not isinstance(offer.ticket, claude_reset.ResetOffer):
            return 'changed'
        return claude_reset.redeem_reset(offer.ticket, path)


@dataclass(frozen=True)
class _CodexTicket:
    account_id: str


class CodexResetAdapter:
    supports_automatic = True

    def poll(self, email, path, usage):
        # Reuse the normal usage read. Do not make a second Codex request.
        if usage is None or not usage.available or not usage.reset_counts_known:
            return ResetStatus()
        remaining = usage.reset_credits
        if type(remaining) is not int or remaining < 0:
            return ResetStatus()
        offer = None
        if remaining > 0 and usage.reset_applicable > 0:
            offer = ResetOffer('codex', email, remaining, 'Usage-limit reset',
                               ('five_hour', 'seven_day'))
        return ResetStatus(remaining, offer)

    def prepare(self, email, path):
        identity = codex_usage.reset_account_id(email, path)
        if identity is None:
            return ResetStatus()
        active = get_active_account(path, provider='codex')
        usage = (codex_usage.fetch_active_codex_usage(path)
                 if active and active.email == email
                 else codex_usage.fetch_codex_usage_for_account(email, path))
        if codex_usage.reset_account_id(email, path) != identity:
            return ResetStatus()
        status = self.poll(email, path, codex_usage.codex_usage_state(usage))
        if status.offer is None:
            return status
        native = status.offer
        return ResetStatus(status.remaining, ResetOffer(
            native.provider, native.email, native.total_remaining, native.label,
            native.clears, ticket=_CodexTicket(identity)))

    def redeem(self, offer, path):
        if offer.provider != 'codex' or not isinstance(offer.ticket, _CodexTicket):
            return 'changed'
        try:
            code = codex_usage.consume_reset_credit(offer.email, path,
                expected_account_id=offer.ticket.account_id,
                expected_credits=offer.total_remaining)
        except Exception:
            # A lost response can follow a successful reset. Do not claim failure
            # or expose raw provider/credential errors through the common UI.
            return 'unknown'
        return {'reset': 'reset', 'nothing_to_reset': 'not_limited',
                'no_credit': 'unavailable', 'already_redeemed': 'already_used',
                'changed': 'changed'}.get(code, 'unknown')

    def redeem_automatically(self, email, path):
        # Existing opt-in Codex policy calls this; Claude has no such operation.
        return codex_usage.consume_reset_credit(email, path)
