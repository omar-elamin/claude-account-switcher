# Usage reset architecture

Claude Code and Codex CLI share one manual reset journey: show each account's
balance, check availability, confirm the provider and account, recheck consent,
redeem, then refresh the balance. Unavailable accounts remain visible and disabled.
Unknown balances are shown as unknown, not zero.

The earlier implementation added Claude beside an existing Codex flow. That left
two menu builders, two manual handlers, different cache shapes, and different
rules for which accounts appeared. Those UI differences were not API requirements.

The shared contract is in `reset_service.py`: an immutable `ResetStatus` and
`ResetOffer`, plus a `ResetAdapter` protocol. The provider registry in `app.py`
selects the adapter. The app has one menu builder, one cache keyed by provider and
email, one manual handler, and one label updater. `reset_ui.py` formats both
providers. A provider's opaque ticket stays inside its adapter.

| Responsibility | Claude adapter | Codex adapter |
| --- | --- | --- |
| Background availability | Separate eligibility GET | Reuse normal usage response |
| Consent binding | Account, organization, credential fingerprint, grant details and balance | Account ID and balance; token refresh may retain the same account ID |
| Redemption | Grant ID and request UUID | Redeem request UUID |
| Network retry | No automatic POST retry | Existing single retry with the same request UUID |
| Automatic redemption | Shared opt-in policy, off by default | Shared opt-in policy, off by default |

With auto-reset off, polling only reads usage and eligibility. Selecting a usable
row performs a fresh check before showing confirmation. Cancelling sends no reset
request. The backend checks consent again before POST. Both providers report an
uncertain response as uncertain.

Each provider has an independent Auto-reset option, off by default. When enabled,
the shared policy requires an exhausted active account and no known account to
switch to. It prefers a usable reset on the active account, then an exhausted
backup account of the same provider. The app rereads target usage and eligibility
before redemption. It rechecks the triggering active account, setting, and known
alternatives immediately before POST, including after the backend's final read.

Manual and automatic requests share an atomic guard keyed by provider and email.
Automatic attempts have a one-minute provider cooldown and a one-hour account
cooldown, including uncertain outcomes. Enabling or disabling one provider does
not change the other provider. Each automatic result gets its own notification,
and completed attempts refresh usage and reset availability.

This uses Adapter for the two API contracts and the existing registry to select
behavior. It avoids an inheritance-based Template Method because authentication,
retry, and grant rules do not share an HTTP algorithm. A new event bus or state
class hierarchy would add indirection without solving the duplication.

Authentication and account switching still use the existing provider modules.
This change does not merge token stores, login methods, refresh policies, or the
Codex gateway into a generic HTTP client. Those boundaries protect account identity
and need separate changes if they are revised.

Tests exercise both providers through the same menu and confirmation journey,
using real request construction and synthetic HTTP responses. They cover identical
emails across providers, changing balances and identities, cancellation, uncertain
responses, duplicate clicks, and the native Cocoa menu. Provider-specific auth and
transport tests remain in place. No real reset is needed to run this coverage.
