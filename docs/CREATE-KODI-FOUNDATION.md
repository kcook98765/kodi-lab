# Durable Create Kodi foundation (issue #196, milestone 1)

This document records the milestone 1 foundation. Milestone 2A adds only the
[installation adapter and narrow explicit recovery](CREATE-KODI-INSTALLATION.md),
with no schema or broker changes. Its two installation-specific recovery commands
are the bounded exceptions to the terminal Needs attention behavior below.
Milestone 2B adds [exact registration and provision hooks](CREATE-KODI-REGISTRATION-PROVISIONING.md),
including explicit confirmation of an uncertain local registration goal. It adds
no general retry, provision recovery, Start or readiness integration.
Milestone 2C adds [exact Start integration](CREATE-KODI-START.md) through existing
ordinary lifecycle authority; final readiness, Create API and orchestration remain
separate work.

This milestone stores approved intent and fixed checkpoints. It has **no runner,
HTTP endpoint, startup continuation, installation, registration, provisioning,
Start, readiness probe or Bridge integration**. The only execution driver and
child implementations are test doubles. Nothing is wired into application
startup or status polling.

## Record and admission

Migration `0092_create_kodi_operations` adds one table. Its immutable intent is
the parent UUID, request UUID, canonical target UUID, stable generation, packaged
reviewed recipe ID/revision/digest, `create_native_v1` scope and approval time.
`CreateKodiService.accept()` requires explicit approval and resolves a new
request's exact reviewed recipe and canonical target on the server. This records
intent; it grants no installation, lifecycle, broker or administrator authority.
The future authenticated mutation handler must establish administrator approval
before invoking it. No public route exists in this milestone.

Each command uses a fresh SQLite `BEGIN IMMEDIATE` transaction, acquiring the
writer lock before reading. Unique constraints on request UUID and canonical
target independently prevent competing records across application processes.

- Replaying the accepted request UUID with identical approved inputs returns
  the same parent, without altering its stage, timestamps, version or children.
  Replay uses the accepted recipe even after packaged recipes change.
- Reusing that UUID with different inputs is a request conflict.
- A different request UUID for the same canonical Kodi is a canonical conflict
  carrying the existing parent UUID. It is not accepted as an unrecorded alias.
- These constraints cover terminal history too. There is no second Create,
  replacement attempt or automatic retry for a failed/completed canonical root.

The record also stores a stage, checkpoint, compare-and-swap version, update and
current-stage dispatch timestamps, terminal outcome/time and bounded safe reason.
Runtime revision/image and container/projection revision are bound as confirmed
evidence becomes available. Profile paths, pins, credentials, proof tokens,
inventory snapshots and redundant child phase histories are not copied.

Installation, provision and Start UUIDs are allocated and committed at planning,
before dispatch. Planned UUIDs cannot have immediate FKs to children that do not
exist yet. Registration and readiness have no existing durable child operation
type; their local correlation keys are derived from parent UUID and fixed stage.
They are not new lifecycle operation kinds or broker capabilities.

Parent IDs survive target deletion; there is no FK to the live target/runtime.
Lifecycle pruning excludes non-NULL parent-referenced provision/Start UUIDs,
including completed parents. Retaining the Start receipt also retains its
ordinary Start authorization sidecar through the existing FK. No parent prune
or delete command is provided. Downgrade refuses to discard populated history.

## Fixed state transitions

Stages are installation -> registration -> provisioning -> startup -> native
readiness. Each stage uses the following closed transitions:

| Command | Required checkpoint | Committed result |
| --- | --- | --- |
| Accept | No existing request/target root | Installation / Accepted |
| Plan | Accepted | Planned; original child UUID persisted |
| Claim dispatch | Planned | Dispatched; dispatch timestamp recorded |
| Record exact successful result | Dispatched | Confirmed; exact binding retained |
| Advance | Confirmed | Next stage / Accepted, or Complete after readiness |
| Refuse before dispatch | Accepted or Planned | Needs attention / Refused |
| Record child refusal/failure/uncertainty | Dispatched | Needs attention, original IDs retained |
| Note missing/uncertain receipt | Dispatched | Needs attention / Uncertain |

All changes require the current version under the writer transaction. Stale or
competing commands fail without modifying the record. A dispatch command returns
a distinct `CreateDispatchClaim` only after its transaction commits. A status
snapshot is not a fresh claim. Losing that return value must not be repaired by
reissuing dispatch. This claim is ledger coordination, never runtime authority.

Result recording checks stage, child UUID, target and approved recipe digest.
Installation success requires exact revision/image. Registration retains that
binding. Provisioning adds the exact container and nonnegative projection
revision. Startup retains the container/pin/image and requires an advanced
projection revision. Readiness must retain that final identity exactly. The
future adapters must verify these values against authoritative child receipts
and current inventory; the ledger cannot turn submitted data into such proof.

Needs attention and Complete are terminal in milestone 1. There is no reset,
retry, replacement UUID, generic recovery override or uncertain-to-success
transition. A later manual Stop does not alter completed Create history and
cannot cause this ledger to restart Kodi.

## Interruption contract

- Before acceptance commits: no parent exists; the original request can be
  submitted again.
- Accepted or Planned: no dispatch claim exists; explicit continuation can plan
  or claim the same remaining stage without replacing prior child IDs.
- Dispatched, including a crash before the external call: execution is possible.
  Inspect the original child; never issue another call because its receipt is
  missing. A lost response follows the same rule.
- A matching confirmed child receipt can be recorded while the parent remains
  Dispatched, without re-execution. Missing/unverifiable evidence requires an
  explicit Needs attention command. Reads do not perform that command.
- Confirmed: advancing to the next fixed stage is safe. A failed next-stage
  transaction rolls back to the same confirmed boundary.
- Complete or Needs attention: status reads, request replay and test-driver
  reconnects cause no execution. An uncertain child cannot be replaced.

`get()` and `for_target()` perform SELECTs only, returning immutable snapshots.
They never plan, dispatch, inspect/reconcile children, reserve resources or mint
viewer tickets. Only a successful explicit command can produce a transition.

## Focused proof and next checkpoint

Tests use isolated SQLite databases, fake evidence and separate OS interpreters.
They exercise all five stages at acceptance, planning, dispatch-before-call,
response loss, confirmed result and next-stage transaction interruption. They
also cover failed acceptance commit, completed history after manual Stop,
reopen/replay, conflicting inputs, process races, stale versions, identity
mismatches, SELECT-only reads, SQL constraints, migration round trips and receipt
pruning beyond 20 records. No real child service is invoked.

Milestone 2 still needs the bounded dispatcher and authenticated accept/status/
explicit-resume API. Add narrow transaction-local parent hooks at existing child
claim/pre-dispatch boundaries; do not call this repository's independent writer
inside an already-held lifecycle writer transaction. Installation must accept the
preplanned UUID and expose its actual pre-RPC boundary. Exact verified reuse of
an existing installation must be bound **before** planning/dispatch; never swap
an already-dispatched child UUID. Provision must return exact verified identity
while ownership is held. Start must reuse `start_managed()` and its existing
authorization, not derive authority from this parent or claim. Result adapters
must validate typed receipts and revalidate current identity/admission before
each real mutation. Missing/uncertain evidence requires inspection/reconciliation
and an explicitly reviewed recovery action, not a fresh child UUID.

Native readiness, screen availability and optional Bridge/agent readiness remain
separate future observations. No additional schema, broker API, authority or
coordination framework is needed for this foundation. The eventual wizard and
prerequisite component are not part of milestone 1.
