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

Each revision's create-only `RuntimeExecutionProfile` is also the authority for
numeric runtime filesystem ownership. Required `runtime_uid` and `runtime_gid`
values describe the owner Kodi-managed profile and add-on files require inside
that exact curated environment. They are bounded POSIX IDs and are resolved from
trusted catalog state, never accepted from a browser, application request, or
broker request and never inferred at operation time from a container, image,
archive, Dockerfile, or host identity. The profile is a frozen domain value and
one-to-one immutable row, so ownership changes require a new revision/profile.
Execution profiles are not folded into the separately recorded build-manifest
digest; their complete typed values, now including ownership, are the catalog
identity compared and persisted by the profile repository.

The profile also owns one required closed Python bytecode policy. Historical
profiles use Interpreter default; suppression profiles require Kodi Lab to add
exact `PYTHONDONTWRITEBYTECODE=1` to the managed container before Kodi starts.
This reserved environment key is not a generic environment surface. Provisioning
loads it only from the pinned profile, composes it after image-owned values, and
rejects duplicate/conflicting keys. Profile policy has final precedence for the
reserved key: suppression yields exactly one value of `1`, while Interpreter
default requires absence. Fresh provisioning and runtime observation both prove
the profile-derived state, so an equivalent caller-created environment is not
accepted. Changing this execution behavior requires a new profile and monotonic
RuntimeRevision even when the immutable image is reused.

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
adds no mounts, caller environment, command, entrypoint, privilege, devices,
capabilities, host namespaces, or resource limits. Creation, start, and stop
remain separate explicit operations. The sole trusted environment translation
is the pinned profile's closed Python bytecode policy.

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
identity, fixed non-root runtime UID/GID, internal JSON-RPC endpoint, process
observation kind, readiness probe kind, and time bounds. It is persisted
separately from provenance and has no update API. Provisioning remains UUID-only,
sets the numeric Docker user from that trusted profile, and otherwise inherits
image defaults; it gains no caller-provided Docker fields.

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

## Phase 8 local-directory development-source workflows

Phase 8 as a whole owns uploaded-ZIP installation, copied/synced local
DevelopmentSources, repository URL testing, protected replacement behavior,
and known add-on provenance. The supported v1 DevelopmentSource flow is:
registered source → attachment → explicit copy/sync materialization → inventory
→ optional enablement → optional smoke test. Each transition has independent
authority and evidence; no earlier state implies a later one. Step 1 implements
only the global reusable source catalog required before any ManagedInstance
association or deployment exists.

`DevelopmentSource` is a create-only registration-time observation. Its UUID,
logical name, `local_directory` kind, root-relative locator, parsed add-on ID,
observed name/version, deterministic extension points, and timestamps are
persisted independently of ManagedInstance, runtime, fixture, Snapshot, and
deployment state. Logical name, locator, and add-on ID are independently unique.
The application DTO omits the locator and every absolute host path.

There is no safe default development root. Explicit deployment configuration
supplies one normalized non-root absolute directory. The application validator
opens that root and locator components descriptor-relatively without following
links, then reads only a fixed root-level `addon.xml`. It admits one bounded
non-linked regular UTF-8 file, rejects DTD/entity declarations, parses identity
and observations without executing code, and performs no recursive tree walk.
Read-only revalidation reports current metadata but never changes the stored
observation.

Step 2 adds mode-neutral `ManagedInstanceDevelopmentSource` metadata for
disposable instances. One instance may attach multiple distinct add-on IDs and
one global source may be reused by multiple instances. The association proves
no deployed bytes or current Kodi state. Reset and Snapshot create/restore do
not read or rewrite it. Successful instance deletion explicitly removes its
association rows in the same metadata transaction while retaining the source
catalog and typed history.

Attach revalidates only the confined root and root `addon.xml`, then requires
the current parsed add-on ID to match registration identity. Mutable name and
version changes are accepted without rewriting the catalog. Attach/detach use
the existing per-instance mutation lease and central reconciliation gate but no
global-heavy authority. Their succeeded/failed terminal history is
operation-specific, survives instance deletion through immutable original UUID
plus nullable live FK, and joins the metadata-only activity projection.

Step 3 adds an authenticated read-only Web projection over the catalog and
current attachment metadata. Global list/detail HTML and JSON expose only the
path-free application DTO, registration-time observations, validation time,
and live attachment counts or instance links. Live instance detail lists its
attached sources and existing typed activity remains the only history source.
Deleted instances are absent from current source attachments while retained
instance history continues to show attachment operations.

Ordinary GET requests never open or revalidate a source tree. The UI labels
stored name, version, and extension metadata as observations and states that an
attachment is not deployment. The Web receives neither configured development
root nor relative locator, has no source content/download surface, and gains no
registration, attach/detach, deployment, filesystem, Docker, or broker
authority.

The administrator mutation slice exposes the already-approved catalog and
attachment application services without changing those core semantics.
Authenticated same-origin CSRF-protected `POST /api/development-sources`
accepts only bounded `logical_name` and `relative_locator`, rejects unknown
fields, and returns the existing path-free `DevelopmentSourceRead` observation
with safe registration/validation timestamps. The route never resolves a path
or reads `addon.xml`; the configured root, descriptor-relative validation,
parsed identity, normalization, and duplicate checks remain exclusively in
`DevelopmentSourceAdministrationService` and `DevelopmentSourceValidator`.
Validation and conflict responses expose only closed safe categories and never
echo a locator, configured root, OS/XML error, or source content.

Explicit strict-empty-body attach and detach POSTs accept only ManagedInstance
and DevelopmentSource UUIDs. They delegate to
`DevelopmentSourceAttachmentService`, which retains its descriptor identity
revalidation, disposable-instance restriction, one-add-on-ID rule,
reconciliation gate, and per-instance mutation lease. Attach creates only the
association; detach removes only the association. Neither action invokes the
broker, Docker, Kodi, copy/sync, enablement, smoke, lifecycle, artifact cleanup,
or catalog deletion. Instance detail offers metadata-only advisory attach
choices and detach controls; the POST always revalidates authority.

Registration history remains the create-only catalog row and its
`registered_at` timestamp; no duplicate global activity row is persisted.
Attach/detach continue using existing typed
`DevelopmentSourceAttachmentOperation` history and unified instance activity.
This product-boundary slice needs no schema migration and does not expose the
relative locator on any response or read presentation.

Step 4 adds one stopped-only copy/sync path. A strict empty-body authenticated
POST queues a typed durable operation only for an attached local directory and
an exactly stopped, provisioned disposable instance. It reserves both the
per-instance mutation and global-heavy leases. The semantic broker request
carries only instance, source, expected container, and operation UUIDs; the
broker independently resolves all filesystem, runtime, ownership, destination,
policy, and Docker authority.

The broker captures the tree twice descriptor-relatively and no-follow into a
root-private immutable stage. Only directories and single-link regular files
are admitted under fixed entry, byte, path, depth, component, and manifest
bounds. Exact VCS administration directories are excluded. Canonical source
manifests normalize modes and never preserve source ownership, ACLs, xattrs, or
mtimes. Destination ownership derives from the pinned RuntimeExecutionProfile.

An existing destination is eligible only when a fresh full readback exactly
matches retained successful full-destination evidence for the same trusted
instance/source/add-on/runtime/container context. New and changed files alone
enter the delta; unchanged paths do not, and nothing is deleted. A previously
verified destination entry later absent from source is preserved as
destination-only and remains in the next full destination evidence. Any entry
added or altered outside a verified deployment is unexplained drift and blocks
sync as a replacement conflict. Thus source and destination digests are
deliberately distinct and may differ after no-delete synchronization.

Deployment evidence is separate from attachment and carries explicit verified,
stale, or unknown confidence. Reset, restore, and runtime/container replacement
invalidate before destruction; Snapshot create does not. Typed deployment
history joins activity and survives instance deletion, while current evidence
does not. Partial application and expired work become reconciliation-required;
no operation resumes, repairs, recopies, or rolls back by deletion. Kodi is
never started and JSON-RPC, enablement, recognition, bind mounts, ZIPs,
repositories, watchers, and MCP authority remain outside Step 4.

Step 5 adds live read-only Kodi recognition inventory without changing Step 4.
The source of truth is the complete installed set returned by a fixed
`Addons.GetAddons` request: type unknown, installed true, enabled all, and only
name/version/enabled properties plus Kodi's addonid/type base fields. One
version-neutral adapter covers Kodi 19.5, 20.5, 21.3, and 22.0 Beta 2, strictly
validates the envelope, pagination, every field and Boolean, rejects duplicates
and partial results, and sorts by add-on ID. It uses 64-item pages, a 2,048-item
total bound, 64 KiB per response, three seconds per exchange, six seconds total,
and no redirects.

Inventory first resolves ManagedInstance to its pinned RuntimeExecutionProfile,
then uses existing managed-container inventory and runtime observation to prove
the exact owned container, runtime revision, managed-network endpoint, running
state, healthy process, and internal JSON-RPC port. Browser and DTOs contain no
endpoint, address, port, container/image identity, credentials, path, digest,
or raw Kodi payload/error. The unprivileged application performs the fixed HTTP
exchange; the broker gains no JSON-RPC operation. Current curated runtimes are
unauthenticated internally, while endpoint configuration remains trusted
runtime state rather than caller input.

The normalized live DTO records instance, observation time, Kodi release,
availability/completeness, safe unavailable reason, and bounded ID, name,
version, strict enabled Boolean, Kodi type, and conservative provenance for
each item. Unavailable inventory never prevents independent instance detail
from rendering and never triggers lifecycle or the 90-second readiness wait.
Observations are GET-only, no-store, non-durable, and absent from activity
history; no migration is needed.

Historical DevelopmentSource attribution is deliberately narrower than current
provenance. It requires exact recognized ID/version plus retained successful
same-instance/source/runtime/container evidence whose confidence remains
`verified_at_last_operation`. The UI says `Prior verified copy`; it does not
claim current byte equality or installation. Stale/unknown confidence or any
identity mismatch yields unknown provenance, with optional separate stale
historical wording. Attachment, Snapshot, fixture, repository guess, database,
and filesystem presence do not establish recognition or provenance. Copied,
recognized, enabled, and working remain separate states.

The Phase 8 runtime compatibility correction adds suppression revisions for all
four curated releases while retaining every Interpreter-default revision. They
reuse their predecessor images but carry distinct immutable execution profiles,
revision identities, approval, and validation evidence. Once eligible, ordinary
new-pin selection prefers the higher suppression revision; existing instances
and Snapshots retain their exact old pins. Normal embedded-Python imports on a
suppression revision do not create `.pyc` or `__pycache__` beneath copied source,
allowing a fresh unchanged Step 4 sync to retain exact provenance. Step 4 itself
has no runtime-artifact exemption or destination-adoption rule. Any future
live-source projection may benefit from the same policy, but it is outside v1
and requires a separate containment-safe design.

Copied-add-on enablement is the next narrow Phase 8 slice. It binds one
synchronous mutation to the exact ManagedInstance, attached DevelopmentSource,
retained successful copy identity/version, current RuntimeRevision/container,
and a new fresh complete destination readback. The broker exposes only semantic
instance/source/container identity and derives the fixed destination; it returns
bounded match and descriptor identity data without paths or digests.

Trusted source `addon.xml` supplies extension and dependency declarations.
Service/auto-start extension behavior is rejected. Python execution-capable
sources require the exact current suppression profile, while safe non-Python
sources do not. Source dependencies are cross-checked with fixed
`Addons.GetAddonDetails` data and current complete inventory. Required missing,
disabled, unknown, mismatched, or version-unverified dependencies block before
Kodi mutation; optional missing dependencies are allowed. No dependency is
installed or enabled, and no version ordering is inferred. Kodi's exact
`0.0.0` unset-bound sentinel is treated as no declaration constraint during
cross-checking while the reported value remains preserved. Because the fixed
API exposes no authoritative compatibility Boolean, any actual required
version constraint is version-unverified even when strings happen to match.

The only Kodi mutation adapter builds `Addons.SetAddonEnabled` internally with
the server-derived add-on ID and literal Boolean `true`. Exact live recognition
and version are mandatory. Already-enabled is an idempotent success after all
preconditions and sends no mutation. Otherwise exactly one enable request is
followed immediately by a fixed detail query requiring exact ID/version and
`enabled=true`. Success does not execute the plugin or prove function.

Migration `0022_dev_source_enablement` adds only typed enablement-operation
history plus the per-instance coordination kind. No enabled-state model or
dependency inventory is persisted. Pre-mutation interruption is ordinary
failure; post-mutation uncertainty is reconciliation-required, remains
historically ambiguous, and is never automatically retried or finalized.
Ordinary terminal retention is newest 20 per original instance; active and
unresolved rows survive.

The authenticated same-origin CSRF-protected empty-body enable POST is
synchronous HTTP 200. Instance presentation provides advisory copied,
recognized, enabled, dependency-blocked, provenance, runtime-policy, and
auto-start states, while POST remains authoritative. The explicit state ladder
is: attached; materialized; recognized; enabled; dependencies generally
unproven; working unproven; tested unproven. Disable/toggle, dependency install,
generic enablement, repository work, ZIP installation, bind mounts, and plugin
execution remain absent.

Copied-add-on smoke testing adds one bounded historical observation, not a
durable working-state field. Eligibility is one copied suppression-runtime
Python video plugin with exactly one executable extension, exact `video`
provides, a safe present library, and only passive metadata companions. Prefixes,
mixed executable types, services, scripts, binaries, and modules grant no
authority.

The asynchronous operation holds only the exact instance mutation lease. It
revalidates attachment, retained successful copy evidence, verified confidence,
revision/container, healthy already-running Kodi, recognition and enabled
state, dependencies, and a fresh full destination match. It records dispatch
before the one internally constructed `Files.GetDirectory` request for the
server-derived plugin root, video media, no properties, and one-item limit. No
caller execution parameter or generic JSON-RPC surface exists.

Operation state, overall result, execution result, and post provenance remain
separate. Correlated success or Kodi error always receives post-run exact
readback. Match preserves verified confidence; mismatch makes the source tree
stale; unavailable verification makes confidence unknown. Returned items are
validated in memory and discarded. A correlated Kodi error may therefore be an
execution failure while provenance remains matched.

Timeout or post-dispatch crash is ambiguous because execution may continue
after the 10-second client deadline. History remains reconciliation-required,
confidence becomes unknown, and no match, retry, restart, reset, or lifecycle
automation clears it. While the exact original container runs, conflicting
mutations are blocked except explicit ordinary stop. Once stopped or absent the
warning may be nonblocking, but history is never rewritten.

The strict authenticated mutation returns HTTP 202; bounded status exposes no
content, URI, path, endpoint, digest, raw response, log, or error text. The UI
warns that the managed network is not egress-isolated and plugin code inherits
existing network access. No network or repository control is added.

**Smoke passed is bounded historical evidence that one fixed plugin-directory
invocation completed and preserved deployed-source provenance. It is not a
general working/health guarantee.**

## Uploaded-ZIP enablement

ZIP enablement reuses lower-level Kodi/dependency/readback primitives but
remains authorized solely by the immutable uploaded artifact and its verified
ZIP installation evidence. Authority begins with ManagedInstance UUID plus
AddonZipArtifact UUID; the server resolves successful installation,
ID/version/type, revision/profile/container, normalized payload, ownership, and
fixed destination. DevelopmentSource evidence cannot grant this authority, and
there is no generic add-on-by-ID service.

The synchronous operation requires an already-running healthy disposable Kodi
on the exact suppression revision/container and repeats fresh identity,
recognition, inventory, immutable destination, and dependency checks. Its broker
verifier accepts only instance, artifact, enablement-operation, and exact
container identities and uses complete exact-tree comparison. Change makes the
target evidence stale; unavailable verification makes it unknown. Neither
outcome mutates or adopts bytes.

Artifact declarations and fixed Kodi details must agree. The pure evaluator
allows optional-missing only; required missing, disabled, unknown, mismatched,
or actually version-constrained dependencies block without dependency or
repository mutation. The target mutation is fixed enable-true followed by
exact ID/version/type/enabled readback. Already-enabled performs every check
and emits no mutation.

Migration `0025_addon_zip_enablement` adds separate bounded history and one
per-instance mutation kind, not an enabled-state table. Known pre-mutation
failure is ordinary; possible post-dispatch mutation is retained reconciliation
and makes only target installation evidence unknown. Existing
Snapshot/reset/runtime/container invalidation remains evidence-owned; history
never recreates authority. The strict empty-body HTTP-200 API and UI expose
  safe classifications only.

## Uploaded-ZIP smoke testing

ZIP smoke testing is a separate asynchronous operation rooted only in a
ManagedInstance UUID and accepted AddonZipArtifact UUID. It requires retained
verified installation lineage for the exact current suppression revision and
container, complete live inventory, exact enabled plugin recognition, immutable
dependency cross-check/evaluation, and fresh exact installed-tree verification.
The only invocation is the established bounded `Files.GetDirectory` adapter;
caller URI, route, query, timeout, media, properties, paths, and add-on ID are
unrepresentable.

Migration `0026_addon_zip_smoke_test` adds separate bounded history and the
per-instance `addon_zip_smoke_test` mutation kind without a global-heavy lease.
Correlated errors preserve verified evidence only after matched post-readback.
Tree change makes evidence stale. Timeout, post-dispatch crash, or unknown
post-readback makes evidence unknown and leaves permanent reconciliation history;
the exact running container blocks another smoke while explicit stop remains
allowed.

**ZIP smoke passed is bounded historical evidence that one fixed
plugin-directory invocation completed and the installed tree still exactly
matched the immutable ZIP payload. It is not a general correctness or health
guarantee.** Returned directory objects are discarded after bounded validation.

## Uploaded-ZIP installation first slice

Uploaded ZIPs are immutable `uploaded_zip` artifacts, never DevelopmentSources
or repository/publisher evidence. Authenticated multipart streaming writes only
to UUID-derived private staging. A bounded manual EOCD/central-directory parser
accepts stored/deflate members under D-0161, validates the one-root descriptor
policy, and atomically publishes an immutable original plus a canonical
normalized payload and manifest. Kodi never receives an unvalidated archive.

**ZIP installation in this first slice is Lab-controlled validated
materialization of an uploaded immutable ZIP artifact. It is not Kodi's native
Install-from-ZIP GUI/installer path.** No Kodi install method, repository,
generic JSON-RPC/builtin, GUI automation, caller path, or archive-extraction
authority exists.

The asynchronous import owns the global-heavy lease. Installation owns both
global-heavy and the exact instance mutation lease. It requires a healthy
running disposable suppression runtime, complete live inventory with the ID
absent, and an absent fixed destination. The same operation stops the exact
container, reproves absence, applies a broker-resolved normalized tree using
profile UID/GID, performs exact stopped readback, restarts and waits readiness,
requires exact disabled recognition and unchanged declared-dependency state,
performs exact final readback, and leaves Kodi running.

The authoritative normalized manifest includes every directory and file with
type, normalized mode, size and file digest. Both readbacks also require exact
runtime ownership and no extra entry. Archive modes, UID/GID, timestamps,
ACL/xattrs and special bits have no runtime authority. Any existing inventory
ID or destination blocks; replacement, upgrade, overwrite and byte reapply are
absent.

Uncertainty after the durable application marker leaves the instance stopped
and history reconciliation-required. Bounded observation may classify absent,
exact, partial, recognition, dependency, and container/revision states but
cannot retry, adopt, delete, restart, or mark success. Snapshot restore, reset,
deprovision and runtime reassignment stale current ZIP evidence; artifacts and
typed history survive. Inventory attribution is only historical `Prior verified
ZIP install` when every retained identity matches.

The Web surface is one bounded upload POST and one strict-empty install POST,
both authenticated, same-origin and CSRF-protected with HTTP 202 status. Safe
presentation omits upload filenames, archive members/content, internal paths,
descriptor XML and digests. Accepted artifacts are retained; no browse,
download, delete, enable, smoke, replacement, repository, or bind surface is
introduced.

### Rejected raw-bind design evidence

Raw direct Docker bind of mutable DevelopmentSource host paths is rejected, not
an active Phase 8 implementation TODO. The completed architecture gate showed
that Docker could re-resolve a replaced source-root path and expose outside-root
content; late hardlinks could expose outside-root files; late Unix sockets could
cross the intended host boundary; and VCS metadata became visible to Kodi,
add-on code, and Snapshot capture. Post-provision container recreation also
discarded writable-layer state. A read-only mount prevents container writes but
does not contain host-side namespace mutation. A bind-like workflow may be
revisited after v1 only through a policy-enforcing containment-safe projection
or equivalent mechanism.

## Phase 8 external HTTPS repository testing

Repository registration is global catalog state, while a separate many-to-many
attachment is required for each target instance. The application accepts only
canonical public HTTPS `addons.xml` or `addons.xml.gz` endpoints. Its dedicated
unprivileged fetcher resolves and explicitly classifies DNS answers immediately
before every connection, connects to one validated address, and preserves the
registered hostname for SNI, certificate verification, and Host. Same-host
redirects are bounded and revalidated. Ambient proxy variables, credentials,
caller headers, custom trust, and broker network requests are outside this
boundary.

Explicit refresh publishes an immutable digest-and-aggregate metadata snapshot
and bounded normalized candidates, never raw XML or caller package-URL
authority. Packages use only the server-derived same-origin
`<index-directory>/<id>/<id>-<version>.zip` location. Bounded fetch reuses the
single strict ZIP validator and publishes an immutable `repository_download`
artifact carrying exact source/snapshot/candidate lineage.

Repository installation has separate Web/application authority but shares the
low-level ZIP materializer. It requires attachment, exact lineage, complete
inventory and destination absence, no prior verified installation evidence,
and rechecks after stop. Any attached
same-ID DevelopmentSource blocks before lifecycle mutation and attachment
changes conflict with installation. Uploaded-ZIP enable/smoke APIs remain
closed to uploaded artifacts, so repository testing ends at exact disabled
recognition and historical repository provenance. Reset/restore stale evidence
without deleting repository records. Generic reinstall, update, force, and
collision override remain absent.

### Exact approved repository replacement test

Migration `0028_repository_replacement_override` adds the one non-generic
exception to same-ID DevelopmentSource protection. A server-prepared plan binds
one repository-download artifact to the current source and repository
attachments, candidate and metadata lineage, RuntimeRevision/container, live
incumbent state, and—when a copied destination exists—its exact successful
deployment evidence and a same-revision central Snapshot no more than 15
minutes old. The plan itself expires after 15 minutes. Immediate explicit
approval atomically binds one execution operation; it is consumed at the
application marker and is never reusable.

The only incumbents are an attached source with no destination or inventory
target, and a freshly verified copied source whose full destination and live
ID/version/type/enabled state still agree. Uploaded ZIPs, prior repository
installs, detached, manual, bundled, inventory-only, destination-only, stale,
and unknown state remain excluded. Versions are not semantically ordered and
byte-identical incoming payloads are no-ops, not replacement tests.

For a copied incumbent, every old path must remain in the normalized incoming
manifest with the same object class. The incoming artifact may add paths and
overwrite existing regular-file content, normalized ownership, and modes; it
may not delete an old path or change file/directory type. A dedicated broker
operation resolves the fixed add-on destination and immutable payload from the
approval. Its request has no path, payload, user/group, delete, overwrite,
Docker, or generic-execution authority.

Execution revalidates all persisted, runtime, attachment, inventory,
destination, Snapshot, and lineage preconditions before and after stopping.
The durable consume/application marker also changes copied-source evidence to
unknown. Success preserves the exact enabled Boolean and declared dependency
observations, publishes verified repository evidence, makes source evidence
stale, restarts, and retains the unchanged source attachment. Active override
is derived from the retained same-ID attachment plus exact current verified
replacement evidence, and copy/sync is explicitly blocked as
`repository_override_active`.

Pre-marker failure invalidates the approval and restores running state if the
container had stopped. Any uncertainty after the marker leaves the exact
container stopped, both evidence classes unknown, and the dedicated operation
reconciliation-required. No retry, adoption, continuation, automatic restart,
or automatic Snapshot restore occurs. Restoring the bound central Snapshot or
resetting the disposable instance and copying afresh are the only first-slice
exits; detach does not restore bytes. Snapshot restore stales repository
evidence and does not reattribute restored DevelopmentSource bytes.

The private Web flow prepares with a strict empty body and then accepts only
`{"confirmed": true}` for immediate approve-and-execute. Safe review/status
projects logical names, add-on identity, versions, class, enabled state,
Snapshot readiness, and bounded effect/rollback wording. Paths, manifests,
digests, URLs, container identities, internal Snapshot paths, and raw errors
remain outside Web output.

Central Snapshot capture canonicalizes Docker's unordered archive enumeration:
two passes must contain the same bounded approved inventory, regular-file bytes
are privately spooled under the artifact staging identity, and publication
emits sorted normalized members. The disk-reserve check accounts for spool plus
final archive. This preserves deterministic artifacts without treating Docker
member order as caller or filesystem authority.

## Phase 9 protected agent workspace foundation

Phase 9 Slice 1 adds an unprivileged application-only workspace catalog. The
only source is a registered `local_directory` DevelopmentSource. The original
source remains outside agent mutation authority; creation performs a bounded
read-only capture beneath the configured development root. There is no broker,
Docker, container, Kodi, Git, mount, ZIP, repository-artifact, or caller path
in the workflow.

The application derives
`KODI_LAB_DATA_ROOT/agent-workspaces/<workspace-uuid>/` and privately retains
`baseline/tree`, `baseline/manifest.json`, and mutable `tree`. The first
normalized no-follow capture is checked against a second source pass, its
captured `addon.xml` must retain the registered add-on ID, and generation zero
is made only from those captured bytes. Independent recapture proves baseline
and tree equality before the catalog becomes ready. Creation is a typed
global-heavy operation; exact pre-publication failure is cleaned, while any
post-publication uncertainty is retained for reconciliation.

Internal read services accept only workspace UUID plus normalized semantic
relative paths. Directory results are bounded to 2,000 safe entries. Content
authority is strict UTF-8 text only, at most 2 MiB per request. Every traversal
opens components descriptor-relatively with `O_NOFOLLOW` and rejects links,
hardlinks, and special objects, including unsafe entries injected after
creation. Safe Web DTOs omit storage/source paths, baseline identity/digest,
manifest, ownership, inode, and raw filesystem errors.

This slice intentionally has no file create/write/replace/patch/delete/rename,
no diff or promotion, no source mutation, no Kodi deployment/reload/test/log
operation, no MCP exposure, no cleanup mutation, and no Git operation.

## Phase 9 bounded workspace editing and derived changes

Slice 2 makes only the private workspace tree mutable. Semantic application
services create an absent UTF-8 file beneath an existing directory, replace an
existing safe UTF-8 regular file, apply an exact full-file preimage patch, and
derive added/modified changes against the immutable baseline. Inputs contain a
workspace UUID, normalized relative path, expected generation, and bounded text;
they contain no source/storage path or infrastructure/runtime parameter.

Every mutation takes a server-derived cross-process lock for that workspace,
requires ready state and exact generation/current-tree identity, then performs a
complete bounded descriptor-relative recapture. Workspace recapture rejects VCS
administration directories instead of applying source-capture exclusion, so an
injected `.git`, `.hg`, or `.svn` object cannot disappear from integrity
evidence. Only an absent target or existing single-link regular file is valid.
New files use 0644; replacements retain the current normalized file mode.

Complete UTF-8 content is capped at 2 MiB and fsynced to a private
same-directory temporary inode. Linux no-replace rename publishes creates;
ordinary atomic rename publishes replacements and patches. A private preimage
link remains outside the mutable tree until full post-write recapture and
conditional database publication complete. Known failure restores the exact
preimage (or removes only the exact just-created postimage) and proves the old
tree digest. Unprovable recovery makes the workspace
`reconciliation_required`; no unknown filesystem state is adopted.

The database compare-and-swap increments generation once and publishes the
server-captured current-tree identity together with content-free mutation
activity. The activity records only workspace/event UUIDs, generation,
create/replace/patch kind, normalized relative path, and time. It is synchronous
history, not an asynchronous operation or generic event payload.

Change derivation recaptures both baseline and current trees and requires their
durable identities. It omits unchanged entries and returns added/modified file
metadata plus at most 16 KiB of UTF-8 before/after preview per side. Missing
baseline entries, added directories, type transitions, invalid descriptor state,
changed binary files, or external current-tree drift are integrity failures.
A valid root `addon.xml` ID change is retained for experimentation but appears as
`identity_changed`; immutable source provenance is never rewritten and later
promotion must refuse it.

Authenticated same-origin CSRF-protected JSON create/replace/patch routes carry
paths in strict bodies, not URL path captures. Authenticated change and activity
GETs plus the administrator detail page expose bounded relative paths and text
previews but no host/source path, internal digest, ownership, inode, temporary
name, or raw error. There remains no promotion, DevelopmentSource mutation,
backup/apply workflow, Kodi integration, MCP exposure, cleanup, deletion,
rename, mkdir, shell, Docker, broker, or Git capability.
