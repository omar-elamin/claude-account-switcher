# Refactor spec v3 — deduplicate, don't re-architect

Repo: branch `dev` @ HEAD (d4cbaad). 192 tests. This is a PLAN; nothing is implemented.

## 0. What changed from v1 and why

Three independent reviewers (architecture, bug-free, over-engineering) rejected v1
for the SAME reason from three angles: the Provider/Strategy interface could not
express what the two providers actually do. Claude's live write needs the target
account's Keychain attribute and OAuth metadata, not just a blob; Codex's raw-bytes
compare-and-set breaks if reads normalise; Codex Add has import-unsaved and
restore-from-backup paths Claude lacks; only two of the four `_finish` blocks are
identical; and there are real UI differences (an expired Codex row opens login;
browser vs Terminal instructions). Every one of those is a genuine difference in
orchestration, not incidental duplication.

So v2 keeps the two provider modules and their orchestration **as they are**, and
removes only the duplication that is real, mechanical, and already caused drift.

**v3 (round 2):** all 18 round-1 issues were marked RESOLVED. Round 2 found two new
defects, each independently by all three reviewers with an executed probe:
(a) the two `_format_reset_delta` copies have DIFFERENT input contracts (Claude: ISO
string, numerics rejected — pinned by `tests/test_usage.py`; Codex: `float()` epoch
seconds), so one untagged formatter would change existing output; (b) Step 4's
orphan fix promised `atexit` protection against a hard kill, which `atexit` cannot
provide (it does not run on SIGKILL), and only Claude owns a `Popen` (Codex finds its
process by pattern), so "the lease owns the proc" was inaccurate.
v3 narrows again: dedupe only the countdown arithmetic, and **drop Step 4**.
Mapping of every prior issue to its resolution is in §7.

## 1. The problem (unchanged, and narrower than v1 claimed)

Drift observed this session, each a copy that lagged its twin:
- `_format_reset_delta` in `usage.py` crashed on `resets_at: null`; the copy in
  `codex_usage.py` guarded it.
- Claude login had no timeout; Codex had 300s.
- Claude Add had no "Opening…" feedback; Codex did.

These are the ONLY kind of duplication this plan removes: **identical or
nearly-identical helpers and app handlers that must stay identical**. Everything
that legitimately differs between providers stays where it is.

## 2. Pattern choice (design-patterns skill)

No new pattern. The skill's Anti-Patterns table applies directly: "premature
abstraction — wait for a clear pattern of repetition" and "prefer simpler
solutions over pattern application." The repetition that is clear is helper-level
and handler-level; the orchestration is not repeated, it is parallel-but-different.
The right move is *Extract Function* (Fowler), not Strategy.

## 3. The work — four steps, each its own green commit

### Step 1 — one home for the pure helpers (zero risk)
Move these to a single module and delete the copies. Callers import from the one
home. No behaviour change. `src/claude_switcher/common.py` (new) OR add to an
existing leaf module — implementer's call; the rule is ONE definition.

| Helper | Today | Note |
|---|---|---|
| `_EMAIL_RE`, `_validate_email` | core.py + codex_core.py | identical |
| `_EXTRA_PATHS`, `_find_claude` / `_find_codex` | core.py + codex_core.py | one `_find_binary(name)` |
| `_decode_jwt_payload` | codex_core.py + codex_usage.py | identical |
| `_format_reset_delta` | usage.py + codex_usage.py | **Only the countdown is shared.** The two differ in INPUT contract and must stay different: Claude parses an ISO string and rejects numerics (`_format_reset_delta(12345) == "?"` is asserted); Codex applies `float()` to epoch seconds (`12345 → "now"`). Extract ONE `_format_countdown(total_seconds: int) -> str` (the identical `days/hours/minutes` → `"Xd Yh" / "Xh Ym" / "Xm" / "now"` rendering) and keep each provider's `_format_reset_delta` as a thin parse-and-guard adapter that calls it. Keep the callers' distinct null handling (Claude omits the countdown; Codex shows `(?)`). Add characterization tests for both adapters: ISO, epoch, numeric string, None, non-string. |

NOT duplication, left alone (reviewer 3 was right): `_LOCK` in config.py vs
keychain.py protect different things; `CLAUDE_SERVICE` in core.py is an alias.

### Step 2 — one Add handler in app.py (the only handler pair that is identical)
`_on_add_claude_account` and `_on_add_codex_account` are the same workflow:
CLI check → restart-cancel if signing in → mark `_signing_in_since` → "Opening/
Restarting … login" notice → thread: wait-for-lease-release if restarting →
provider add → identical completion (`_finish`: notification, rebuild, refresh).

Replace with one `_on_add(sender)` reading `sender._provider`, driven by a small
per-provider table of the values that differ:
`(label, check_cli, add_fn, cancel_fn, login_instruction)`. The menu builds both
Add items from that table. The two identical Add `_finish` blocks become one.

Explicitly NOT collapsed (they differ): the switch completion (clears
`_switch_in_progress`, alert-vs-notification) and the usage completion (retry /
queued-refresh logic). `_on_claude_account_click` vs `_on_codex_account_click`
differ (expired Codex row → login) and stay; that branch is a real product
behaviour with a test (`tests/test_app.py` expired-row case).

### Step 3 — the app-side provider table replaces the `if provider ==` ladders
Where app.py branches only to pick a label / function / prefix, read the table
instead. Where a branch encodes a real difference (the expired-row click), keep it
and comment why. No quota; the reviewer checks each remaining branch is real.

### Step 4 — dropped (v3)
The lease/cancel state dedup is removed from this refactor. It was optional, carried
the highest handoff risk (two owners / deadlock, per round 1), and its orphan-repair
claim was unachievable (`atexit` does not run on SIGKILL). The plan stops at Step 3,
which already removes every copy that caused drift.

**Follow-up, NOT part of this refactor — recorded accurately:** an app instance that
is hard-killed leaves its `claude auth login` child orphaned to launchd (observed:
pid 51019, ppid 1, 1h07m). Only Claude owns that `Popen`; Codex launches via
Terminal. Orderly Quit could kill the Claude child; a hard kill cannot be caught in
the app, so the realistic mitigation is lifecycle work (process-group / parent-death
handling) with its own tests. Also: any "is a sign-in live?" check must verify the
process's parent is the running app, not merely that a `claude auth login` exists.

## 4. Behaviour that MUST be preserved
Unchanged from v1.1 §4 (ten invariants), all already pinned by tests. This is a
behaviour-preserving refactor; assertions move but do not weaken. Test imports
and patch targets are updated **in the same commit** as each move (reviewer 1/2).

## 5. Explicit DO-NOT list
- No `Provider` class / protocol / strategy object. No `accounts.py`.
- Do not move Add/switch/remove/import orchestration out of core.py / codex_core.py.
- Do not move Codex refresh / compare-and-set out of codex_usage.py.
- Do not delete core.py, codex_core.py, or codex_usage.py.
- No registry, ABC, DI, event bus, Command, State machine, async, metaclass.
- No line-count targets, no "zero names in two modules" target, no branch quota.

## 6. Acceptance — a checklist, not metrics
- [ ] Each helper in Step 1 has exactly one definition; its copies are deleted.
- [ ] One `_format_countdown` implementation; two thin parse adapters retained; characterization tests for both (ISO, epoch, numeric string, None, non-string) pass with today's outputs unchanged.
- [ ] One `_on_add` handler; one Add completion block; both Add menu items built from the table.
- [ ] Every remaining `provider ==` branch in app.py is a documented real difference.
- [ ] All existing tests pass; no assertion weakened; count drops only where two provider-specific tests become one parametrised test.
- [ ] No new dependencies. Module count ≤ today's + 1 (`common.py`), or +0 if helpers land in an existing leaf module.
- [ ] The three drift bugs in §1 cannot recur: the countdown rendering, the login timeout, and the Add feedback each have a single implementation.
Line counts are reported for information only.

## 7. v1 issue → v2 resolution
| v1 blocking issue (R1/R2/R3) | v2 |
|---|---|
| Provider interface can't express Claude metadata / identity precedence | **Dropped** — no interface; orchestration untouched |
| Normalised read breaks Codex raw CAS; UsageState loses fetch-failed | **Dropped** — codex_usage refresh/CAS untouched |
| Shared Add omits Codex import-unsaved / restore-from-backup | **Dropped** — core adds untouched; only the app *handler* is shared |
| Real UI differences overlooked | **Preserved** — expired-row click and instruction text kept, documented |
| 4 `_finish` are not identical | **Corrected** — only the 2 Add completions merge |
| Migration hands off shared state unsafely / two owners / deadlock | **Narrowed** — lease dedup is optional Step 4, one atomic commit, overlap tests required, no lock held while waiting |
| Cancel/restart has two owners | **One owner** — restart stays in the shared app handler; core add still refuses a held lease as the safety net |
| Metrics measure names/lines, §7 forces the architecture | **Replaced** — checklist of named duplicates removed; no targets that pressure structure |
| **(round 2)** `_format_reset_delta` copies have different input contracts; one formatter changes output | **Narrowed** — dedupe only `_format_countdown`; parse adapters + null handling stay per provider; characterization tests required |
| **(round 2)** Step 4 `atexit` cannot survive SIGKILL; only Claude owns a Popen | **Dropped** — Step 4 removed; orphan issue recorded accurately as a separate lifecycle follow-up |
