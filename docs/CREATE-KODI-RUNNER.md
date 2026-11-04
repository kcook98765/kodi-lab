# Bounded internal Create continuation (issue #196, milestone 3B1)

`CreateKodiRunner.continue_once(operation_id, version=...)` explicitly continues
one already-approved durable parent. Each invocation performs at most one ledger
transition or one existing adapter action, then reads the current parent. An
adapter retains its own transactional claim, result confirmation and bounded
native checks. There is no loop, scheduler, startup hook or UI. Milestone 3B2's
[protected continuation POST](CREATE-KODI-API.md) invokes it once; acceptance and
status APIs do not invoke the runner.

The runner receives the existing repository and five adapters through its
constructor. It does not create another approval, lifecycle implementation,
authority record or persistence table. The original single Create intent covers
the normal fixed sequence; existing adapters repeat authoritative admission.

## One invocation

| Durable boundary | Action |
| --- | --- |
| Accepted | Plan the same stage and persist its child identity; return before execution |
| Planned installation | Existing installation adapter; verified cached reuse or original planned child only |
| Planned registration | Existing exact canonical registration adapter |
| Planned provisioning | Existing provision adapter and transaction-local parent/child claim |
| Planned startup | Existing ordinary managed Start adapter, including its scoped one-use authority |
| Planned/dispatched readiness | Existing bounded read-only native inspection/recheck |
| Confirmed installation/registration | Advance once in the existing ledger |
| Confirmed provisioning | Existing Start adapter's exact provision validation and advance |
| Confirmed startup | Existing readiness adapter's original Start validation and advance |
| Dispatched installation | Inspect/resolve the exact original installation receipt; never launch |
| Dispatched registration | Confirm the exact local registration goal without bootstrap mutation |
| Dispatched provisioning/startup | Inspect original child ownership under a SQLite writer transaction; wait or fence |
| Complete | Return historical completion, without checking or starting Kodi |
| Needs attention | No lifecycle execution, reset, replacement UUID or implicit retry |

The existing narrow read-only confirmation methods may establish an original
uncertain installation result or registration goal from Needs attention. That
invocation only confirms evidence and returns; it never advances or executes the
next stage. Refused/failed operations remain inert. The runner never invokes the
installation adapter's explicit `resume_refused()` recovery command.

Native readiness may be explicitly rechecked at its existing Dispatched boundary.
An interrupted or temporarily unsuccessful observation does not rerun Start.
The fixed readiness correlation and original mutation child IDs remain unchanged.
Generic Confirmed-readiness ledger data cannot bypass native verification.

## Ownership and uncertainty

Installation dispatch still commits immediately before broker launch. Provision
and Start dispatch still share the existing typed child/lease claim transaction;
Start also commits its ordinary authorization there. The runner neither handles
opaque proofs nor expands their scope. Existing receipt retention applies.

The new repository command `inspect_lifecycle_dispatch()` checks the exact planned
child, kind, target, running receipt, runtime pin and unexpired matching lifecycle
lease under `BEGIN IMMEDIATE`. Startup additionally requires the original matching
ordinary authorization, lease digest and owned projection binding. Valid ownership
means wait until the original worker completes or ownership expires; it does not
prove the worker process is alive indefinitely. Inspection never renews a lease.

Absent, terminal, expired, mismatched or reconciliation-required evidence is
recorded as Needs attention / Uncertain. The original children, leases,
reservations and authorization evidence are retained. No inventory-based success
inference, cleanup or mutation replay occurs. Existing provision/Start success
confirmation commits atomically with the child terminal result; a successful
child without that joint parent confirmation is inconsistent evidence, not a new
recovery authority. Operator recovery for those uncertain outcomes remains
separate work.

## Concurrency and reconnect

Callers provide the durable parent version. Unknown parents and stale input
versions raise the existing typed transition error. A concurrent claim is fenced
by the existing SQLite version checks and child authority, across independent
processes. If a parent changes while an adapter is entered, a losing transition
returns the current durable winner without resetting its claim. Other existing
admission errors propagate; there is no automatic repair.

Reconstructing the runner and adapters after browser/application loss uses the
same parent and original child identities. Status reads remain passive. A later
manual Stop cannot alter historical completion or cause another Start.

## Focused verification and next boundary

Tests use isolated SQLite databases, the actual installation/registration/
provision/Start/readiness adapters, fake broker/native transports and independent
Python interpreters. They cover fresh/cache full progression, one action per
invocation, original receipts, local registration interruption, active ownership,
expired/mismatched evidence, lost mutation responses, read-only readiness recheck,
historical completion and concurrent original-child dispatch exclusion.

Milestone 3B2 supplies the authenticated administrator POST with existing
CSRF/origin protections, strict parent/version input, safe status serialization
and no-store response. GET, acceptance, reconnect and application startup remain
passive. Request timeouts must not invite redispatch of uncertain mutation
stages. A guided wizard and its wait/inspection pacing are still separate work.
