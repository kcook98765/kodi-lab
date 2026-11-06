# Issue #238 Phase 4: managed Kodi Web delivery

Base: PR #241, `94e99b1b17ed9f139911f47392fcebf5f3e8667c`.
This draft is stacked on the clean Reset branch, leaving #239/#240/#241 intact.
It implements the ordinary human workflow; deployment, merge, native qualification
and automatic version switching are excluded.

## Audit and implementation

The old Kodi landing page foregrounded a selected development workspace rather
than a runnable version. Guided Create Kodi, Current Kodi and exact instance
pages offered overlapping lifecycle entry points. Existing standard noVNC already
supported viewing without development selection, while the version and clean
Reset coordinators supplied the necessary identity/lease/recovery protections.
The older disposable fixture-reset service can restore fixtures and is unsuitable
for ordinary clean Reset.

`/kodi` now presents Matrix, Nexus, Omega and Piers preview with preparation,
state, native readiness and Start/Stop/Reset/Open Screen. Missing files link to
reviewed guided installation; Start uses approved cache only. Stop preserves the
exact profile; confirmed Reset uses the existing clean coordinator and leaves its
replacement stopped. A running container with failed native readiness still
owns capacity; another version is not offered as an implicit switch. Unknown,
conflicting, duplicate, busy or protected state fails closed with recovery links.

Technical identity, receipts, runtime installation/approval, Bridge, sources,
workspaces, security and reconciliation remain in Advanced. Current Kodi's
lifecycle controls are collapsed under Advanced. Guided installation defaults
to stopped preparation and returns to the version page; its optional startup is
Advanced. Household and external Kodi administration is preserved.

### Thin administrator adapter

`web/managed_slots.py` composes the reviewed coordinators. Browser confirmation
is bound to a fingerprint of canonical version, exact profile/container/pin/image,
observation epoch, selected-session identity and reservations. Its immutable
`managed_web_intents` ledger precedes dispatch. The same original UUID binds to
one existing shared parent intent; parent/child recovery, exact retirement proof,
no second uncertain removal and no creation over uncertain removal are unchanged.
GET/status/refresh never invoke or advance an operation. Explicit continuation
rechecks the live administrator session and original binding.

A reserved Web administrator receipt-owner UUID occupies the existing parent
schema's legacy `client_id` field without creating an MCP client or credential.
It maps to the existing `web` resource actor; MCP authorization explicitly denies
that namespace. Existing presets, grants, destructive agent permissions, external
Kodi scope and runtime authority are unchanged. Browser requests retain existing
Origin/CSRF checks, with read-only live-session rechecks inside lifecycle writer
transactions (the usual session resolver can touch last-use and open a writer).

Approved cached registration reuses the existing reviewed-recipe canonical target
checks, factored from bootstrap. Only administrator dispatch may create an absent
canonical Target row; MCP still needs its pre-existing exact target/grant. No
image download, approval, repin, profile adoption or migration is introduced.

One accepting-transaction integration correction publishes Reset's bound parent
before reauthorization. Empty-slot canonical registration intentionally changes
the initial snapshot. Guard failure rolls the parent, registration and hold
changes back together. This preserves all destructive dispatch/recovery rules;
the clean Reset regression suite passes with the correction.

Migration `0005_managed_web` adds only the browser ledger and immutable-update
trigger. It changes no policies, grants, existing profiles or receipts, and refuses
a lossy downgrade once confirmations exist. Existing parent receipt limits also
apply in addition to the Web ledger's 4,096-request cap. Quota exhaustion needs
Advanced review; receipts are never silently pruned or reused.

## Focused verification

**FOCUSED PASS**, using isolated databases, fake broker/native transports and Node
DOM harnesses. Every new/materially changed test file was selected individually
by `scripts/check-focused`. No full release suite or native runtime test ran.

| Selection | Result |
| --- | --- |
| New administrator Web adapter (`test_managed_slots.py`) | 20 passed: four versions, cache-only empty Start/Reset, retained Stop, confirmed clean replacement, no MCP impersonation, missing cache, duplicate refusal, stale confirmation, live session revocation, CSRF/Origin, client/viewer/input/playback blockers, concurrent requests, original UUID replay, uncertain provision/removal, read-only refresh, workspace-free view-only screen entry, stale ticket, clean development guidance, native failure and explicit version switching |
| New Web DOM/semantics (`test_managed_slots_dom.py`) | 2 passed, including Node controller: no automatic mutation, request saved before POST, lost reply replay, checkbox/cancel/focus, stale dialog, original pending receipt, uncertain recovery, storage denial, semantic labels and responsive/mobile CSS contract |
| New migration (`test_managed_web_migration.py`) | 2 passed: preserved authority, matching schema, immutable consent/binding, clean integrity and downgrade refusal |
| Shared version/Reset and MCP (`test_managed_versions.py`, `test_managed_clean_reset.py`, both MCP files) | 35 + 42 + 9 + 7 passed; Reset rerun after accepting-transaction correction |
| Authentication and reviewed recipe registration | 9 + 47 passed, including read-only writer-transaction session recheck, expiry/revocation and existing bootstrap behavior |
| Current Kodi, navigation, guided Create, ordinary Start and noVNC authority | 61 + 28 + 7 + 3 + 8 passed; guided Create invokes its existing Node controller |
| Head-schema, migration, recovery, transition and artifact compatibility (12 files) | 323 passed, 9 existing gated transition skips; current-head expectations/table inventories updated mechanically for 0005 |

Total across unique selected tests: **603 passed, 9 gated skips**. Focused Ruff
format/lint and strict mypy pass over all 29 changed/new Python files. Both changed
production JavaScript controllers pass Node syntax checks. DOM/fixture results
are not real-browser, native Kodi, graphical noVNC or deployed acceptance.

During development, an input-blocker test leaked its in-memory guard into later
cases; the fixture now releases its exact handle in a finalizer. Another test
incorrectly expected broker response loss to be safely resumable; it now asserts
uncertainty and no redispatch. The empty-slot confirmation test exposed the
accepting-transaction integration correction described above. These were fixed
before the final focused pass; no production guard was relaxed.

## Remaining acceptance and follow-ups

- Native Linux and Windows/WSL2: all four cached runtimes, retained Stop/Start,
  clean Reset, exact retirement, Bridge recovery, deployment and real source/Git
  preservation. A source-only draft does not qualify or upgrade an installed Lab.
- Real desktop/mobile keyboard and browser rendering; first-viewer framebuffer,
  disconnect/input cleanup, stale-page behavior and actual graphics remain to
  qualify. The existing noVNC transport/security contract is reused unchanged.
- Existing noncanonical/duplicate profiles require explicit administrator review;
  no automatic migration/adoption/deletion occurs. Revoked or unresolved runtime
  eligibility may require Advanced recovery before ordinary controls reappear.
- After Reset, the replacement is unselected for development. Existing retained
  version selection must be enabled/valid, then the user explicitly binds the
  workspace, prepares Bridge and deploys again. Older single-session setup must
  not prepare a duplicate profile. Fully automatic reattachment is a follow-up.
- Automatic switching still needs a durable parent covering both exact version
  authorities, active users/holds and proven Stop before new Start. The Web UI
  deliberately requires explicit Stop followed by Start.

All four human actions are implemented for eligible canonical slots. Issue #238
remains open for the separate switching/development follow-ups and qualification.
Nothing is merged, deployed or run against installed Kodi/container/profile state.
