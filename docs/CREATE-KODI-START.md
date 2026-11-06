# Exact Create managed Start (issue #196, milestone 2C)

This adapter performs one explicitly requested Start. It does not implement a
runner, Web Create API, wizard, final native readiness stage or Create completion.
It reuses `start_managed()` and the existing ordinary Start authority, leases,
broker capacity admission and native-only background readiness behavior.
No schema, broker contract, permissions, Bridge or viewer controls change.

## Explicit preparation and claim

`CreateKodiStartService.advance()` verifies Provisioning / Confirmed against the
protected original PROVISION receipt, reviewed runtime, exact registration,
stopped container and stored post-provision projection under a writer transaction.
It advances only to Startup / Accepted; it performs no Start. The adapter then
persists a planned Start UUID before invoking lifecycle. Existing evidence for
that UUID is never replayed, including terminal, failed or uncertain receipts.

At Start claim, the adapter supplies the exact `ManagedStartBinding` (target,
runtime revision, image, container and stopped state) and a transactional
precondition. It repeats the original provision/runtime/registration proof and
requires the current projection to equal the saved post-provision revision.
This prevents a delayed page or an A-to-B-to-A context from borrowing current
identity just because Kodi happens to be stopped again.

The existing `start_managed()` issuer creates its one-use opaque proof and scoped
authorization. A narrow optional `on_claim` callback runs after that authorization
is issued, within the same writer transaction, before the coordinator reserves
and claims the matching lease. The parent verifies the exact running typed START
child, target/container/pin/image, lease digest and predicted owned projection,
then checkpoints Dispatched. The existing coordinator verifies authority against
the claimed lease before committing everything together. A callback/admission
failure rolls back parent dispatch, child, authorization and lease together.

No raw proof is passed to the parent callbacks or stored in the parent. Proof
generation, digesting, transport and atomic one-use consumption remain in the
existing lifecycle/broker implementation. The parent grants no broker authority.
An in-flight lease blocks ordinary selection/pin mutations; the broker repeats
exact inventory/authority/revision/lease checks at its atomic reservation boundary.

The broker enforces the administrator's running limit for both retained and
ordinary managed instances. Start does not modify that policy or select a
different development workspace. Known running instances may coexist up to the
configured limit; unknown starts retain their reservations. Existing target
identity, reconciliation, grant, hold and input ownership restrictions remain.
A pre-existing running container cannot
substitute for the planned child: expected state is stopped, and a no-action
Start reply is not accepted as this operation's successful mutation.

### First-install resource policy

New Linux and Windows/WSL2 installations both run the same locked host
initialization command. Packaged initialization applies the current default of
one running managed Kodi only if the database had no tables at entry. The local
host `kodi-lab-admin migrate` command uses the same initialization and lifecycle
lock. Passive reads and first-use row insertion share that default; application
capacity observations and broker admission read the same recorded policy.

Historical migrations retain their original seed of four. An existing versioned
database, upgrade, repeated initialization or partially initialized database keeps
its recorded running limit and idle settings. No migration automatically converts administrator
resource policy. Initialization interrupted after schema creation requires
inspection; a later invocation cannot infer that the recorded policy is disposable.

The prerequisite component displays the current limit and running usage. A higher
limit is supported. Running capacity is required only for an explicitly requested
Start, and the broker checks it again atomically before dispatch. Installed and
stopped profiles use no running slot, including in retained mode.

## Exact reply and joint confirmation

The lifecycle's optional result callback receives the actual broker reply before
status observation or lease release. The adapter requires matching target and
container, running state, `start_performed=True`, exact fresh runtime/image
inventory, no other active/unknown Kodi, the running child, active unexpired lease
and the matching consumed ordinary authorization.

No additional result column is necessary. The parent keeps its last confirmed
pre-Start projection at claim. Only verified reply capture changes that value
to the authorization's owned projection (exactly prior + 1). That durable change
records result observation. Proof consumption or a running inventory alone
cannot produce it. The authorization retains the immutable owned projection.

At the existing lifecycle terminal transaction, parent success requires the
original successful START receipt, consumed matching authorization, captured
owned revision and exact post-release projection (owned + 1), plus matching
current inventory. Parent confirmation and child success commit together. Failure
rolls back terminal receipt, lease release and parent confirmation together.
The parent retains the precise post-release identity for later readiness checks.

The adapter stops at Startup / Confirmed. This means the exact Start succeeded;
it does not mean native usability, viewing availability or optional agent/Bridge
capability has been confirmed. Existing native-only background readiness remains
unchanged. No Bridge preparation is added.

## Interruption and replay

- Before claim commit: no accepted Start authority or dispatch exists. A planned
  UUID remains retained. This milestone adds no refusal reset/retry mechanism.
- After claim, before broker dispatch: preserve the original child/authorization
  and lease. Never call Start again or reconstruct/regenerate proof for it.
- Lost/invalid reply or missing receipt: remain fenced, even if inventory is
  running or the original proof was never consumed. No replacement child UUID.
- After verified capture, before terminal commit: retain the exact binding and
  original recovery evidence; captured reply is not terminal success.
- Wrong/expired ownership or projection: fail closed. A losing/rolled-back
  claimant cannot mark a different token owner's operation uncertain.
- After terminal commit, including response loss or a later manual Stop: replay
  returns the same historical Start result without another Start or readiness
  attempt. Parent references protect the START receipt and its authorization
  sidecar from ordinary pruning.

Unknown/failed operations require existing inspection/reconciliation. This
milestone adds no uncertainty-resolution engine, new attempts, implicit Stop,
reset, reprovision, repin, profile replacement or stale-container adoption.
Read-only parent lookups never call the adapter or run a stage.

## Later integration

A later milestone must explicitly advance to readiness and establish native
usability from fresh, identity-fenced observations, including recovery from lost
process-local readiness evidence. Screen availability remains separate; opening
the screen must still use PR #197's explicit view-only exact-instance flow.
Optional Bridge/agent readiness cannot authorize native success or trigger silent
Bridge preparation. Authenticated accept/status/explicit-continuation APIs and a
bounded coordinator/wizard remain separate work, with passive polling unable to
drive execution. No such work is included here.
