# Create Kodi registration and provisioning (issue #196, milestone 2B)

This milestone exposes two explicitly invoked application adapters. They reuse
the existing canonical bootstrap and lifecycle provision service. There is no
runner, Web API, wizard, Start, native readiness, Bridge or agent integration.
There is no migration, new table, broker protocol or authority change.

## Exact runtime and registration

Both adapters revalidate the parent's approved recorded recipe, canonical target,
runtime revision and image against an exact Ready installation record, sealed
eligibility/validation and the broker's image observation. A prospective unused
installation UUID from cache reuse is never mistaken for a succeeded child;
the exact catalog binding supplies the verified source. No installer launch,
reacquisition, runtime reassignment or profile replacement is requested.

An existing managed registration is compatible only with the exact accepted pin,
managed target/release and supported disposable instance type. Its name and other
existing settings are preserved. An older pin within the same definition is not
silently accepted as the new approved pin. Conflicts fail closed.

`CreateKodiRegistrationService.register()` uses the existing
`bootstrap_recipe_instance()` when registration is absent. It commits Planned
and then Dispatched before either of the helper's existing local transactions.
That helper may leave a Target without a ManagedInstance if interrupted. The
parent preserves its original local correlation UUID and checkpoint; a duplicate
does not call the helper again. Exact confirmation stops at Registration /
Confirmed. An explicit ledger advance can then prepare Provisioning / Accepted.

For an already-compatible registration, a writer transaction repeats the exact
proof and atomically advances to Provisioning / Accepted. No helper call,
dispatched child or dispatch timestamp is fabricated for this read-only reuse.

After uncertain local registration, explicit `confirm()` may only establish the
exact canonical DB goal by read-only verification. It does not repeat the helper.
An absent, partial, conflicting, revoked or mismatched registration remains
fenced. These local canonical rows are registration's authoritative evidence;
there is no invented lifecycle operation kind for registration.

## Transactional provision contract

1. Explicit `CreateKodiProvisioningService.provision()` persists the planned
   application provision UUID. Requests already dispatched/uncertain/confirmed
   do not invoke lifecycle provision again.
2. The existing lifecycle precondition checks the exact parent/runtime/pin,
   absent target container, positively stopped other inventory and existing
   session/retained rules under its writer transaction. Normal lifecycle
   reconciliation holds, mutations and reservations remain authoritative.
3. `on_claim` validates the running typed PROVISION child, original target,
   active lease/digest, pinned revision and owned projection revision. It commits
   parent Dispatched in that SAME transaction. There is no nested parent writer.
4. The existing broker provision operation resolves and admits the request
   through its existing capacity reservation and authorization. The broker's
   internal reservation UUID is not substituted for the application child UUID.
5. The lifecycle's optional `on_result` hook retains the verified reply before
   further status observation or lease release, in a fresh writer transaction
   after the claim transaction. It checks exact target/revision/image/container,
   fresh inventory, `created=True`, stopped state and current lease ownership.
   The parent stores the exact container and owned projection revision while
   remaining Dispatched. This is evidence retention, not success publication.
6. Existing lifecycle terminalization verifies its owned projection. Its
   `on_terminal` hook confirms the parent and successful child receipt in one
   transaction, or records failure/uncertainty. Existing lease release advances
   projection once; the parent stores that exact post-release revision. A failed
   callback rolls back receipt, release and parent confirmation together.

Other lifecycle callers omit the hooks and retain their behavior. No second
provision implementation, lifecycle kind, broker permission or session bypass is
added. Provisioning without a workspace/selection follows the existing retained
admission; no development selection or workspace is created or changed.

## Interruptions and limits

- Before claim commit: child, lease and parent dispatch roll back together. A
  preplanned UUID remains retained. This milestone does not add a provision retry
  or automatic reset after a refusal.
- After claim, before dispatch, or after a lost reply: preserve the original
  child and existing recovery evidence. Missing receipts/inventory do not permit
  another call or UUID. Inventory alone cannot establish provision success.
- After exact capture, before terminal commit: the parent retains the original
  container and owned revision, but does not report success. A terminal callback
  failure leaves the child/lease/reconciliation context intact under existing
  rules. A losing/rolled-back claimant cannot fence another lease token's owner.
- After terminal commit: refresh/replay returns the same parent and protected
  receipt, including after response loss or later independent lifecycle changes.
- Wrong replies, changed pins, expired/lost ownership or container replacement
  remain fenced. The stored original container is never rebound to replacement
  inventory. A pre-existing target container is deliberately refused in 2B;
  adopting it or replacing its profile is outside this adapter.

Parent references already protect provision receipts from normal pruning, even
after more than 20 later operations. No parent/child cleanup is added. Explicit
operator recovery uses existing receipt, reservation and reconciliation evidence;
this milestone adds no general provision uncertainty-resolution mechanism.

## Next checkpoint and failed installation recovery

Milestone 2C can consume Provisioning / Confirmed, explicitly advance the ledger,
and plan the existing `start_managed()` operation using the exact retained
container/runtime/image and post-release projection revision. Its authority must
still come from the existing scoped ordinary Start record/lease/proof. Native
readiness, screen availability and optional Bridge readiness must remain separate.
No such execution, API or UI is wired here.

For an eventual verified failed installation retry, retain the failed child and
require fresh explicit approval plus proven terminal worker/lock/reservation
state. Review an immutable old-to-new attempt relationship before assigning a
new child UUID. Do not reset the root, overwrite the failed reference, loosen
canonical uniqueness or use a new attempt to escape ambiguity. That mechanism
is deliberately not implemented in 2B.
