# Phase 7 snapshot contract

Phase 7 Step 2 implements creation of centrally stored immutable snapshots for
disposable ManagedInstances only. A Snapshot is captured mutable instance state;
it is neither a RuntimeRevision, a FixtureDefinition, nor a reset baseline.
Restore, deletion, persistent snapshots, archive transfer, content browsing,
incremental storage, compression, and cross-revision behavior are deferred.

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
automatic pruning. Delete is absent.

`SnapshotCreateOperation` is a separate durable workflow with queued, running,
succeeded, failed, and reconciliation-required states and bounded real phases.
Queued work can be acquired after restart. Expired running work is inspected
only through trusted state and never resumed automatically; uncertain state is
quarantined/reconciliation-required. Terminal operation history follows the
existing bounded operation convention.

A closed `ManagedInstanceMutationLease` excludes reset, snapshot create/future
restore, lifecycle, fixture assignment, and runtime reassignment for one
instance. Reset retains its own operation model. A singleton global-heavy lease
allows only one create or future restore across the lab; ordinary lifecycle on
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
import, restore, or delete controls.

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
