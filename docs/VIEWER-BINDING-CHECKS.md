# Recurring viewer binding checks (#214)

Source baseline: `933c98efce5620776d9eb14b11344ec0e3308cdf`, schema
`0014_human_reset_bridge`. Source-only correction; native latency remains
unqualified. Related acceptance: [#305](https://github.com/kcook98765/kodi-lab/issues/305).

## Finding and attribution limit

An established interactive viewer repeats a full admission-style lookup before
each trusted Web heartbeat. This is a sequence of database queries, schema
inspection, sealed recipe/capability validation, private socket work and Docker
requests, not one cheap identity read. Separate socket/Docker timeouts do not
bound their combined elapsed time to the Web watchdog's 1.5-second budget.
Thread-pool scheduling, database read/lock waits, filesystem reads, process
scheduling and garbage collection can add elapsed time. Source inspection proves
the repeated work; it does not identify which component caused a native outlier.

The retained #305 report at
`kodi-lab-deployment/native-zip-305-acquisition-20261113/REPORT.md` records an
interactive disconnect after 66.526 seconds, watchdog phase `binding_check`,
maximum check 1,538 ms, write gap 1,789 ms and GC pause 138 ms. Broker termination
was `revoked`, with native exit 0/proven termination and maximum validation
586 ms. No original ZIP request occurred. The check maximum covers the watchdog
callback, not a timed catalog subphase; the last phase label and GC maximum do
not establish causality. The GC maximum is not proven to overlap that check.

Keep the historical observations distinct:

- Original Nexus first-viewer disconnect lacks initiating helper/close evidence.
- Later Nexus `binding_check` and Omega pre-pixel helper exit 1 remain unproven.
- PR #360 established input-batch scheduling and heartbeat freshness defects;
  it did not attribute every historical binding delay.
- The subsequent `ownership_renew` failure had a 1,442 ms durable commit.
  PR #361 removed recurring renewal writes. Its production acceptance passed
  300.291 seconds view-only, Take Control, navigation and Release Control.
- #305 reproduced a binding-check failure on post-#361 bytes in separate QA.
  That failure is not evidence that ownership renewal writes returned.

Earlier read-only probes and sparse database lock samples did not reproduce or
causally attribute the failures. A sampled absence of a writer does not exclude
a short competing write. No native session was run for this source task.

## Actual production path

`web/novnc.py:relay.watchdog.authorized` runs on a bounded worker before each
trusted heartbeat, with a 250 ms sleep after success:

1. Re-resolve current administrator authentication. Existing session-touch writes
   can still occur at their own interval; PR #361 removed viewer ownership writes.
2. `PrototypeViewer.binding`: interactive input exclusion, then the injected
   production authority from `web/novnc_optin.py:build_viewer`.
3. Web `capability_for`: managed revision plus admitted sealed display capability.
   Installed capability checks screenshot/navigation eligibility, current approval,
   artifact/profile/validation/attestation and wheel-owned recipe assets. It does
   not merely consult an in-memory list of enabled targets.
4. `resolve_binding`: new authenticated private Unix socket lookup. Broker
   `HumanDisplayService.resolve` repeats capability qualification, reads the
   provisioning specification, creates a Docker client (including API negotiation),
   lists the exact labelled container, inspects container and image, validates the
   provisioning contract/health and resolves the catalog binding.
5. Web repeats capability validation to fence concurrent changes, then runs its
   own `CatalogViewAuthority.resolve` against the broker-returned pin.
6. Foreground validity and fresh read-only ownership authorization against the
   exact attempt/owner/mode/binding/claimed broker/state/fixed deadline.
7. Only the awaiting coroutine, after its current/cancellation check, sends the
   private heartbeat. Browser input cannot manufacture that heartbeat.

Each catalog resolution reads the disposable instance and runtime reconciliation
projection, full outstanding-operation reconciliation, revision artifact and
execution profile, active development-session policy/session, selected workspace
generation/source/container, and mutation lease compatibility. Reconciliation
projects typed historical uncertainty across operation tables and checks orphaned
pending launch/execution ownership. Its empty path still performs schema
inspection PRAGMAs. This observer is constructed without a broker, so it does not
dispatch Docker work or perform recovery/mutation itself.

The private lookup uses existing handshake/capacity synchronization and a fresh
broker thread. Web's two read slots remain occupied until actual worker completion,
including after a caller timeout. There is no steady-state ownership writer lock,
update or commit. Ordinary reconciliation reads can still wait on unrelated
writers or database/pool I/O. Socket and Docker waits are additional work.

## Small correction and security contract

Previously `binding(..., "interactive")` first called
`require_control_available`, running **another** full operation reconciliation
before the broker and Web catalog checks. Now it calls `require_input_available`,
which still freshly reads mutation leases and remote guards. The two subsequent
catalog reconciliation checks are unchanged. The removed check preceded both
retained observations and never created or renewed authority.

Full `require_control_available` remains at Take Control before retiring the
old view-only attachment. Ownership creation also keeps full reconciliation and
its serialized admission. No positive authorization result is cached. This is
one removed duplicate, not a replacement of the catalog authority contract.

| Boundary | Required checks retained |
| --- | --- |
| Admission / Take Control | Full authority, pre-handoff unresolved-work/busy refusal, one-use ticket, exact claim, serialized creation, SQL conflict triggers, inherited absolute deadline |
| Established Web heartbeat | Current admin, lease/remote exclusion for interactive mode, full broker lookup and Web capability/catalog checks, foreground, exact active ownership/revocation |
| Before interactive native input | Independent broker ownership + capability + catalog reconciliation/development binding + exact Docker container/image/start/health; unchanged per-envelope check and FIFO scheduling |
| Helper renewal | Same independent broker validation at least every second; existing two-second Web and three-second helper expiry; no timeout change |
| Stop / Reset / replacement | Stop's lifecycle exception can acquire its lease, but recurring checks refuse it; lifecycle claim/revocation/stopped-container proof and Reset admission triggers remain. Replacement/start/catalog/development generation changes cannot inherit old authority |
| Revocation / stale attempt / worker loss | Fresh database readers, conditional exact-attempt writes, cancelled caller cannot emit late heartbeat, no fence removal before native termination/neutral-input proof |

`NOVNC_INPUT_TRIGGERS` exclude agent mutation/remote guards against **all** retained
interactive rows, including expired/revoking/blocked ones. The lifecycle exception
permits supported Stop to obtain its claim; it does not authorize ongoing human
input. Reset's separate triggers also prevent viewer admission during pending
Reset. These protections are verified by existing migrated-schema, concurrent
ownership, lifecycle and Reset tests. They do not cover every historical uncertain
operation record: the remaining catalog reconciliation checks are necessary.

Committed ownership revocation is independently observed before broker input.
Administrator revocation retains the existing Web watchdog propagation boundary.
Database reads and already authorized native bytes are not one atomic operation;
the existing bounded in-flight race is unchanged. No new revocation allowance,
ownership write, helper protocol, persistent state or migration is introduced.

## Call counts, diagnostics and disposition

The deterministic production-Web-path comparison substitutes the exact old
preflight while keeping the rest of the new path and database unchanged. Both
catalog validations are real; sealed capability/Docker acquisition are doubles:

| Per interactive binding lookup | Before | After |
| --- | ---: | ---: |
| Full operation reconciliations | 3 | 2 |
| SELECT statements in this fixture | 100 | 78 |
| Schema PRAGMAs in this fixture | 9 | 6 |
| Web capability calls | 2 | 2 |
| Private broker lookups | 1 | 1 |
| Steady-state ownership writes/commits | 0 | 0 |

The 22 SELECT / 3 PRAGMA reduction describes the empty-operation fixture.
Actual production totals include real capability, provisioning and Docker work,
and depend on selected-session/history state. These counts do not establish a
native millisecond improvement or prove the 1.5-second failures eliminated.

[Slow-check diagnostics](VIEWER-DIAGNOSTICS.md) now separate worker queue delay,
worker elapsed/thread CPU, and fixed subphases across Web and broker. They log
only checks of at least 250 ms using the existing private bounded sink, complete
after late workers finish, and never log identities, SQL values or payloads.
`task_completed` means the callback returned, not that authorization succeeded.
Elapsed minus CPU cannot distinguish storage, locks and scheduling. Nested phases
overlap, and timestamp-correlated broker lookups are not exact request tracing.

The safe correction is limited to this earlier duplicate. Moving all reconciliation,
sealed capability validation or Docker checks to admission would require a broader
invalidation contract covering every writer, historical orphan/uncertainty transition,
runtime incarnation and process restart. SQL exclusion triggers alone do not prove
that contract. No such speculative simplification is implemented.

Independent source/security review should precede merge. If the owner later
authorizes deployment and diagnosis, at most one bounded representative native
viewing/control session with these timings can determine whether a slow subphase
recurs. No new QA installation or ZIP delivery is needed for that diagnostic.
Keep #214 and #305 open; do not repeat native ZIP acceptance before reviewed
disposition of this issue.

## Targeted validation

| Required regression | Deterministic evidence |
| --- | --- |
| Ordinary interactive heartbeat | `test_novnc_live_heartbeat.py`: real relay, 1,200 ticks in each mode, zero routine ownership writes/commits |
| Runtime replacement | `test_novnc_authority.py`, `test_same_instance_novnc.py`, `test_novnc.py`: revision, image, epoch, selection and replacement handoff refusal |
| Explicit revocation / stale attempts | `test_novnc_ownership_race.py`, `test_novnc_control.py`: independent readers, delayed old checks and exact successor protection |
| Managed lifecycle / Reset | `test_managed_lifecycle_claims.py`, `test_managed_clean_reset.py`: lifecycle claim/Stop and Reset worker/binding fencing |
| Agent/human exclusion | `test_novnc_control.py`, `test_managed_human_input_priority.py`, `test_novnc_control_migration.py`: concurrent claims, remote guards and retained expired/blocked ownership |
| Pending / uncertain operations | `test_same_instance_novnc.py`: real launch/smoke coordinator records, missing lease, ambiguity and containment; recurring interactive path also checked |
| Slow individual subphases | `test_novnc_binding_timing.py`: controlled queue/elapsed/thread CPU timing, successful and exceptional phase completion, privacy and logger failure |
| Web timeout / late worker | `test_novnc_diagnostics.py`, `test_novnc_binding_timing.py`: real 1.5-second watchdog timeout, late complete summary after terminal event, retained real-worker slot |
| Interrupted check cannot grant | Same timeout tests assert no trusted heartbeat after timeout/late success; stale-attempt tests retain independent broker revocation |
| PR #360 / #361 | Broker input scheduling, buffered-heartbeat freshness, transport, lookup capacity/state and Web ownership/live-heartbeat tests |

Results and exact commands are recorded with the draft PR. No full suite,
browser/native acceptance, runtime acquisition or production mutation was run.
