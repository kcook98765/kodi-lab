# Durable viewer ownership and live heartbeats

Source baseline: `425cfbb2f4f7fb6c6abaa44c0ddea4bb726b5cb4` (PR #360).
This correction addresses recurring ownership commits in [#214](https://github.com/kcook98765/kodi-lab/issues/214).
The schema remains `0014_human_reset_bridge`. Source validation is separate from
native acceptance; #214's original first-viewer and helper-startup failures remain
unresolved. Historical qualification: [sustained viewing](VIEWER-PIERS-QUALIFICATION.md)
and [broker scheduling and heartbeat freshness](VIEWER-INPUT-SCHEDULING.md).

## Ownership and liveness

Previously, every Web watchdog tick conditionally updated `expires_at` to two
seconds ahead and committed SQLite before sending a trusted heartbeat. At four
ticks per second, unchanged viewing could require approximately 1,200 durable
renewals in five minutes. Slow synchronization could exhaust the watchdog budget.

Now `NovncSessionRow` is the durable worker fence. It identifies one instance,
viewer attempt, administrator owner hash, mode, exact container/image/start and
catalog/development-session binding, claiming broker incarnation and native exec.
`expires_at` bounds an unclaimed admission to ten seconds. Broker claim atomically
sets it to the existing `absolute_expires_at`; neither field changes on a live
heartbeat. Handoffs inherit the original absolute deadline (at most 600 seconds).
The broker's `valid()` observes durable authority, not connection liveness.

The Web watchdog retains its 250 ms cadence and 1.5 s check bound. Before emitting
a private heartbeat it checks current administrator authentication, catalog/runtime
binding, the interactive foreground lease, and read-only `authorized()` against
exact instance/attempt/owner/binding/mode, claimed broker, active state and fixed
expiry. Each database check opens a fresh session. There is no authorization cache
or additional lease service. Browser traffic cannot synthesize this private
heartbeat; its timing is never taken from browser-provided timestamps.

The broker binds the private stream to its exact claimed request. It uses
process-local monotonic first-byte receipt times and its unchanged two-second Web
lease, with one envelope per selector turn. Input, old buffered heartbeats,
unrelated later reads and a heartbeat's late suffix cannot refresh that deadline.
Its independent authorization callback checks the exact broker/attempt and current
catalog/runtime/container identity at least every second and before every
interactive input envelope. Only that callback permits native helper renewals.
The helper independently expires after three seconds without broker renewal and
has its own 600-second maximum. Interactive browser foreground pulses retain their
separate three-second, non-revivable lease. View Only needs no foreground pulse.

## Persistent transitions and complete operation paths

| Operation | Persistent action and live behavior |
| --- | --- |
| **Start Viewing / View Only** | Authenticate and verify Origin/CSRF, one-use ticket and server-resolved runtime. Commit `pending`, then broker conditionally claims the exact row as `active`, fixing expiry to the original absolute deadline. Commit exec identity before helper start/arming. Live streaming uses read-only checks and connection heartbeats. |
| **Take Control** | Advisory busy checks preserve the old view when an agent operation already owns input. Stop the old attempt, prove native termination, remove its fence, then commit a fresh interactive attempt with the inherited deadline. SQL triggers arbitrate concurrent agent/human admission. No in-place mode upgrade or automatic reacquisition. |
| **Keyboard / mouse** | No ownership write. Web checks cancellation, foreground liveness and input bounds; the broker freshly checks authority for every interactive envelope. The sealed helper enforces its admitted mode. |
| **Agent commands** | Existing remote guards and mutation leases arbitrate atomically against every interactive row, including expired, revoking or blocked rows. View Only retains worker identity/recovery state but does not itself exclude authorized remote input or policy-compatible agent work. Uncertain work still blocks viewing. |
| **Release Control** | Cancel and durably revoke the old interactive attempt. Native worker death plus the fixed neutral-key/button probe must succeed before deleting the fence and committing a view-only successor. Agent input may resume only after verified deletion; denied commands are never queued or replayed. |
| **Stop Viewing / browser disconnect** | Close live transport and revoke the exact attempt; unclaimed rows can be deleted. Claimed rows become `revoking`, then are deleted only after broker proof. Unproven termination becomes `blocked`, retaining capacity and interactive exclusion. |
| **Authorization revocation / logout** | Logout cancels the owner's local attachments and durably revokes them. Current authentication is also checked on every Web watchdog tick. Committed row revocation reaches separate broker readers at their next validation, including before interactive input. Any failed check stops heartbeat production. |
| **Web crash / restart** | No process-local attachment or one-use ticket survives. Socket loss or the independent broker deadline stops live permission without waiting for a cleanup write. The durable row survives until native proof; a restarted Web cannot overwrite it. |
| **Broker crash / restart** | No heartbeat state survives. The independent helper deadline stops its worker. Startup recovery examines the stored old container/exec/mode, never a replacement container. A false/failed proof retains `blocked`; a successful proof alone permits retirement. Native flock ownership survives helper loss while its child remains. |
| **Native helper termination / recovery** | EOF or Docker attach loss alone is insufficient. Existing exec inspection, exact-container lock/process probe and interactive neutral-input proof govern retirement. The broker retains an unconfirmed completion and retries proof/finalization through its existing reaper. |
| **Managed Kodi Stop** | Existing lifecycle claim excludes successor admission. Revoke live viewing, retain the worker fence, and retire only on the exact broker-confirmed stopped-container receipt. A wrong container cannot clear it. |
| **Reset / replacement / binding change** | Existing Reset triggers, lifecycle guards, reconciliation checks and catalog/selection identity checks remain. Pending Reset refuses new viewers; replacement cannot inherit old authority. Changed runtime, start incarnation or development generation ends the old stream. |
| **Expired / stale records** | Only expired **unclaimed** admissions are swept automatically. Active expiry denies further authority but never frees an interactive worker fence. Both ordinary and privileged reconciliation avoid a write transaction when no expired pending row exists. |

The one-row instance primary key and unique attempt prevent successor admission
while any old worker fence remains. Unchanged SQL triggers exclude human control
against existing remote guards/mutation leases, and exclude those agent writers
against interactive ownership. These triggers do not depend on a heartbeat,
process-local registry, state being `active`, or unexpired timestamps. Human
priority means subsequent conflicting commands are refused while control is
reserved; an already executing agent command must finish before Take Control.
Production/External admission and grants remain unchanged.

## Cross-process and slow-storage behavior

Web, broker and agent/MCP may run in separate processes. Durable exclusion uses
SQLite, not Web memory. The root broker still reads the database directly and
uses its closed one-shot state-UID writer for claim/start/finish transitions;
its raw SQL claim and the ORM claim have identical fixed-expiry semantics.
Revocation travels through durable row state plus live stream cancellation/loss,
not an assumed shared Python object. Lost or delayed heartbeat permission and
persistent successor exclusion are independent.

Transition calls acknowledge success only after their transaction completes.
The broker does not admit a tunnel before claim commits or arm a helper before
exec identity persistence and fresh authority validation. Slow/failed/unconfirmed
transitions retain existing refusal/cleanup/fence behavior. Release success
requires termination proof and durable fence removal; a timeout cannot report a
successful release. An already established stream performs no renewal commit to
wait for. This does not guarantee immunity to unrelated SQLite writers holding
exclusive locks, slow authorization reads, Docker lookups, scheduler stalls or
network/backpressure failures: existing bounds still fail closed.

A read can race a later revocation. A delayed Web check might emit one obsolete
heartbeat, but it cannot modify any row or authorize input through the broker's
fresh check. Native execution and database validation are not one atomic
transaction; previously authorized in-flight bytes retain the existing bounded
transport race. The correction adds no queued-input replay or revocation latency
allowance. Failed reads and uncertain worker death remain restrictive.

No migration or stored-record rewrite is required. Old active rows retain their
old expiry and fence; a new check cannot revive them. Any later deployment should
stop/restart the Web and broker together and reconcile workers through supported
recovery. A mixed old/new deployment is not qualified: an old claim can leave the
short admission expiry, and old Web code still performs durable renewal writes.

## Focused evidence and write frequency

`test_novnc_live_heartbeat.py` drives the real Web relay/watchdog through 1,200
250 ms ticks for **each mode**, with controlled viewer/wall clocks, real SQLite
sessions and an independent broker ownership reader. It asserts 1,200 accepted
heartbeats, unchanged stored ownership/exec/deadlines, and **zero write statements
or commits throughout steady state**, including pending-admission reconciliation.
SQLite's real commit function is delayed 1.5 seconds throughout the test; it is
reached only for final revocation after live transport closes. That transition
still commits and leaves the fence until explicit proof. These are five minutes
of deterministic clock progression, not five minutes of native/browser acceptance.
The delay models commit latency, not the physical disk's synchronization internals.

Expected reduction: approximately **1,200 to zero routine ownership renewal
commits per five-minute session**, for View Only and Take Control. Admission,
claim, exec registration, revocation and proven completion still commit; handoffs,
recovery and unrelated application operations can add writes. This is not a
claim of zero total database writes.

| Required regression | Evidence |
| --- | --- |
| 1–2, 12: both sustained modes; slow commit | `tests/web/test_novnc_live_heartbeat.py` |
| 3–4: authorized/missing heartbeats; fixed expiry | Live-heartbeat test; existing broker buffered-heartbeat/transport tests; foreground handoff tests; ownership expiry checks |
| 5–6: revocation and replaced attempts | Real sockets plus independent database readers in `test_novnc_ownership_race.py`; stale same/different-owner checks; broker input scheduling revocation |
| 7–8: agent exclusion and proven release | Existing `test_novnc_control.py`, `test_novnc_tab_handoff.py`, `test_managed_human_input_priority.py`; migrated-schema arbitration |
| 9–10: Web loss, broker restart, expired controller | Real socket Web-loss check, blocked recovery with exact old exec proof, expired-fence concurrent-admission rejection |
| 11: Stop/Reset/runtime identity | Existing Stop recovery, managed lifecycle claims, clean Reset, catalog authority and same-instance selected-session tests |
| PR #360 preservation | Unmodified broker FIFO/fairness, cancellation, authorization, first-byte heartbeat freshness, fragmentation, backlog and lookup-capacity regressions |

**INTEGRATION PASS:** 39 focused cases (each changed test file run individually),
then 175 additional integration cases passed; one opt-in Chromium case skipped.
Ruff format/lint and strict mypy passed on all ten changed Python paths. The
integration lane completed in 140 seconds. It preserved PR #360's regressions
and exercised all rows in the evidence table. The exact command selection is
recorded with the PR. The complete regression suite and native acceptance are
outside this source task.
After review and separately authorized deployment, the source evidence can justify
one bounded representative native viewing/control test with appropriate retained
profile preservation and exact-version reporting. It cannot close #214 or prove
that independent binding-check/helper-startup failures have been corrected.
