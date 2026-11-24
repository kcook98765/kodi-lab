# Reviewed legacy snapshot recovery

This is an operation-specific administrator recovery for a source-bound legacy
restore that verifiably retired its original container and received a definite
before-create provisioning refusal. It creates a **new stopped successor** and
a new receipt. The failed restore remains failed and immutable.

Implementation, isolated native qualification, application installation and an
exact production recovery are separate approvals. Synthetic tests do not establish
recovery of a particular production profile or its native usability.

## Eligibility and retained evidence

The offline reviewer supplies a bounded `LegacyCaptureReview` packet. It contains
complete retained broker/Web request coverage, original receipt and selected row,
one-shot restore intent/accepted/terminal observations, pre-operation non-overlap,
native-stop identity proof, independently reviewed snapshot identity and retained
source/workspace/policy attribution. Raw captures stay independently retained;
SQLite keeps their hashes, normalized proof and the explicit reviewed causal link.

Historical session-admission and operation-linked dispatch records may genuinely
be absent. They are recorded as `not_recorded`, never backfilled or inferred from
current metadata. The supported historical code semantics must establish that the
exact refusal occurred before provider entry. A null successor, phase, age, free
lease or absent container is insufficient.

The broker audit omits restore UUID and full removed CID. The reviewer must join
its exact removal/refusal request IDs to the incident using complete ordered
primary captures, independently recorded full-CID observations and installed code
semantics. Peer PID zero is unavailable namespace identity, not worker proof.
The Web capture may contain exact `/login` 303 authentication records; unrelated
mutating POSTs or a second restore are refused. Native stop can be either the
existing managed-version receipt or the actual Web lifecycle response plus an
independent stopped full-CID/profile observation. Neither is converted into a
fictional historical receipt of the other kind.

Attribution is checked **before the first preview**. Complete retained source,
workspace and policy rows anchor locators, baseline UUIDs/digests, generation,
content identity and policy. For independently observed rows proven equal to an
older complete-table commitment, the offline validator reproduces the original
collector's ID ordering and `json.dumps(rows, sort_keys=True, default=str)` SHA-256
for every row/count before deriving selected-row fingerprints. This is an explicit
new equality proof against a retained commitment, not invented old raw data.
Missing or contradictory attribution refuses; current values are never adopted
as the original baseline. Whole-row fingerprints use the recovery canonical JSON;
SQLite policy booleans normalize to boolean values.

The reviewer also fixes original snapshot UUID, manifest/archive hashes and
lengths, member count and original runtime image. Preview independently validates
the artifacts and reads only bounded selected-skin/version/profile metadata from
those exact bytes. Unknown metadata is labelled honestly. Current workspace
version does not establish the snapshot's skin version.

## Supported offline boundary

Use the installed `kodi-lab-snapshot-recovery-maintenance APPROVED_PLAN` only in
the separately approved daemon-host context. The plan identifies the exact
installation, Engine, named state volume, two full application service CIDs,
candidate package digest and reviewed evidence. It is privileged local
infrastructure input, never a Web/MCP request.

The helper:

1. Proves the actual responding daemon's filesystem/PID/mount/root context from
   the exact SDK Unix endpoint before either CLI mode classifies consumers.
   Verifies the SDK Engine ID, exact Compose project/service identities, shared
   named state-volume creation identity, and each daemon PID's start/cgroup
   membership in its full approved CID. A remote/wrong PID namespace refuses.
2. Validates the complete broker capture before stopping either service. Only
    structured completed audits and narrowly recognized INFO Alembic setup or
    supported deployment measurements are accepted. The two additional legacy
    classifications below require independently verified installed-code policy.
    Diagnostics remain diagnostics, never substitute audit completions.
    Unknown/error/unconfirmed records refuse.
3. Exhaustively discovers state-volume consumers across all Engine containers,
   including read-only mounts, backing-path ancestors/descendants, physically
   resolved bind aliases and local bind-volume aliases. Every ancillary consumer
   requires explicit offline review of its exact full CID and metadata digest,
   plus independently derived stable, preincident physical inactivity proof.
4. Uses that **same SDK client** to stop only the approved Web CID, then broker
    CID, with infinite graceful timeout. No ambient Docker CLI/context, forced
    kill, native managed-container control, extraction or cleanup is performed.
5. Confirms clean exit, unchanged restart identity, PID zero and complete
    exhaustion/removal of the previously verified cgroups. It checks final logs
    and both stopped states again, and repeats ancillary-consumer proof before
    each stop and before sealing. Listener absence alone proves nothing.

### Required daemon-host context and supported topology

Both drain and `--inspect-consumers` require the actual local Linux daemon-host
filesystem root, PID and mount namespaces, plus the existing independently
verified service/cgroup context. Sharing only PID/cgroup visibility or mounting a
Docker socket into another root/mount view is insufficient. A nested daemon's
socket reached from its outer host must refuse; run the qualified installed helper
in that daemon's actual host context. This does not grant a new namespace-entry,
proxy, permission or remote-execution facility.

The entrypoint derives the endpoint from the SDK's actual `UnixHTTPAdapter`,
checks expected Engine/socket binding, and sends only a bounded read-only
`GET /_ping`. Linux `SO_PASSCRED`/`SCM_CREDENTIALS` identifies the response writer.
`SO_PEERCRED` alone is insufficient: a socket-activated listener can still identify
systemd's creator PID rather than dockerd. The actual response sender must be the
supported direct dockerd running as root, with stable process-start identity and
the same PID/mount namespaces and root device/inode as the helper. Bounded
mount-table digests also match and remain stable. Missing/ambiguous/truncated
credentials, unsupported endpoints/proxies and identity/view drift refuse. The
context is rechecked before CLI output; drain seals retain the derived receipt.

The supported state topology is the exact local named volume and reviewed direct,
lexical ancestor/descendant, symlink-resolved and local bind-volume mappings.
`Path.resolve` is **not a general kernel bind-alias mapper**. Independently verify
that the daemon-host topology has no additional nonlexical kernel bind aliases
of state before approving this procedure; such aliases are unsupported. A matching
namespace/root or a mount-table digest alone does not establish their absence.
Physical topology or out-of-band mount changes invalidate that approval and
require renewed independent review; do not automatically adopt a new topology.
The guard proves the correct stable view, not immunity to arbitrary privileged
host changes throughout a maintenance epoch.

### Retained ancillary consumers

`kodi-lab-snapshot-recovery-maintenance APPROVED_PLAN --inspect-consumers` is a
read-only inventory of bounded IDs/digests for independent administrator review;
it neither adopts the inventory nor produces a drain seal. The approved plan's
`ancillary_consumers` lists exact reviewed full CIDs and metadata digests (at most
32). Read-only mounts receive the same checks as writable mounts: an old broker
could retain a dispatch channel while reading authority metadata.

For each reviewed ancillary container, the helper requires exited/PID-zero,
nonrestarting state, restart policy `no`, unchanged metadata and `FinishedAt`
before the original incident. The independently verified live pair must establish
one common full-CID cgroup pattern and inspected cgroup parent. Only then may the
helper derive the ancillary full-CID path and verify that exact kernel cgroup is
empty or absent. A historical nonzero exit remains a historical failure; it is
not evidence of successful work or a recovered outcome.

An unresolvable bind source is conservatively a **potential state consumer**.
Its sealed alias value is JSON `null`, not a fabricated resolved path. It needs
the same exact-CID review and complete physical inactivity proof. Unknown/live,
unreviewed, changed or restarted consumers refuse. A new CID reusing the same
unresolved source also refuses. Missing paths need not be recreated and retained
containers must not be deleted to manufacture eligibility.

The normalized seal preserves exhaustive consumer identity, derived inactivity
proof and alias classification. Later broker checks match that frozen proof;
they never adopt a new baseline. The two main retired service CIDs may remain
unchanged and stopped or be absent after coordinated recreation. Ancillary
consumers must remain retained and unchanged. Live attestation binds the consumer
proof digest before preview, claim/handoff and execution.

### Exact legacy diagnostic classifications

The helper obtains applicability through read-only SDK metadata for the exact
broker: fixed entrypoint, immutable image identity, dedicated UID/GID, passwd/group
identity and exact supported broker-source hashes. A caller flag or supplied
boolean cannot enable these classifications.

- A `copy_sync` measurement with raw `completed:false` is accepted only for the
  exact supported pre-write additions-review counts/phases and an immediately
  following successful, authorized protocol-1 `review_source_additions` audit.
  The supported synchronous sequential implementation returns the review proof
  before setting the measurement's completion flag. Measurement operation UUID
  and wire request UUID are independent and are not equated. Interrupted copy,
  apply phases, a missing/wrong following audit or an error outcome refuse.
- A raw `response_transport_error` can be classified as a pre-decode denial
  reply loss only with explicit null operation/request/protocol, positive peer
  PID and UID/GID zero under the independently proven dedicated-UID-10001 policy.
  That exact implementation rejects root before decode or dispatch. Authorized,
  unknown or decoded peers and generic transport errors remain uncertain.

Raw flags/outcomes stay unchanged. Separate diagnostic counters preserve this
distinction; neither classification invents a request dispatch or successful
mutation. Final complete-log validation remains mandatory after a future
authorized drain.

Preserve the raw drain observations and seal privately. Under the existing
exclusive application lifetime lock, the matching installed administrator command
`kodi-lab-admin snapshot-recovery-seal REVIEW DRAIN` validates and persists the
normalized seal/evidence in SQLite. It does not dispatch recovery. Both ordinary
services must remain excluded during the coordinated handover/migration.
There is no new durable evidence tree or backup dependency; existing consistent
SQLite backup includes the new rows. Independently retained raw inputs remain
external operational evidence.

Start matched Web/broker/schema `0019_snapshot_recovery` with
`KODI_LAB_SNAPSHOT_RECOVERY_EPOCH` selecting that exact sealed epoch. Web uses the
existing application/authentication/middleware in restricted mode; normal routes,
MCP, executors and router sweepers do not start. The inherited lifetime lock is
checked and flocked. The broker holds a stable lock in its verified runtime socket
volume and remains restricted for its entire process lifetime, even if epoch
state later disappears. A new-only lock never proves a legacy process exited.

Before preview, claim/handoff and execution boundaries, a challenge-bound live
broker attestation must match package, epoch, process owner, boot, Engine,
installation, volume creation identity, exact runtime image/network and complete
inventory. A stale persisted owner or ordinary inventory reply cannot satisfy it.

## Preview, confirmation and execution

Existing administrator login, Host/HTTPS policy, same-origin and CSRF protections
apply. `/snapshot-recovery` presents exact reviewed identity and original snapshot
metadata. A five-minute fingerprint confirmation consumes a new one-use recovery
claim and acquires exact instance/global-heavy leases in a checked writer
transaction. A server-issued stable confirmation UUID also works on LAN HTTP.
No caller supplies filesystem, image or container authority.

The original selected generation closes with its attribution intact. The normal
session handoff reserves one next generation; ordinary capacity/session/version
admission provisions it. Source/ZIP/Bridge confidence is invalidated conservatively
without rewriting successful historical copy/review receipts.

Each `provision`, `apply`, `verify` step has immutable actual wire request and
attempt IDs, intent and deadline. The broker consumes each once before native
entry, checks both exact leases and current identity, and durably records actual
ordinary provisioning admission after reservation and before create. Writer
transactions are not nested around another writer. Long native calls renew the
same leases; lost ownership cannot authorize the next step.

Application uses only the supported fixed snapshot destination and fresh-profile
checks. A separate read-only verification compares every supported entry's exact
bytes/type/size/mode/UID/GID, included-inventory equality and recognized SQLite
integrity. Archive ownership/safe-mode checks and opened no-follow immutable
artifact identity remain mandatory. No fixture application, alternative archive,
workspace copy, global exception or old-receipt continuation exists.

Only complete verified stopped closure creates the typed resolution. Both central
reconciliation and runtime mutation projection independently validate original
receipt, target/snapshot/session lineage, dispatch/admission/events and result
digest. Resolved newer receipts cannot hide an older unresolved receipt. Future
legitimate Start/Stop or later session handoff preserves this historical relation
while retaining ordinary current-identity guards.

## Status, interruption and leaving maintenance

The authenticated `/api/snapshot-recovery/RECOVERY_ID` receipt remains readable
after ordinary Web returns. History links the new operation. Browser reload and
reply loss use the saved status link; no mutation is automatically retried.
`intended_final_state=stopped` is distinct from verified final stopped outcome.

Any committed claim is one-use, including failure. Lease loss, unknown successor,
uncertain create/apply/verify and service restart retain the new and original
fences and every asset. Known but unaccepted create observations remain labelled
as such. Neither timeout nor receipt inspection removes a successor or clears a
lease. Sweeps/restarts preserve the first terminal failure reason. There is no
automatic resume, resend, replacement request ID, handback or destructive retry.

For leaving maintenance, obtain bounded process identities with the read-only
`kodi-lab-admin snapshot-recovery-status EPOCH_ID`. A fresh approved host drain
includes these owners and independently matches their namespace PID/start inside
the exact service CIDs. Container init/tini start is distinct from application
start. `snapshot-recovery-leave EPOCH_ID DRAIN` requires complete process retirement
and either no committed claim or verified success. Uncertainty requires a separate
supported reconciliation decision, not an override.

## Future deployment ordering

After independent qualification and exact installation approval:

1. Stage the matched qualified maintenance helper **before replacing the still
   running legacy Web/broker pair**. Preserve its installed-code/policy identity
   for the supported preflight. Do not pre-stop services as a drain substitute.
2. Run the supported exact-service drain in the proven daemon-host context and
   retain complete raw observations. Keep ordinary services excluded thereafter.
3. Use the supported upgrade path to verify the predecessor-schema backup and
   migrate to `0019_snapshot_recovery`; retain the verified `0018` backup.
4. Perform the metadata-only seal/import within its five-minute drain-freshness
   window. Sealing records proof and dispatches no recovery.
5. Keep ordinary services excluded while independently verifying the consistent
   backup rehearsal. Then start the matched restricted Web/broker/schema pair.
6. Obtain current paired attestation and a **new five-minute preview** for explicit
   confirmation. A fresh preview does not replace drain, attribution or topology
   proof. Native execution and any production installation/recovery still require
   their separately approved phase.

## Quiescence source trace and validation

`storage/snapshot_recovery.py` has explicit terminal vocabularies, not a guessed
active-state/name filter:

| Work family | Source machinery and refused nonterminal forms |
| --- | --- |
| Restore/reset/lifecycle/ZIP/source/repository/promotion operations | Their typed coordinators/executors; queued/running plus any unknown nonterminal state |
| Runtime acquisition | `runtime_recipe_installations`: pending/acquiring/assembling/validating |
| Workspace capture/import/creation/cleanup | Capturing/creating/staging/switching/applying/queued/running; immutable completed receipts remain history |
| Managed version/Create/Reset/Bridge parents | Explicit phase/checkpoint predicates for accepted/prepared/planned/dispatched/confirmed and other incomplete phases |
| MCP execution/deployment/launch/settings and request dispatch | Pending/running/dispatching; ambiguities stay subject to target reconciliation |
| Replacement/promotion review | Open unbound session replacements and pending/approved replacement or queued promotion work |
| Reservations/input/viewers/holds | Exact lease/reservation rows, durable remote/noVNC claims and unreleased runtime holds; expiry is not drain proof |
| New recovery | Any other consumed non-successful claim; own dispatch identity and phase checked separately |

Inert retained authorization/registration records do not prove a worker is live;
their actual child-operation and dispatch records are checked. No sweeper clears
work to manufacture eligibility. Focused validation covers real predecessor
migration/constraints, existing-state preservation, authorization, drift before
and after preview, process/context mismatch, leases/claims, wire replay, writer
wait/native-entry expiry, uncertainty and conservative restart. Native acceptance
must separately reproduce the legacy failure in an owned Engine with matched
installed artifacts and independently inventoried synthetic/approved fixture
bytes. Original-profile acceptance requires the actual original complete entry
count/profile/skin evidence, usable bounded startup and final normal Stop.
