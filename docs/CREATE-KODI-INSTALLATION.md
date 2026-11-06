# Create Kodi installation integration (issue #196, milestone 2A)

This is an explicitly invoked application adapter, not the complete coordinator.
There is no runner, startup hook, Web route, wizard or provisioning/Start/readiness
integration. It reuses `RuntimeInstallationService` and the existing reviewed
worker/broker contract. The database schema and broker authority are unchanged.

## Installation boundary

`CreateKodiInstallationService.install(parent_id, version=...)` checks the stored
approved recipe/canonical target against the packaged selection. For a new
installation it persists the planned child UUID before invoking the installer.
The installer's optional `operation_id` and `before_dispatch` arguments form a
paired, stricter path: reviewed generations (Piers requires explicit preview consent), ordinary install only, no
implicit retry/reacquisition, no joining unrelated work, and no reconciliation.
Without these arguments, existing installer callers retain their behavior.

The callback commits the parent's version-checked Dispatched transition. The
very next broker call launches that exact UUID. A failed callback prevents launch.
A committed callback followed by a crash, missing receipt, invalid reply or lost
response never permits another launch. Requests with a stored dispatch checkpoint
only inspect/record that original child's evidence; they do not call Start again.
Competing processes still depend on the database CAS, not an in-memory lock.

`observe_create()` uses read-only receipt, eligibility, image, exact worker and
acquisition-lock observations. It does not call the stateful `_view()` or
`_reconcile()` paths. An exact running worker is Active. A nonterminal receipt
with a gone/exited worker and free lock is Interrupted, not automatically Failed.
Mismatch, missing evidence or unavailable observations cannot prove success or
safe failure. Pending receipts for other generations also prevent a new launch.

Normal receipt confirmation stops at Installation / Confirmed. No subsequent
stage executes. A worker may perform its existing canonical registration as part
of the existing installer; the adapter adds no registration operation of its own.

## Verified cached runtime

Reuse requires the exact approved recipe ID/revision/digest, a Ready receipt,
current runtime eligibility (including sealed provenance/approval/validation),
the exact broker-verified image and a compatible existing canonical pin, if any.
The same proof is repeated while the parent holds the database writer lock.
Changed/unavailable proof rolls back the parent transition. No worker launches,
download, registration helper or pin/profile mutation is requested for reuse.

Reuse atomically binds the verified runtime/image and advances to Registration /
Accepted, where this adapter stops. It never fabricates Dispatched or a dispatch
timestamp merely to pass the milestone 1 confirmation constraint.

If no child was planned, the verified source receipt UUID is explicitly linked as
`installation_operation_id`. If a child UUID was already planned, it remains
unchanged and unused: it must have no existing receipt and no parent dispatch.
The cached source is correlated by the parent's immutable approved recipe and
exact runtime revision/image to existing Ready installation records. Its UUID is
not substituted for the planned child, and the planned child is never described
as having executed successfully. Later readers must validate the full binding,
not select an arbitrary latest installation. This derives evidence already held
by the authoritative runtime catalog without another parent column/table.

## Narrow explicit recovery

| Situation | Supported resolution in 2A |
| --- | --- |
| Refused before dispatch | Explicit `resume_refused()` verifies no child receipt/worker, retains the planned UUID, revalidates current admission and continues under the original approved scope. Changed packaged recipe remains refused. |
| Original exact worker still active | Observe the same UUID. No second launch and no change of child identity. |
| Original outcome uncertain | Remain fenced. Explicit `resolve()` can consume only that original receipt after its exact recipe, terminal state, worker/lock and eligibility/image evidence are verified. No receipt means no resolution. |
| Verified original failure | Record Failed and retain the original UUID. It remains final in 2A. There is no new attempt, automatic retry or reset. |
| Fully verified installed runtime before any dispatch | Atomic reuse as above, including after an explicit pre-dispatch refusal. |
| Unrelated installation becomes Ready after ambiguous dispatch | It cannot resolve the original missing child or authorize reuse. Preserve uncertainty. |

Only installation uncertainty can be resolved from Needs attention; development,
provisioning, startup and readiness states retain milestone 1's strict behavior.
Completed history cannot restart Kodi. Resolution is an explicit ledger command,
not a status GET/poll side effect. Read-only parent lookups remain unchanged.

Supporting a *new installation attempt* after a verified failure is deferred.
It needs a separately reviewed durable relationship preserving the failed child
and fresh owner approval; overwriting the single planned UUID or bypassing the
canonical root uniqueness is not acceptable. No attempt-history mechanism is
added here.

## Limits and next checkpoint

The DB-to-broker gap is conservatively uncertain. A worker can also find a cached
runtime after launch and exit without a receipt for the requested UUID under the
existing CLI behavior; 2A intentionally leaves that case fenced rather than
adopting another receipt. Timer expiry, image presence alone, an exited worker
or an unrelated successful operation cannot close it. Establishing that exact
worker's outcome requires a separately supported inspection/result mechanism.

Future milestones still need registration/provision result adapters, existing
`start_managed()` integration, native/viewing/optional Bridge observations,
authenticated accept/status/explicit-resume routes and bounded execution. Every
actual child operation must repeat its existing authoritative admission checks.
No profiles, pins, selections, credentials, grants or viewer policy change here.

Focused tests use isolated databases, sealed fake runtime evidence and fake
workers. They cover the dispatch/crash gap, lost/mismatched responses, cache
eligibility/identity drift, original-UUID resolution, explicit refusal recovery,
failure finality, competing runners and SELECT-only observations. No live Kodi
or runtime acquisition is used.
