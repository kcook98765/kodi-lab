# Architecture

`V1-SPEC.md` is authoritative. This document is a concise implementation map,
not a replacement specification.

Kodi Lab uses Python, FastAPI, server-rendered HTML with minimal JavaScript,
SQLite metadata, filesystem artifact storage, Nginx, Docker Compose, and a
restricted container-management broker. The application and broker must not
grant unrestricted Docker-socket access.

The common application/service layer will serve web, API, and MCP adapters.
MCP operates and develops against targets; the web UI administers the lab.
Managed and remote Kodi implementations share a generic Target abstraction.
Version-specific behavior belongs in runtime/Kodi adapters rather than service
conditionals.

The recommended module boundaries in specification section 48 will be added as
their implementation phases begin. Phase 0 keeps only the package,
configuration, and migration metadata needed for a working foundation.

## Core domain and storage

The Phase 1 core slice keeps immutable typed domain records separate from
SQLAlchemy rows. A managed instance is the managed subtype of a Target and uses
the Target ID as its own stable identity. Target deletion cascades to its
managed-instance subtype row; repositories expose domain records and leave
transaction boundaries to callers.

SQLite connections created by the application engine helper enable foreign-key
enforcement explicitly. Alembic owns schema creation and version transitions.

### Runtime catalog

Runtime definitions provide stable UUID identity plus a unique operator-facing
slug and Kodi-version metadata. Each finalized runtime revision has its own UUID
and a positive per-definition revision number; revision number is the only
newest-revision ordering authority.

Revision source, manifest, SHA-256 content identity, and build timestamp are
immutable after creation. Approval and Trusted Custom Build confirmation are
separate lifecycle metadata changed through explicit repository operations.
There are no normal definition/revision deletion APIs, and restrictive foreign
keys preserve revision history. Managed instances reference exactly one runtime
revision UUID and may change that selection without changing the revision.

## Managed-instance application service

`kodi_lab.application` is the transaction and policy boundary above the explicit
repositories. Each create or runtime-reassignment operation opens one database
transaction, resolves stable UUID identities, selects an exact or newest-approved
revision, applies approval and Trusted Custom Build policy, validates Kodi-version
compatibility, and commits only the completed managed-instance change.

The service exposes typed application errors instead of SQLAlchemy failures.
Outer CLI, web, and MCP adapters may translate those errors later without
duplicating selection policy. The service has no Docker, Kodi, transport, or
runtime-installation dependency.

## Runtime-catalog administration service

`RuntimeCatalogAdministrationService` owns atomic registration of logical
definitions and complete finalized revisions, plus the approval and per-revision
Trusted Custom Build confirmation lifecycles. New revisions are unapproved and
unconfirmed; the service never mutates intrinsic revision data.

Lifecycle administration and runtime usability remain separate concerns. The
administration service records approval/trust state, while
`ManagedInstanceService` applies that state plus injected global Trusted Custom
Build policy when selecting a revision. Existing instance references remain
historically valid after approval or trust is revoked.

## Restricted broker boundary

Phase 2 introduces `kodi_lab.broker` as a separate privileged-infrastructure
boundary. Unprivileged application code uses a typed client over
`/run/kodi-lab/broker.sock`; it does not receive Docker-group membership or
direct Docker-socket access. The production broker runs as root and authorizes
only the kernel-reported Unix peer UID resolved from the dedicated `kodi-lab`
account. Filesystem permissions restrict access first, but exact-UID
`SO_PEERCRED` authorization is still mandatory. No token or secondary secret is
part of this local protocol.

The broker uses one bounded, length-prefixed JSON request and response per Unix
stream connection. A closed operation registry validates operation-specific
parameters before invoking an explicit handler. It exposes `ping`, `broker_info`,
`capabilities`, the empty-parameter `managed_container_inventory`, UUID-only
`provision_managed_container`, and explicit semantic `start_managed_container`
and `stop_managed_container`, plus stopped-only
`deprovision_managed_container` and fixed-purpose
`observe_managed_kodi_runtime`; all require the authorized peer. Existing-container
mutations retain strict ownership, immutable-identity, and runtime-state
preconditions. There is no shell, path, environment, raw Docker, generic
execution/reconciliation/create/remove/restart/exec, or other lifecycle
operation.

The client/broker boundary is independent of Phase 1's service policy. A narrow
infrastructure adapter performs fixed read-only Docker list/inspect calls for
inventory and fixed semantic provision/start/stop/deprovision operations. It selects
containers by the
exact `io.kodi-lab.managed=true` and canonical
`io.kodi-lab.instance-id=<UUID>` ownership contract, then emits a bounded
Docker-independent schema. Names, legacy labels, images, networks, and Compose
metadata never establish ownership. Raw inspect data does not cross the broker
boundary. Start and stop resolve through both labels, freshly revalidate the
selected object against the caller's observed immutable container ID/state, and
verify the desired state afterward; stop alone uses a fixed 10-second timeout.
Already-converged containers succeed without mutation, while a required mutation
requires expected state to match fresh observed state. Transitional/unknown
states are never mutated, and no Kodi readiness is inferred. Future lifecycle
adapters likewise remain behind semantic allowlisted handlers rather than being
imported into Phase 1 services. See
`BROKER-SECURITY.md` for the protocol, threat model, path-safety, audit, and
deployment contracts.

Provisioning adds a read-only trusted-state resolver inside the privileged
boundary. Given only managed-instance UUID, it reads the Target, ManagedInstance,
exact pinned RuntimeRevision/RuntimeDefinition, and separate immutable
RuntimeContainerArtifact mapping. The resolved Docker specification never comes
from the client. The adapter may then create only a disposable, stopped container
with deterministic name, fixed ownership/revision labels, exact image ID, and the
validated `kodi-lab-managed-v1` prerequisite network. It publishes no ports and
adds no mounts, environment, command, entrypoint, privilege, devices,
capabilities, host namespaces, or resource limits. Creation, start, and stop
remain separate explicit operations.

Provisioning retries adopt nothing by name. Existing owned state is idempotent
only after full invariant validation. Post-create failures remove only the exact
new ID after fresh ownership proof; uncertain partial state is retained for
operator reconciliation. The broker never creates or rolls back images,
networks, or volumes. Persistent storage remains deferred.

Deprovision resolves the canonical persisted ManagedInstance revision pin before
Docker access, then discovers exactly one container through fixed ownership
labels. Fresh inspect must prove the caller-observed immutable ID, canonical
instance and revision labels, deterministic name, and stopped state. The adapter
uses only normal non-forced, non-volume removal and proves both exact-ID absence
and absence of any remaining owned container for the instance. It does not delete
application/catalog state, images, networks, or storage. Non-identity Docker
configuration drift is deliberately not an ownership substitute or a permanent
barrier to removing an otherwise exactly identified stopped container.

## Phase 3 single-runtime readiness

The first Phase 3 runtime is a dedicated Kodi 21.3 image whose immutable result
is registered separately from its logical revision. Repository-owned build
assets provide an internal Xvfb display, software rendering, a fixed signal-aware
entrypoint, a minimal disposable seeded profile, and a process-only Docker
healthcheck. Kodi runs as non-root user `kodi`; the container publishes no ports,
declares no volumes, and uses no host display, device, capability, or legacy
runtime resource.

Each Kodi revision used for execution has one immutable trusted execution
profile. The profile carries only bounded semantic expectations: exact Kodi
identity, fixed internal JSON-RPC endpoint, process observation kind, readiness
probe kind, and time bounds. It is persisted separately from provenance and has
no update API. Provisioning remains UUID-only and continues to inherit image
defaults; it gains no caller-provided Docker fields.

The root broker performs only Docker-owned observation. Its fixed-purpose
runtime operation resolves the persisted revision/profile, proves the immutable
owned running container with fresh inspect, maps Docker health to Kodi process
state, and returns one address from the sole canonical managed-network
attachment plus the profile-owned port. It exposes neither raw inspect nor
caller-selected networking and performs no HTTP or exec.

Unprivileged Kodi code performs the application probes directly over the managed
bridge. Its adapter has explicit ping and identity operations only. The readiness
coordinator keeps container, process, JSON-RPC, and usability state separate and
uses a single 90-second monotonic deadline, one-second polling, and bounded
three-second HTTP attempts. Usability means a running container, healthy Kodi
process, valid JSON-RPC ping, and exact Kodi 21.3 stable application identity.
See `PHASE3-RUNTIME-READINESS.md` for the executable contract.

## Phase 2 closeout boundary

Phase 2 is complete at the repository and controlled-host-validation levels.
The implemented broker surface is limited to health/capability metadata,
managed inventory, disposable create-only provisioning, two-state start/stop
reconciliation, and stopped-only deprovision. Controlled development host validation used
the real Unix-socket authorization and Docker adapter paths for the full
provision/inventory/start/stop/idempotency/deprovision sequence and confirmed
legacy isolation. It left the canonical managed network and an unprovisioned
validation-state record, but no managed validation container.

This closeout does not make the broker a general runtime orchestrator. No
component currently owns managed-network creation/removal as a product API;
provisioning only validates the prerequisite network. Persistent-instance
storage, production service installation/naming, and Kodi process/JSON-RPC
readiness remain outside the Phase 2 boundary. Phase 3 consumes the immutable
runtime artifact, managed-instance pin, typed broker client, ownership labels,
and semantic lifecycle operations to establish one real Kodi runtime end to end.

## Phase 4 authenticated Web slice

The first Web adapter is a localhost-only FastAPI application at
`127.0.0.1:8100`; `127.0.0.1:8180` is reserved for ephemeral validation. It uses
server-rendered Jinja2 templates, local CSS, and a small polling/action script.
Configuration rejects wildcard, LAN, and unapproved port values. There is no
Nginx, CORS, public API, production service, or LAN exposure in this slice.

Authentication uses one fixed local administrator provisioned only by the
hidden-input `kodi-lab-admin set-password` command. Argon2id protects the
password. Browser sessions are opaque 256-bit bearer tokens whose SHA-256
digests and expirations are stored in SQLite. Cookie-authenticated mutations
require a session-bound synchronizer token and same-origin validation; login is
also origin-checked and has bounded in-process throttling.

`ManagedRuntimeStatusService` is the sole display aggregation boundary. It joins
short-lived canonical repository reads with bounded typed broker inventory and
process observation plus ephemeral readiness results. A separate lifecycle
service reloads trusted immutable container/revision/state preconditions before
calling explicit BrokerClient methods. Browser requests never carry those
preconditions and no Web module imports or invokes the Docker SDK.

After broker start, a per-instance in-process registry runs the existing bounded
readiness coordinator without holding the HTTP action open. Generation and
cancellation state invalidate stale work. Results are never persisted; after a
Web restart, running runtime status is reconstructed and readiness is reported
as not checked until a new approved readiness run.

## Phase 5 curated runtime matrix contract

Target and RuntimeDefinition now share an exact typed Kodi release identity.
Stable releases use canonical `major.minor`; alpha, beta, and RC releases append
their channel and positive ordinal. Each RuntimeDefinition is one immutable
release identity and independently carries mutable product support policy:
Current, Legacy, Preview, or Retired. Support transitions are explicit,
validated, monotonic, and never rewrite release identity or historical pins.

Runtime validation is independent of approval and Trusted Custom Build
confirmation. Trusted administration appends immutable evidence to the exact
RuntimeRevision. Eligibility requires the newest applicable passing evidence for
`kodi-runtime-v1`; old-contract and earlier run records remain inspectable.
There is no durable validating state and evidence registration does not execute
Docker or Kodi work.

`RuntimeRevisionEligibilityPolicy` is the single new-pin policy boundary. It
combines exact release compatibility, permitted support tier, approval/trust,
artifact/profile existence, profile identity agreement, and validation. Normal
selection permits Current; explicit trusted flags add Legacy and/or Preview;
Retired is always excluded. Exact selection cannot bypass policy. Newest-eligible
selection stays within one definition and chooses the highest eligible revision,
allowing a valid older revision when a newer one is unapproved, incomplete, or
failed. Existing ManagedInstance pins are not reinterpreted through this policy.

New revision validation uses an isolated, privileged validation database. Its
orchestrator may bootstrap one exact stopped disposable pin after catalog,
artifact, profile, approval, and compatibility checks, because validation
evidence cannot exist before the runtime is exercised. This is not a product
selection path and is not exposed through Web or broker request fields; ordinary
creation and reassignment continue to require passing evidence.

Execution profiles retain immutable per-revision behavior but no longer encode
Kodi 21.3 as the only possible identity. The 21.3 values remain unchanged, while
prerelease ordinals and bounded future per-version endpoint/timing differences
are representable. Build assets remain in version-owned directories.

`RuntimeMatrixService` now projects that catalog into authenticated semantic
DTOs. It lists every curated definition, including retired history, and reports
exact release/channel/ordinal, support tier, effective validation, newest and
selected revision numbers, bounded availability/reason, opt-in requirements,
and existing-pin count. It returns neither revision UUIDs nor artifact, image,
evidence-path, provenance, or Docker details. Newest-eligible evaluation remains
owned by `RuntimeRevisionEligibilityPolicy`; templates do not reproduce it.

The Web adapter adds authenticated read-only matrix HTML/JSON and exact
Target-scoped resolution. Strict Include Legacy and Include Pre-release query
flags are request-local preferences. Global Trusted Custom Build enablement is
trusted deployment configuration and has no browser override. Resolution uses
the Target's immutable exact Kodi identity and cannot cross definitions. This
slice deliberately adds no instance creation/reassignment or catalog mutation:
those require a later Target administration contract. Existing pins continue to
be presented by status services without retroactive new-selection filtering.

## Phase 6 disposable reset and predefined fixture slice

`FixtureDefinition` is finalized create-only catalog metadata for trusted
package resources, not a binary store or scripting language. A ManagedInstance
may reference one exact definition through a restrictive nullable foreign key.
The first two immutable packages are `small-mixed-v1` and `small-mixed-v2`, both
compatible only with Kodi 21.3 stable. v1 is retained historical evidence;
because source visibility alone did not give the scanner content assignments,
v2 adds a Kodi-generated `MyVideos131.db` with only fixed Movies/TV path
configuration and no library or user rows. Kodi still scanner-creates all media
entities from NFO and synthetic media. Trusted application administration
registers either fixed manifest and assigns it only to a compatible disposable
exact pin. Runtime reassignment validates a retained assignment before changing
the pin.

`apply_assigned_fixture` adds one semantic operation to the restricted broker.
Its request carries only ManagedInstance UUID and expected container ID. Trusted
broker-side resolution joins persisted assignment and revision state to the
package-owned manifest, then the Docker adapter revalidates the exact canonical
archive before fixed-destination extraction into `/home/kodi/.kodi`. Archive
members have fixed Kodi uid/gid and permissions, and the operation requires one
exact owned stopped container whose revision label matches the persisted pin.
There is no generic Docker copy surface and no exec or caller-selected path,
payload, fixture, destination, method, or parameter. The v2 database uses the
same operation and fixed destination; there is no SQL or database broker API.

`DisposableInstanceResetService` owns stop/deprovision/provision and optional
fixture initialization as one application state machine. It takes only
ManagedInstance UUID, holds one process-local keyed lock, performs short catalog
reads without spanning infrastructure work, and provisions from the existing
exact RuntimeRevision pin. It never invokes runtime eligibility or revision
selection. Success always ends provisioned and stopped; no-fixture instances do
not temporarily start. Assigned fixtures are applied while stopped, initialized
through normal start/readiness plus a fixed unprivileged Kodi adapter, verified
semantically within 60 seconds, and normally stopped again.

Reset is bounded reconciliation, not a transaction over Docker. Failure before
old-container removal retains the proven old state; failure provisioning after
removal leaves the catalog pin intact and no container. Fixture application or
initialization failure retains the fresh container stopped if that state can be
proved, otherwise it returns reconciliation-required. Persistent reset,
transitional or ambiguous state, force removal, revision upgrade, distributed
locking, reset history, and Web reset are absent from this slice. See
`PHASE6-RESET-FIXTURES.md` for the executable contract.

`ResetStatusService` is the application-owned read projection for reset and
fixture state. It joins durable instance, exact pin, and fixture metadata with
fresh managed-container inventory and the injected process-local
`ResetActivityRegistry`. Eligibility and compatibility are computed once in the
application layer; templates do not reconstruct policy. The DTO contains only
semantic provision/container states, exact Kodi release and pinned revision,
bounded fixture metadata, current phase, last in-process result, and safe
failure/reconciliation fields. It excludes container/image identity, package or
evidence paths, and infrastructure exceptions.

The authenticated dashboard and one read-only JSON route consume that
projection. The registry is bounded memory, not durable history or a
multi-worker lease; a process restart truthfully returns to idle and rebuilds
current eligibility from persistence plus broker observation. Destructive Web
reset, confirmation UX, and cross-process coordination remain deferred.

Phase 6 Step 3 adds a reset-specific durable coordinator. `ResetOperation`
stores one bounded state/phase, timestamps, safe failure, reconciliation flag,
and opaque lease-token digest. A SQLite partial unique index permits at most one
queued or running operation per instance; conditional updates require the
matching digest and an unexpired lease. Queue, claim, heartbeat, and terminal
updates use short transactions and never span broker/readiness work. The newest
20 terminal rows per instance are retained.

Queued work is claimable after restart. Expired started work is never resumed:
it becomes reconciliation-required with its last phase retained. The local
registry improves same-process presentation but grants no ownership. Manual
acknowledgement is deferred until its durable audit/evidence semantics exist.

The localhost Web adapter adds an informational confirmation and strict POST
that creates a queue record and returns 202. Only the lease winner invokes the
existing reset service. Active reset blocks Web lifecycle and trusted
fixture/revision changes. Browser input cannot select revision, fixture,
container, image, phase, or operation authority; Web retains no Docker socket.

## Phase 7 disposable snapshot and instance lifecycle

Snapshot creation is a distinct typed workflow over mutable Kodi profile
state. It does not alter RuntimeRevision identity, FixtureDefinition assignment,
or reset semantics. Phase 7 Steps 2 through 6 support disposable
instances only and deliberately supply no import/export, download, automatic
retention, bulk deletion, or persistent-instance behavior.

The broker is the only snapshot byte writer. From a request containing only
snapshot UUID, ManagedInstance UUID, and expected immutable container ID, it
resolves canonical database identity and captures the fixed allowlist below
`/home/kodi/.kodi`. It performs two bounded streaming passes, rejects unknown or
unsafe state, writes deterministic uncompressed tar plus canonical manifest to
UUID-derived staging, and atomically publishes root-owned group-readable files
under `KODI_LAB_DATA_ROOT/snapshots`. The application then independently
validates storage identity, manifest form, all digests/member metadata, bounds,
and recognized Kodi video databases before marking the Snapshot available.

`SnapshotCreateOperation` remains operation-specific. Its ten-minute lease is
coupled transactionally to a per-instance typed mutation lease and a singleton
global-heavy lease. Reset uses the same per-instance exclusion while retaining
`ResetOperation`; lifecycle, fixture assignment, and revision reassignment use
short typed mutation leases. This adds mutual exclusion, not a generic job
system or general filesystem/Docker authority.

Capture starts only from one exact stopped or running owned container. Running
sources are normally stopped and proven stopped. After valid publication, the
application restores the prior running state and requires normal readiness;
stopped sources remain stopped. A valid available artifact survives a later
restart failure, while the operation records the source-state recovery failure
or reconciliation requirement.

Restore adds one equally narrow broker operation. The caller supplies only
Snapshot UUID, source ManagedInstance UUID, and expected fresh container ID;
the broker resolves and revalidates the immutable artifact, applies it only to
the fixed Kodi profile root, and reads the stopped container back to prove
manifest inventory, modes, hashes, and recognized database integrity. The
application validates before lifecycle mutation and revalidates the central
artifact afterward.

`SnapshotRestoreOperation` is distinct from create and reset. It uses the same
per-instance and singleton global-heavy leases, records the source's immediate
running/stopped state, and drives exact old-container removal, unchanged-pin
fresh provisioning, fixed snapshot application, verification, and optional
start/readiness. Any uncertainty after destructive removal is reconciliation
required; stale running restore is never automatically resumed.

Snapshot metadata is safe for authenticated list/status/restore-confirmation
presentation, but snapshot files and manifests are secret-bearing. Web has no
archive, manifest, member-list, path, preview, download, import, or deletion
route outside the exact per-Snapshot delete workflow. Restore confirmation
accepts no container, path, revision, fixture, or archive authority and warns
that there is no automatic backup.

Deletion is its own durable typed operation. Queueing atomically changes one
available, failed, or quarantined Snapshot to `deleting`, making it immediately
non-restorable, and reserves the singleton global-heavy lease without taking a
per-instance mutation lease. The broker accepts only the Snapshot UUID, resolves
the trusted root-owned path, and uses descriptor-relative no-follow removal for
that exact UUID subtree. The application independently proves artifact absence
before one transaction deletes Snapshot metadata and terminalizes retained
operation history. Stale or uncertain destructive work is never resumed or
represented as available; it remains reconciliation-required. Web exposes only
explicit confirmation, 202 queueing, bounded status, and list state, with no
retention policy, delete-all, or bulk authority. See
`PHASE7-SNAPSHOTS.md` for the executable contract.

Disposable ManagedInstance deletion is a separate container-first durable
workflow. Its strict Web request requires explicit confirmation and an explicit
preserve-or-destroy Boolean; the server, never the browser, reloads the complete
Snapshot set. Queueing atomically reserves the `INSTANCE_DELETE` per-instance
mutation kind and rejects any active reset, Snapshot operation, transitional
Snapshot, lifecycle, fixture, or revision mutation. The destroy branch also
reserves the singleton global-heavy lease; preservation does not.

After exact container absence is proven, preservation performs one final
transaction that changes each Snapshot source binding from `attached` to
`deleted_source`, clears only the live source FK, records immutable original
UUID/name/deletion time, removes ManagedInstance/Target metadata, copies the
bounded current note once into retained delete history, releases authority, and
terminalizes success. Artifact UUID-derived paths and bytes never move. Source
binding remains separate from artifact validity. Detached Snapshots are
archive-only and reject restore before any lease, Docker, or broker activity.

Destruction iterates the authoritative terminal Snapshot set in UUID order only
after container absence. Under both leases it marks one row deleting, invokes
the existing UUID-only broker removal, proves fixed artifact absence, then
removes that row and records progress. It creates no nested Snapshot delete
operations. Thus each artifact precedes its metadata, and final instance
metadata removal occurs only after all enumerated rows and artifacts are proven
absent. Stale destructive work is never automatically resumed; bounded
read-only reconciliation exposes state without repair authority.

Terminal reset and Snapshot operation histories keep immutable ManagedInstance
UUID identity without a mandatory live FK. The dedicated delete history also
retains revision/fixture identity, policy, counts, prior container state, and
safe reconciliation fields. ManagedInstance notes are exact nullable strings
bounded to 4,096 Unicode code points and copied once only at successful metadata
deletion. Global authenticated preserved browsing is metadata-only; its Delete
action reuses artifact-first Snapshot deletion and offers no restore, content,
path, export, import, or clone capability.

Phase 7 coordination recovery is cross-kind rather than coordinator-local.
One closed application dispatcher examines only current reservation rows and
their typed owners at startup and before reservations. It releases an expired
mutation/global-heavy lease only after matching kind, operation UUID, instance,
expiry, and token digest are all proven; dual-lease operations require both
halves to agree in the same SQLite transaction. Corrupt or orphaned ownership
is retained and fails closed. No stale path invokes destructive infrastructure
or resumes a job.

Released coordination does not imply safe mutation. Typed unresolved operation
records feed a central `ManagedInstanceReconciliationStatus` gate. Reset,
restore, and instance-delete uncertainty blocks all instance mutations. Create
reconciliation is phase/source-state aware: publication-only quarantine with a
proven recovered source does not over-block unrelated lifecycle, while unknown
source recovery does. Snapshot delete and create quarantine additionally use a
Snapshot-scoped status. Multiple records fail closed.

The Web uses these DTOs as read-only projections. Warnings expose bounded
semantic state and blocked actions, never raw database models, Docker identity,
lease material, paths, broker responses, or Snapshot contents. There is no
resolution mutation in this slice. Ordinary terminal operation history is
bounded to 20 per original instance and type; active and
reconciliation-required records are never pruned, and instance-delete history
remains unbounded.

## Typed lifecycle history and activity presentation

Provision, start, stop, and deprovision remain synchronous methods on the
existing runtime lifecycle service. Each request acquires the existing closed
`lifecycle` mutation authority and creates one
`ManagedInstanceLifecycleOperation` before broker mutation. While that exact
lease is held, trusted current inspection records the prior semantic state; the
service then uses its existing broker path and records the resulting semantic
state. Success requires stopped after provision, running-usable or
running-not-ready after start, stopped after stop, and unprovisioned after
deprovision. Known-safe failures and ambiguous outcomes remain distinct.

Lifecycle persistence contains only closed semantic kind/state values, UUIDs,
the immutable original instance identity, a nullable live FK, bounded name and
safe reason, and timestamps. The FK clears on ManagedInstance deletion while
the original UUID remains. Per original UUID, the newest 20 succeeded/failed
rows are retained; running and reconciliation-required rows are protected.
There is no generic activity table and no queued lifecycle worker.

Expired running lifecycle work joins the existing stale dispatcher. The
dispatcher proves exact row/lease ownership, obtains trusted inventory/process/
readiness observation, terminalizes reconciliation-required, and releases only
that lease without resuming work. For every lifecycle kind, the closed stable
observations unprovisioned, stopped, running-usable, and running-not-ready are
shown as warnings but are not independently mutation-blocking; normal action
preconditions still apply. Transitional or ambiguous observation blocks all
instance mutations.

`ManagedInstanceActivityItem` is an application-only merge over lifecycle,
reset, Snapshot create/restore/delete, and ManagedInstance-delete histories.
It orders by requested time descending, UUID descending, and a fixed source
tie-break and emits only bounded semantic summaries and optional typed
references. Live instance detail shows exact runtime/revision, fixture,
readiness, reconciliation, Snapshot summary, current escaped notes, and ten
recent items. Full live history returns the retained projection without that
ten-item cap.

Deleted-instance discovery is anchored by surviving instance-delete history,
not a tombstone. The authenticated global view groups by immutable original
UUID and exposes historical name, exact runtime/revision, fixture, explicit
Snapshot policy, deletion result, and reconciliation flag without notes.
Deleted detail projects all retained typed operations and links any detached
archive-only Snapshots back to their original history. These views add no edit,
restore, undelete, repair, retry, acknowledge, or finalization authority.
