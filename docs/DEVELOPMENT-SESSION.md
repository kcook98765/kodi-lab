# Single disposable development session foundation

This opt-in foundation reserves one logical add-on/workspace, canonical target,
exact runtime revision and session generation. Presence of an active policy
pointer enables the restriction; migration creates empty tables and does not
activate it, enroll any existing resource or change grants. There is no new MCP
lifecycle/create/reset/delete authority. Web composition owns preparation.

Initial reservation requires no managed containers, no in-flight start/provision
reservation, no instance mutation lease and no global-heavy lease. Every stopped
container still occupies the provisioned environment. Existing unrelated resources
cause a conflict; no name-based cleanup, implicit adoption or eviction occurs.
Canonical identity and current approved runtime eligibility are rechecked using
the existing policy, including immutable cached artifact/profile/provenance proof.
This initial entry point permits stable/current and legacy releases; prerelease
selection needs its existing explicit preview acknowledgement in a later Web flow.
No runtime/image acquisition happens here.

All production broker provision paths now persist a reservation before Docker
dispatch, including when the opt-in policy is inactive. Session reservation and
provision/start admission use the same SQLite writer boundary. A delayed Advanced
provision therefore cannot race a new session's apparent empty inventory. A session
also constrains existing Advanced, reset and agent start paths at the broker; a
page-only guard or max_running=1 would leave a second stopped container possible.
Uncertain reservations survive process restart and never expire into a free slot.
Only known network/image preflight errors, raised before any create, release their
reservation and retain the prior session phase.

Phases are reserved → provisioning → bound, or uncertain after an unverified
outcome. Binding requires the exact post-provision target/runtime/container/image
inventory. Initial creation cannot adopt an existing idempotent result. The
selected profile can stop/resume without advancing session generation. If it
vanishes, an Advanced retry cannot silently replace it. An explicit absent/idle
close retains generation history and allows a new deliberate reservation.

An uncertain provision cannot be released merely because inventory is currently
empty: an accepted daemon create may still complete after a transport timeout.
The reservation remains fenced across restart, even after the application lease
ends. A future supported settlement review needs independently verified dispatch
completion, not an operator checkbox or inferred timeout. No automatic expiry or
absence-only clearing API is provided. An ordinary reserved/bound session can
close only with proven absence and no pending reservation/lease.

Workspace/source/history remain outside the profile. Workspace cleanup refuses
session references before removing bytes. Retained generations are capped at 256;
capacity exhaustion requires review, not automatic history deletion.

Migration 0080 adds the session history, singleton pointer and provision reservation
tables with foreign-key/check constraints. There is no data backfill, security
policy migration or resource removal. Populated history/reservations refuse lossy
downgrade. An older broker/application cannot enforce this opt-in policy; deployment
must update matched application/broker/schema bytes together and preserve backup/
recovery. Migration and deployment are separately authorized operational work.

This PR supplies the durable reservation/admission foundation, not the complete
#51 workflow. Simplified Web preparation, exact source attachment/copy, independent
native/Bridge readiness, selected diagnostic retention and explicit Fresh Test/
Switch composition remain required. No replacement or retirement is dispatched by
this service. Existing recovery fences remain authoritative; native sequential
version-switch and Windows acceptance remain later qualifications.

## Minimal Web workflow

Open **Development → Development session**. Select one ready workspace and an
eligible approved cached canonical runtime, then reserve the empty environment.
**Prepare Kodi** creates only the selected clean stopped profile. **Resume** and
**Stop** reuse its exact container without resetting source iterations. Links lead
to the selected Kodi's existing source/Bridge/native setup and workspace actions.
Preparation alone is not an assertion of deployment or capability readiness.

All actions use authenticated Web administration, Origin/CSRF checks and the
current session generation. Preparation rechecks runtime eligibility inside the
existing lifecycle claim; Resume/Stop recheck exact target/runtime/container before
dispatch. Stale bindings and unknown inventory refuse actions. Lost/failed browser
responses disable resubmission and direct the operator to status/history review.
No polling scheduler, image acquisition, new MCP tool or new grant is added.

**Release empty reservation** only closes an absent, idle ordinary session; it
never removes a profile or clears uncertain provision. Fresh Test and version
Switch remain outside this initial UI while retained diagnostics, dispatch
settlement and reviewed replacement remain incomplete. Existing unrelated
containers block reservation and are preserved. Native and Windows acceptance
remain separate from the route/Node/fixture evidence for this page.

## Save a Fresh Test / Switch review and hold evidence

The session page links to **Review Fresh Test or Switch**. Stop the current
session through the existing action, save diagnostics for that exact test, then
select the approved cached version and 1–8 retained captures. The ordinary form
displays the current session/container and next immutable runtime/image. It
requires acknowledgement that native crash reports are uncollected and excerpts
can be incomplete. Saving holds the selected bodies against explicit discard.
This is a durable review step; it does not execute retirement or replacement.

Migration 0082 adds two initially empty review/hold tables after 0081. A review
binds the exact current generation, runtime, container, image, workspace
generation/tree digest, source/add-on, next target/revision/image and selected
capture IDs/digests. The same target means Fresh Test; a different canonical
target means Switch Version. Cached runtime eligibility, one exact stopped
container, no other managed resources, busy/uncertain reservations and stored
reconciliation are rechecked inside SQLite writer admission. Capture bodies and
their historical identity/hash/typed schema are verified before the hold. No
source attachment, grants, baseline, Bridge trust or lifecycle evidence is copied.

Only one review can be open, with at most 128 retained review-history rows.
The 16 KiB UTF-8 snapshot bound and 2 GiB free-space reserve apply before
admission, including the actual database filesystem. Capacity refuses rather
than pruning. The request UUID replays its exact saved receipt even after a lost
response, process restart or later selection drift. It cannot be rebound to
another intent. Saving/cancellation uses Web authentication, same-origin and
CSRF checks; there is no new MCP authority or diagnostic-body exposure. Page
responses are non-cacheable. Review metadata links to private captured bodies.

Resume/Stop remain ordinary explicit actions. A saved review records facts at
creation; it does not freeze the session or claim that those facts stay current.
Final execution will need fresh eligibility/identity/lease/reconciliation and
evidence checks, exact confirmed retirement and an atomic next reservation.
Existing lifecycle history lacks an exact original-container retirement witness
and cannot supply that proof by inference. Unknown Docker creates remain fenced.
Missing inventory, elapsed time and service restart do not settle dispatch.

An absent session cannot be released while its review is open. **Cancel review**
requires its displayed snapshot digest and explicit confirmation. It releases
only that review's holds, preserves the saved review/capture history and leaves
the session, current profile and all other fences intact. Retained bodies can
then be separately discarded through their existing reviewed action. No worker,
automatic expiry, scheduler, profile removal or runtime mutation is introduced.
Populated review history refuses lossy downgrade even after cancellation. A
matched reader/application/schema is required to enforce holds; migration,
deployment, existing-resource execution and native/Windows acceptance remain
separately authorized work.

## Exact retirement receipt foundation

The trusted lifecycle service now accepts an optional exact disposable-runtime
scope for deprovision: original target, immutable revision/image and container.
This is an internal composition boundary, not a new Web/MCP action or grant.
The reviewed Fresh/Switch page still performs no retirement or replacement.

New empty migration 0083 adds `managed_runtime_retirements`. An exact prepared
scope and child lifecycle operation are claimed together under the existing
mutation lease. Stored pin/type/image are checked before claim; the actual
leased action checks current container/runtime/image again before clearing
readiness or invalidating trust/deployment evidence. Existing unscoped Advanced
deprovision does not invent or backfill exact witnesses for historical actions.

Only the exact broker removal reply followed by an unprovisioned lifecycle
postcondition can publish confirmation. Confirmation, successful terminal record,
owned projection and lease release commit in the same transaction. Failure to
publish rolls back terminalization and retains ownership for existing recovery;
cancellation after commit leaves the confirmed receipt intact. A lost or
mismatched reply, failed postcondition, interrupted dispatch or lost lease cannot
produce positive proof even when current inventory is empty. The new terminal
hook executes only after existing ownership guards and before history pruning.

Receipts preserve original identity outside disposable resources and protect
their exact lifecycle operation from pruning. Up to 256 scoped history rows are
admitted; exhaustion refuses before dispatch, without automatic overwrite or
pruning. Prepared/failed/unconfirmed and confirmed history all refuse lossy
downgrade. The typed read checks matching original operation/kind/state/time;
it never repairs or reconstructs proof from a historical success or absence.

A confirmed deprovision receipt does not settle an uncertain **create**, release
the old session, reserve a successor, copy trust/baselines or authorize another
destructive action. Those are separate steps. The next implementation must bind
the reviewed intent to this exact operation, keep selected evidence held, and
advance old closure plus new reservation atomically under shared admission.
Current-source tests use fake lifecycle/broker transports and temporary migrated
SQLite; they establish no native or existing-resource execution acceptance.
