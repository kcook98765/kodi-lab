# Phase 7 snapshot contract

Phase 7 Steps 2 through 6 implement creation, source-bound restore, explicit
deletion, and preserve-or-destroy disposable ManagedInstance deletion for
centrally stored immutable snapshots. A Snapshot is captured mutable instance state;
it is neither a RuntimeRevision, a FixtureDefinition, nor a reset baseline.
Persistent snapshots/instance deletion, clone/import, undelete, archive
transfer, content browsing, incremental storage, compression, cross-instance
restore, and cross-revision behavior are deferred.

## Content and capture state

The broker captures a closed subset beneath `/home/kodi/.kodi`:

- included state is `userdata/**`, `addons/**`, `fixture-media/**`, and
  `.smb/**`;
- excluded state is `temp/**`, `addons/packages/**`, `addons/temp/**`,
  `userdata/Thumbnails/**`, `userdata/Database/Textures*.db*`, Kodi logs,
  crash/core files, and PID/lock artifacts;
- recognized empty-only runtime scaffolding is exactly top-level `media/` and
  `system/`. Either may exist only as an empty directory and is omitted from
  archive and manifest. Any child of any type fails capture before publication;
- reject every unknown top-level entry rather than silently omitting it;
- reject symlinks, hardlinks, sockets, FIFOs, devices, and every type other than
  regular file or directory.

Empty-only scaffolding is neither included nor excluded state. It does not
recursively allow `media` or `system`, and it does not generalize to arbitrary
empty trees. Both streamed source passes use the same safe archive
classification; content appearing between passes is observed and causes
capture failure rather than being ignored.

The source must be one exact owned provisioned disposable container whose
revision label matches the persisted pin. A stopped source remains stopped. A
running source is normally stopped and proven stopped before capture, then
restarted and required to pass normal readiness. Unprovisioned, transitional,
unknown, ambiguous, and persistent sources fail closed.

## Archive, manifest, and bounds

Archive v1 is deterministic uncompressed tar. Members are sorted relative
paths, uid/gid and names normalize to Kodi identity 1000/1000 and `kodi`, mtime
normalizes to zero, and only the source's safe permission bits are retained.
Capture uses streaming reads and never loads the archive into memory.

Canonical manifest version 1 is compact UTF-8 JSON with sorted keys and a final
newline. It records snapshot/source/revision/fixture identities, exact Kodi
release, capture timestamp, prior runtime state, archive SHA-256/size/member
count, and each member's relative path, type, size, safe mode, and regular-file
SHA-256. Archive and manifest digests identify their exact bytes.

Trusted non-request configuration fixes these limits:

- archive: 16 GiB;
- one regular file: 8 GiB;
- members: 250,000;
- relative path: 512 UTF-8 bytes;
- manifest: 64 MiB;
- free space: at least `max(2 GiB, estimated archive bytes + 1 GiB)`;
- operation and heavy-work leases: ten minutes, renewed at meaningful phases.

Unknown free space fails before large archive writes.

## Storage and validation

The only final path is
`KODI_LAB_DATA_ROOT/snapshots/<snapshot-uuid>/manifest.json` plus `state.tar`.
The broker creates UUID-derived staging below the same snapshot root and uses an
atomic rename. Final directories are root:`kodi-lab` 0750; both files are 0440.
Labels never influence paths and successful artifact bytes are never updated.
Capture failure may remove only the exact proven staging subtree and never a
published snapshot.

The application independently proves the final directory identity and exact
contents; canonical manifest; archive/manifest digests; sizes/counts; member
inventory, ordering, paths, types, ownership, modes, mtimes, and hashes; and all
numeric limits. Recognized `userdata/Database/MyVideos*.db` members are copied
to private temporary validation files and checked read-only with
`PRAGMA integrity_check`. Add-on databases remain opaque byte-preserved state.
Only fully validated artifacts become available; failed validation produces a
failed or quarantined non-restorable Snapshot.

## Persistence and coordination

`Snapshot` durably records immutable source/revision/fixture/capture identity,
optional validated label, manifest version, digests, archive size/member count,
availability state, and validation result/time. Available snapshots have no
automatic pruning.

`SnapshotCreateOperation` is a separate durable workflow with queued, running,
succeeded, failed, and reconciliation-required states and bounded real phases.
Queued work can be acquired after restart. Expired running work is inspected
only through trusted state and never resumed automatically; uncertain state is
quarantined/reconciliation-required. Terminal operation history follows the
existing bounded operation convention.

A closed `ManagedInstanceMutationLease` excludes reset, snapshot create/
restore, lifecycle, fixture assignment, and runtime reassignment for one
instance. Reset retains its own operation model. A singleton global-heavy lease
allows only one create, restore, or delete across the lab; ordinary lifecycle on
other instances remains independent. SQLite unique/conditional writes, opaque
token digests, expiry, and phase renewal enforce ownership across processes.

## Broker and Web boundaries

The sole new broker operation is `capture_managed_instance_snapshot`. Its
request has exactly snapshot UUID, ManagedInstance UUID, and expected immutable
container ID. Trusted broker-side resolution supplies all paths, source scope,
revision, fixture, ownership, archive policy, and bounds. There is no generic
copy-out, copy-in, tar, exec, caller destination, Docker field, member list, or
archive payload.

Authenticated Web supports per-instance creation, list, and bounded operation
status only. POST is same-origin and CSRF-protected and accepts only an optional
label. Labels are trimmed, nullable, at most 120 Unicode scalar values, reject
controls and path separators, and are escaped in presentation. Confirmation
warns about the temporary Kodi stop, prior-state restoration, sensitive local
state, central local storage, and lack of automatic retention.

Snapshots are secret-bearing. Web never exposes storage paths, member names,
raw manifests, archive bytes, credentials, media paths, preview, download,
or import authority. Deletion is available only through the explicit bounded
per-Snapshot workflow below.

## Failure and identity semantics

The operation refreshes leases between stop, capture, validation, publication,
restart, and readiness phases. Exact RuntimeRevision and fixture assignment are
rechecked and never mutated. Reset still returns to the assigned deterministic
fixture baseline and ignores snapshots.

Failure before validated publication leaves no available Snapshot. A captured
artifact that fails independent validation is quarantined. If an artifact is
valid and available but restart/readiness cannot restore a previously running
source, the Snapshot remains available while the create operation fails or
requires reconciliation. No valid snapshot is deleted to hide a lifecycle
failure.

## Disposable restore contract

Restore is destructive and source-bound. Only an `available`, independently
valid Snapshot can return to its recorded source disposable ManagedInstance.
Current and captured RuntimeRevision UUIDs must be identical, and current and
captured fixture assignment UUIDs must be identical, including null equality.
Eligibility failure occurs before lifecycle mutation. Restore never changes a
pin, fixture assignment, support tier, or Snapshot byte.

The application revalidates the root-owned UUID-derived artifact before
stopping the source. It records whether the exact single source container is
running or stopped, normally stops it when necessary, proves stopped state,
deprovisions it, proves absence, and provisions a fresh stopped container from
the unchanged exact revision. Fixture initialization is not run. The broker
then applies `state.tar` to the fixed `/home/kodi/.kodi` destination.

The fresh profile may contain only the exact image seed `userdata/` and
`userdata/guisettings.xml`, plus already classified excluded runtime artifacts
and empty-only `media/` or `system/`. Any other included or unknown state fails
before extraction. The broker revalidates artifact ownership, canonical
manifest, archive digest, inventory, bounds, and types; reads the applied
profile back; verifies every declared file hash, size and safe mode plus exact
directory inventory; rejects unexpected included state; and performs read-only
integrity checks for recognized `MyVideos*.db` files. The application compares
the bounded broker proof and revalidates the central artifact again.

If the source was running, the fresh restored container is started and must
pass normal readiness. If it was stopped, it remains stopped. Final success
also rechecks unchanged revision and fixture identities. Repeated restore uses
the same immutable artifact but always provisions a new container identity.

`SnapshotRestoreOperation` remains separate from create and reset records. It
holds the same per-instance mutation exclusion and singleton global-heavy
lease, persists only bounded semantic phases, and records prior source state.
Queued work may start after process restart; expired running restore is never
destructively resumed and is terminalized reconciliation-required.

Before old-container removal, failure retains the old container and attempts
normal restart/readiness if restore stopped a running source. After removal,
provision or any apply/verification uncertainty requires reconciliation. A
fresh container with failed application or validation is retained stopped when
provable, without fixture fallback or automatic reset. When bytes verify but
restart/readiness fails, structural restore remains present stopped and the
operation reports reconciliation rather than success. Snapshot availability
and bytes remain unchanged in every case.

Web adds authenticated metadata-only restore confirmation, queued POST, and
bounded polling status. POST accepts only `confirmed=true`, is same-origin and
CSRF protected, and returns 202 without depending on request lifetime. The
confirmation states that current disposable state is discarded and no
automatic safety snapshot is created. It exposes no archive, manifest, member,
path, container, runtime-selection, fixture-selection, download, or repair
authority.

## Explicit snapshot deletion contract

Deletion is administrator-initiated only. There is no TTL, automatic pruning,
retention cap, disk-pressure policy, bulk operation, or delete-all control. One
available, failed, or quarantined Snapshot may be selected through its source
ManagedInstance route. Queueing atomically changes it to `deleting` and creates
a separate `SnapshotDeleteOperation`; creating and deleting states are not
eligible. Once deleting, the Snapshot is immediately non-restorable and cannot
receive a second delete operation.

The delete operation records copied Snapshot and source-instance UUIDs, prior
terminal state, bounded timestamps/state/phase, lease ownership, and safe
failure status. It intentionally has no retaining Snapshot foreign key, so the
history survives successful Snapshot-row removal. Existing create and restore
operation records likewise retain copied target UUID identity. Terminal delete
history follows the bounded operation convention and stores no member, content,
archive-path, or secret data.

Deletion follows one ordering: transition to deleting, acquire the singleton
global-heavy lease, invoke the broker, independently prove the UUID directory
absent, delete Snapshot metadata, then terminalize operation success in the same
database transaction. Artifact removal therefore always precedes metadata
removal. No Snapshot tombstone remains in the primary table. The source
ManagedInstance and its container, pin, fixture, and runtime state are not
mutated, so deletion does not acquire the per-instance mutation lease.

`delete_managed_snapshot_artifact` accepts exactly one canonical Snapshot UUID.
Broker-side persistence must prove that Snapshot exists in `deleting` state.
The broker derives only
`KODI_LAB_DATA_ROOT/snapshots/<snapshot-uuid>`, verifies root and target
ownership/modes, opens both without following links, and recursively removes
only descriptor-relative children. Symlinks are unlinked rather than followed;
the snapshots root, siblings, staging directories, and outside paths are never
targets. No host path, filename, wildcard, recursive flag, Docker field, shell,
or generic deletion authority crosses the protocol.

An absent exact artifact is acceptable only for an already-deleting Snapshot,
including failed/quarantined captures with no published artifact and the crash
window after broker removal. An available Snapshot missing its artifact cannot
be silently treated as deleted. The application independently checks the fixed
UUID path after the broker returns before it removes metadata.

Failure or uncertainty after filesystem deletion begins leaves the Snapshot
`deleting` and the operation failed or reconciliation-required; it never assumes
the artifact is intact and never rolls back to available. Trusted read-only
inspection distinguishes row-plus-artifact present, row-plus-artifact absent,
and inconsistent storage. Expired running delete operations are terminalized
reconciliation-required and never automatically invoke deletion again.

Authenticated Web confirmation displays only safe capture metadata and warns
that deletion is permanent, prevents future restore, and does not alter the
current Kodi instance. The same-origin CSRF-protected POST accepts only
`confirmed=true`, returns 202, and exposes bounded operation status. Deleting
Snapshots show pending status without restore or duplicate-delete actions;
successful ones disappear from the list. No content, member, manifest, path,
download, retention, bulk, or automatic deletion interface exists.

## Disposable ManagedInstance deletion contract

Deletion requires an authenticated same-origin CSRF-protected strict request
containing exactly `confirmed=true` and an explicitly supplied
`preserve_snapshots` Boolean. Neither choice is defaulted. Confirmation counts
and aggregate known bytes are informational; queueing reloads every Snapshot
and active operation server-side, accepts only available/failed/quarantined
rows, and atomically reserves the `INSTANCE_DELETE` mutation kind. The browser
never selects Snapshot UUIDs. Snapshot delete queueing reciprocally rejects an
attached source with an instance-delete reservation.

The durable disposable-only operation stores immutable original instance,
name, revision and fixture identity; explicit policy; authoritative count and
aggregate bytes; current UUID and completed progress; expected/observed
container identity and prior state; bounded timestamps/lease/safe failure; and
reconciliation status. It is not a generic job. Active reset, Snapshot
create/restore/delete, transitional Snapshot, lifecycle, fixture, revision, or
second deletion work conflicts. Preserve holds only the instance lease. Destroy
also holds the singleton global-heavy lease across artifact work.

Both branches cancel readiness observation, resolve exactly zero or one owned
container, normally stop and prove a running container stopped, deprovision an
owned stopped container, and prove complete absence before touching user
Snapshots. Unsupported/ambiguous container state fails closed. This ordering
prevents Snapshot destruction followed by discovery that the instance cannot
be removed.

Preservation finishes in one database transaction. The complete unchanged
Snapshot set is revalidated; each row keeps its UUID, artifact bytes/digests,
runtime and fixture references, capture time, label, and validity state while
its independent source binding becomes `deleted_source`. The live source FK is
cleared and immutable original ManagedInstance UUID, source display name, and
deletion timestamp remain. Authority is released, ManagedInstance/Target
metadata is removed, notes are copied once to deletion history, and the
operation succeeds atomically. No committed detached/live-usable intermediate
state exists and artifact paths are never moved or renamed.

Destroy re-enumerates the unchanged terminal set in deterministic UUID order.
For each row it records deleting/current progress, runs the existing UUID-only
broker deletion core, independently proves artifact absence, removes metadata,
and increments progress. No nested `SnapshotDeleteOperation` is created. Only
after every enumerated artifact and row is absent does the final transaction
release authority, remove instance/Target metadata, copy bounded notes, and
terminalize success. Failed and quarantined rows follow the selected policy and
are never silently discarded.

Source binding and validity are orthogonal. A `deleted_source` Snapshot remains
available/failed/quarantined as appropriate but is archive-only in v1. Restore
returns `source_instance_deleted` before mutation/global-heavy leases, Docker,
or broker work. Authenticated `/snapshots/preserved` presentation exposes only
label, UUID, capture/source identity, trusted Kodi release/revision, captured
fixture, size, validity and binding. It offers exact Delete through the proven
artifact-first operation, but no Restore, manifest, member, path, bytes,
download, preview, clone, or import.

Terminal reset and Snapshot operation histories have no mandatory live
ManagedInstance FK; their existing ManagedInstance UUID is immutable historical
identity. Instance-delete history is retained without automatic pruning.
`ManagedInstance.notes` and `notes_at_deletion` preserve null/empty semantics
and exact content but accept at most 4,096 Unicode code points. They are never
trimmed, normalized, or truncated. Migration refuses any oversized legacy note,
and the UI reports only whether a note exists. Notes never enter Snapshot
metadata or other operation histories.

Any stale/expired destructive instance deletion becomes
reconciliation-required and is never resumed. Read-only inspection reports
live instance and container presence, policy, attached/detached/deleting row
counts, artifact presence/absence, phase, and durable progress. It does not
delete, repair, clone, restore, or infer completion.
