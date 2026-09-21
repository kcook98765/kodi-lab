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

## Phase 9 reviewed canonical-source promotion

Slice 3 adds a private, administrator-reviewed three-way transition from one
ready AgentWorkspace into its registered canonical DevelopmentSource. Planning
fully recaptures immutable baseline, current workspace, and current source. It
admits only added or byte-modified UTF-8 regular files, capped at 100 paths,
2 MiB per text file, and 4 MiB aggregate complete before/after review text. No
truncated preview can be approved. Modifications require exact source-baseline
agreement; additions require source absence and safe existing parents. A source
tree may have unrelated safe changes, but any unsafe object anywhere blocks.

Plans are private, expire 15 minutes after becoming reviewable, and bind exact
workspace generation/tree, baseline, source capture, immutable add-on ID,
affected pre/post identities, one eligible running disposable target, its
RuntimeRevision/container, and exact retained verified source deployment.
Browser projections expose only logical names, safe UUIDs, add-on/version
metadata, changed relative paths, full review text, bounds, and rollback/staging
notices. Approval accepts only `{"confirmed": true}` and creates exactly one
source operation. Workspace/source/path/runtime/postimage authority is never
caller-selectable.

The ordinary application account must have actual write/search permission on
the registered source root and affected parents. Registration grants nothing.
Any DevelopmentSource directory provisioned writable for promotion becomes part
of the Kodi Lab application trusted-computing base. Registration alone is read
trust; an administrator separately provisions promotion OS permissions. Kodi
Lab never chmods or chowns source trees. Compromise of the application service
account could modify any promotion-enabled source writable by that account, so
ownership and ACLs must be least-privilege and promotion enabled only where
intended.
Execution repeats complete no-follow source validation and workspace identity
checks, then publishes and verifies an operation-bound private exact backup.
After a durable application marker consumes approval, fixed paths apply in
UTF-8 lexical order. Linux `renameat2` no-replace publishes additions;
`RENAME_EXCHANGE` publishes modifications and exposes the displaced inode for
exact preimage proof. Lack of those primitives fails closed. No parent is
created and no Git, shell, broker, Docker, or Kodi facility participates.

A content-free durable journal records each path as not attempted, applied,
rolled back, or uncertain. Failure rolls applied paths back in reverse order,
conditional on exact operation postimages; operation-created additions have the
only narrowly scoped unlink. Full source recapture must prove the original
affected state. Unproven state becomes reconciliation-required and is neither
retried nor adopted. Startup closes a pre-marker interruption as failed and
marks every post-marker interruption unresolved.

Success writes a private exact selected-target sync handoff before making all
prior deployment confidence for the source stale. It then marks the workspace
`promoted`; only `ready` workspaces can mutate, so promoted, creating,
abandoned, and reconciliation-required trees are immutable. Historical
enablement/smoke/deployment rows are untouched. The handoff does not confer
generic deployment confidence or browser copy authority.

Migration `0031_agent_workspace_promotions` stores private plans/review paths,
single-use source operations, per-path journal, source lease, target handoff,
the promoted workspace state, and promotion coordination kinds. Per-workspace,
per-source, selected-target, and global-heavy coordination follows a fixed
plan/operation → target → global → source acquisition order.

## Phase 9 promotion-bound Kodi verification

Slice 4 implements D-0209 as a separate durable
`PromotionVerificationOperation`. Source terminal success, handoff publication,
verification-child creation, exact reservation, and queued coordination commit
as one database publication boundary. Source success therefore cannot commit
without its required child. The promotion service cannot be constructed
without the publication collaborator, and later dispatch only executes or
recovers the durable child; it never creates a missing child after success.
Execution starts afterward and never reopens
approval or rewrites `AgentWorkspacePromotionOperation`. Preflight binds the successful
promotion, promoted workspace, attached DevelopmentSource, disposable target,
RuntimeRevision, immutable container, prior destination proof, promoted
metadata, and live pre-stop enabled Boolean. Drift ends before target mutation.
The catalog `ManagedInstance.runtime_state` is not live Docker authority; the
exact broker inventory and immutable container precondition are.

Queueing durably reserves the handoff to the sole verification UUID but leaves
`consumed_at` and the operation's consumption marker unset. The reservation is
non-reusable across deterministic failure, ambiguity, or recovery. Preflight,
cursor capture, stop, and the promotion-bound broker sync all require that exact
reserved-but-unconsumed state.

The operation acquires the target mutation lease and global-heavy lease in the
existing order. A semantic broker cursor captures only fixed
`/home/kodi/.kodi/temp/kodi.log` and `kodi.old.log` identity, then the operation
stops the exact container. A handoff-only broker resolver feeds the existing
changed/new-only copy core with the prior verified destination proof despite
the intentional promotion staling transition. Exact sync publishes a new
ordinary verified deployment row and, in the same database transaction,
permanently consumes the handoff. Any earlier deterministic failure remains
reserved and unconsumed; mutation ambiguity remains reserved, unconsumed, and
reconciliation-required rather than becoming reusable. The same container
starts and uses normal readiness; no recreate, enable mutation,
shell, or arbitrary JSON-RPC occurs.

Inventory is authoritative for ID, promoted version, supported type, and exact
enabled preservation. Ten seconds after readiness, the broker correlates at
most 1 MiB of new data across append or normal `kodi.log` to `kodi.old.log`
rotation. Correlation ambiguity is `incomplete`. At most twenty sanitized
512-byte target snippets and 8 KiB total reach Web status. The complete
attributable raw buffer, bounded by the 1 MiB acquisition ceiling, is decoded
and deterministically redacted before control normalization, the 40 KiB
retained-evidence cut, and public output truncation, so truncation cannot expose
a token fragment that escaped matching. This covers common Bearer,
key/token/password,
URL-userinfo, and obvious token-prefix forms but is not general DLP. Raw bytes
are transient inside the private broker boundary; retained evidence and normal
DTO/history surfaces contain only redacted snippets. Target-correlated
Python/import/dependency/service failures fail verification; unrelated
DBus/VDPAU/Zeroconf warnings are counted separately. Logs never substitute for
inventory evidence.

Migration `0032_promotion_verification` stores stage markers, separate sync,
reload, inventory and log outcomes, single-use handoff binding, bounded counts,
a separate private bounded-snippet evidence row, and verification coordination
kinds. Filesystem or lifecycle
uncertainty after mutation is reconciliation-required and does not retry or
roll back the already promoted source. Canonical patch-back is complete; Phase
9 cleanup/abandon/retention remains separate work. MCP exposure remains its
later Phase 11/12 milestone and is not a Phase 9 completion blocker.

## Phase 9 explicit workspace abandonment and byte cleanup

Slice 5 completes D-0210 without conflating lifecycle with retained storage.
Ready workspaces may be explicitly abandoned only while promotion planning and
execution are inactive. Promoted workspaces remain promoted and cannot be
abandoned. Both outcomes are immutable; reconciliation-required workspaces are
protected from both actions. `AgentWorkspace.bytes_state` separately records
`present` or `cleaned`, with promoted, abandoned, and cleanup timestamps retained
for path-free list/detail projection.

Cleanup is an authenticated, confirmed, UUID-only durable operation and owns the
global-heavy lease. Eligibility requires abandoned or promoted lifecycle,
terminal creation and promotion, terminal verification for promoted workspaces,
no active plan or operation, and no reconciliation outcome. Expired leases do
not weaken those checks. There is no caller path, bulk selector, broker, Docker,
Kodi, Git, shell, root helper, source write, or automatic scheduler.

Before deletion, the operation records the no-follow device/inode identity of
the exact server-derived `agent-workspaces/<workspace-uuid>` root. A
descriptor-relative walk stays on that device, opens directories with
`O_NOFOLLOW`, and unlinks non-directory entries—including injected links,
hardlinks, FIFOs, sockets, or devices—without following them. It removes the
complete baseline and editable tree, retains the storage parent and sibling
workspaces, fsyncs deletion, and verifies exact subtree absence. A mount/device
boundary or root substitution fails closed.

Pre-marker interruption is an ordinary failure. Post-marker interruption may
resume only as the same operation against the persisted exact root identity;
absence is accepted only with that durable marker. Contradiction becomes
reconciliation-required. Successful repetition returns the one retained cleanup
operation and does no second deletion.

Migration `0033_agent_workspace_cleanup` adds independent byte state,
lifecycle timestamps, typed cleanup operations, content-free lifecycle events,
and the cleanup global-heavy kind. Workspace metadata, provenance, final
generation, mutation history, promotion plans/operations/journal/handoff,
verification and bounded log evidence, and private source-promotion backup are
not workspace bytes and are retained. V1 has no TTL, background or disk-pressure
pruning, cleanup-all, or automatic cleanup after abandonment or promotion.

Migration `0034_phase9_completion_remediation` makes the four Phase 9 live
ManagedInstance links (plan, source operation, handoff, and verification)
nullable while retaining immutable original target UUIDs and the plan's bounded
target-name snapshot. The centralized instance-deletion finalizer detaches only
terminal history. Active or reconciliation-required Phase 9 work still blocks
deletion, and workspace, source, backup, handoff, verification, and log history
survive target deletion.

**Phase 9 — Agent Working Copy: COMPLETE.** The final formal review closed Phase
9 at implementation HEAD `e25a502597b94ffa46f0710059f9a09cc0d6086b`, Alembic
head `0034_phase9_completion_remediation`, and 1770 collected tests. Mandatory
atomic verification publication, full-buffer secret redaction before retained
truncation, terminal target-history retention, source-promotion TCB disclosure,
and non-root validator hygiene are part of the completed boundary. MCP/Git
integration and the broader optional editing, execution, retention, and rollback
features remain later work rather than Phase 9 requirements. Phase 10 begins
below with the architecture-reviewed screenshot-only Slice 1 boundary.

## Phase 10 Slice 1 managed screenshots

Phase 10 is canonically **Browser remote/display**, but Slice 1 implements only
the mandatory screenshot foundation for exact Kodi 21.3 stable. The Web
administrator supplies one ManagedInstance UUID. Application status requires
the pinned release, exact healthy running container, usable Kodi readiness, and
absence of a reconciliation blocker. Capability remains distinct from this
momentary eligibility; capture never provisions or starts an instance.

The broker operation `capture_managed_kodi_screenshot` accepts only the
server-bound instance, expected container, expected RuntimeRevision, and capture
UUID. The pinned image's `x11-utils` package does not contain `xwd`, so its
implementation is a fixed isolated Python X11 `GetImage` operation against the
Xvfb `:99` root window.
Raw framebuffer data is too large for the 65,536-byte framed JSON protocol, so
the broker writes it to a capture-UUID-derived private staging file and returns
only path-free semantic metadata. Pre/post selection verifies exact labels,
revision, identity, running state, and Docker health. No generic exec or
caller-controlled process/path/display/format/timeout is present.

The unprivileged application parses only the expected bounded 1280x720 RGB
layout, converts it with the Python standard library, and validates a canonical
RGB8 PNG end to end. Slice-1 geometry is exactly 1280x720; PNG output is capped
at 8 MiB. The latest PNG is atomically placed in the private screenshot cache.
Migration `0035_managed_screenshots` persists only one current binding per live
instance plus last-attempt classification. Successful replacement publishes the
new binding before removing the old bytes. Failure never changes current
capture metadata or bytes. Missing bytes become safe unavailability without
automatic recreation.

Authenticated APIs provide visual status, CSRF/same-origin protected manual
capture, and authenticated latest-image delivery with `image/png`, `no-store`,
and `nosniff`. The instance detail page shows capability, availability, image,
UTC timestamp/server-derived age, dimensions, byte size, a manual Refresh
button, and bounded failure state. There is no automatic refresh. Centralized
instance deletion removes cache bytes and metadata; screenshots do not become
history. Bounded structured audit events retain only semantic identities,
timestamps, outcome, dimensions, and byte count.

Per-instance exclusion and a four-capture global semaphore bound observational
work. Lifecycle identity is revalidated immediately before publication. Slice 1
adds no durable operation handle, remote control, input, interactive display,
MCP, agent identity/authority, general logs, workspace exposure, source
promotion, Git, shell, or Docker surface.

## Phase 10 Slice 2 managed screenshot runtime parity

Slice 2 completes the mandatory common screenshot abstraction across the four
current curated RuntimeRevisions: Kodi 19.5 revision
`51950000-0000-4000-8000-000000000005`, Kodi 20.5 revision
`52000000-0000-4000-8000-000000000005`, Kodi 21.3 revision
`3a000000-0000-4000-8000-000000000006`, and Kodi 22 Beta 2 revision
`52200000-0000-4000-8000-000000000005`. Capability resolution loads the exact
pinned revision and its immutable execution profile, then matches that closed
set. A version string, nearby revision, or lookalike profile is insufficient.

All four pinned images use the fixed non-network Xvfb `:99` root at 1280x720x24.
The broker-owned X11 program now reads the server setup's root depth, pixmap
format, image byte order, scanline padding, root visual class, bits-per-RGB and
channel masks. It accepts only TrueColor depth 24, 32 bits per pixel, 32-bit
scanline padding, RGB888 masks and a fully consistent row length. It writes a
private versioned raw-frame envelope containing those observed fields plus the
bounded framebuffer bytes. The unprivileged application independently parses
that envelope, supports the closed LSB/MSB byte-order variants, and creates the
same canonical RGB8 PNG. Unknown geometry or layout fails closed rather than
being guessed or rescaled.

Controlled sequential live validation proved all four exact revisions expose
the same 1280x720, LSB-first, TrueColor 24/32-bpp RGB888 layout. Each produced a
strictly valid, non-uniform initialized PNG, authenticated delivery, and atomic
latest-only replacement. Kodi 20.5 additionally proved that an injected refresh
failure retains the prior UUID, timestamp, metadata and bytes. No permitted
preexisting deterministic UI mutation exists, so freshness validation used two
independent captures without adding browser-remote authority; identical hashes
are expected for an unchanged static framebuffer.

There is no schema change: migration `0035_managed_screenshots`, cache,
coordination, deletion, Web routes and UI remain runtime-neutral. Slice 2 adds
no remote action, auto-refresh, interactive display, noVNC/VNC, keyboard input,
MCP, token, agent, workspace, general-log, lifecycle, shell, Git, or generic
Docker/X11/filesystem authority. The mandatory screenshot portion of Phase 10
is complete; browser remote and the optional mediated interactive display remain
separate later slices.

## Phase 10 Slice 3 fixed browser remote

Slice 3 implements D-0214, D-0215, and the remote subset of D-0218 for the four
exact curated Kodi revisions. The authenticated same-origin, CSRF-protected Web
route accepts a ManagedInstance UUID and one closed action enum. Browser code
never contacts Kodi, managed-network addresses, container ports, X11, or VNC
and has no method, params, player-ID, endpoint, timeout, key, text, path,
command, or shell authority.

`BrowserRemoteService` proves running/healthy/usable state, no reconciliation
blocker, a short per-instance `browser_remote` lease, exact revision/profile,
and unchanged container identity. It builds one fixed bounded JSON-RPC request.
Navigation maps to nine `Input.*` operations; volume/mute to fixed
`Application.SetVolume` increment/decrement and `Application.SetMute` toggle.
Player actions first use fixed `Player.GetActivePlayers`; video, audio, picture
priority and lowest-ID tie-breaking derive the player entirely server-side.

The control request is attempted once with no retry. Uncertain acknowledgement
is `remote_action_ambiguous`; Kodi rejection or an invalid operation-specific
acknowledgement is `remote_kodi_rejected`. Raw responses never escape. The Web
response contains only invocation/instance UUIDs, action, semantic result,
completion time, and a separate screenshot-refresh outcome.

Successful navigation immediately reuses `ManagedScreenshotService` while the
remote lease remains held. The fixed order is mutation lease, remote dispatch,
screenshot coordinator; screenshot capture never takes the mutation lease.
Refresh failure preserves the prior cache and remote success. Playback/volume
does not refresh because canonical V1 requires navigation refresh and capture
would add latency to rapid media controls.

The UI renders fixed controls only for a currently usable exact curated revision
and updates the image without a page reload. A per-session/per-instance
in-process token bucket enforces 10 actions/second, burst 20, before dispatch.
Buckets use monotonic activity, opportunistic 15-minute idle expiry, explicit
logout retirement, and a hard 4,096-bucket ceiling. A new key is rejected before
dispatch when the ceiling is full of non-idle buckets, so active enforcement is
not weakened. There is no migration or durable remote history.

Controlled sequential live validation proved the common mapping on Kodi 19.5,
20.5, 21.3, and 22 Beta 2. All 16 actions were accepted with no version-specific
mapping: the nine navigation actions succeeded and each caused exactly one fresh
screenshot UUID and timestamp; volume and mute succeeded; and all four
player-relative actions returned `remote_no_active_player` for the intentionally
idle fixture. PNG hashes changed for visible navigation transitions without OCR.
Each disposable instance was deleted, the managed network was left empty,
production roots remained absent, and protected fingerprints were unchanged.
Mediated interactive display remains unimplemented.

## Phase 10 Slice 4 mediated Kodi 21.3 live display

Slice 4 implements D-0217 and the display subset of D-0218 as one authenticated,
same-origin, view-only WebSocket carrying validated binary PNG frames. Capability
matches only exact RuntimeRevision `3a000000-0000-4000-8000-000000000006` and
its immutable Kodi 21.3 stable execution profile. Other curated runtimes return
`display_unsupported` even though their manual screenshot and semantic remote
capabilities remain complete. The visual projection exposes only
`display_supported`, `display_available_now`, and
`display_backend=live_screenshot_stream`.

`ManagedLiveDisplayService` binds the instance, revision, profile, and exact
managed container identity. It requires running, healthy, usable,
unreconciled state and revalidates those facts plus the authenticated server
session once per frame interval. A process-local registry admits one stream per
instance and two globally, assigns a non-credential display-session UUID,
enforces a 15-minute hard lifetime, and provides cancellation. Central mutation
reservation signals cancellation before lifecycle, reset/restore, deletion,
runtime reassignment, deployment/install/replacement, or promotion verification
proceeds. Display never holds a durable mutation lease and never causes a
lifecycle operation to wait. Once registry capacity is acquired, WebSocket
acceptance and every later exit share one cleanup scope; acceptance failure,
setup failure, normal close, stream failure, and cancellation release the slot
exactly once. Session-start audit is deferred until acceptance succeeds.

The display backend calls the existing `capture_managed_kodi_screenshot`
operation. `ManagedScreenshotService` now has one shared private capture path
that performs the broker handoff, reads the UUID-derived raw artifact, uses the
same X11 decoder and PNG encoder, strictly validates the 1280×720 RGB8 PNG, and
revalidates container identity. Durable manual refresh publishes that result as
before. Ephemeral display capture instead returns the validated bytes in memory
and always removes raw staging. It creates no screenshot row, durable PNG,
cache replacement, frame history, or per-frame audit event.

The WebSocket accepts no query options or credentials and no browser messages.
Only binary PNG travels server-to-browser. Capture, validation, one send with a
two-second timeout, and monotonic scheduling are sequential: at most one frame
is in flight, queued-frame capacity is zero, rate is at most one frame/second,
and slow work reduces the effective rate without catch-up. Logout, password
change, idle/absolute expiry, session deletion, reconciliation, lifecycle
cancellation, support/profile change, container replacement, or runtime
unavailability terminates the exact stream without retry or adoption.

The instance page continues to default to the independent latest durable
screenshot. Live display starts only by explicit action, is labeled
`Live · up to 1 fps`, leaves the existing semantic remote visible, and restores
the latest screenshot on stop/error. The frame has no keyboard focus, click, or
pointer behavior. Remote actions continue through the existing authenticated
HTTP route; navigation keeps its one durable post-action screenshot refresh and
later live frames reflect the changed Kodi framebuffer.

Session-level audit records only bounded UUIDs, Web/local-admin markers,
timestamps, safe end reason, frame count, and aggregate bytes. There is no
migration, new broker operation, VNC/noVNC, keyboard/pointer/text input, image or
runtime rebuild, MCP/token/agent authority, general log surface, higher-rate
video, or display parity beyond Kodi 21.3. With four-runtime screenshots,
four-runtime semantic remote, and this mediated display path implemented, the
required V1 Phase 10 scope is implementation-complete pending formal review.

## Phase 10 formal completion

**Phase 10 — Browser remote/display: COMPLETE.** The final formal completion
review closed Phase 10 at implementation checkpoint
`9821463b22c1d03a619571455f24e22853c0134e`, Alembic head
`0035_managed_screenshots`, and 1884 collected tests. Managed screenshots and
the closed semantic browser remote are live-proven across Kodi 19.5, 20.5,
21.3, and 22 Beta 2. Exact curated Kodi 21.3 additionally provides the required
explicit, view-only, same-origin mediated live-display path.

The completed boundary includes a monotonic 15-minute idle TTL and hard
4,096-entry ceiling for session-plus-instance remote rate buckets, plus
exception-safe display capacity from acquisition through WebSocket acceptance
and teardown. The browser cannot reach Kodi JSON-RPC, X11, VNC, or a container
display directly; cannot choose Kodi methods, parameters, players, screenshot
commands, paths, display backends, rates, formats, or timeouts; and cannot send
keyboard, pointer, text, or control messages over the display WebSocket. Live
frames are validated PNG bytes held only in memory after private raw staging is
removed, and they neither queue nor alter the durable latest-screenshot cache.

Optional future visual features remain non-blocking: approximately three-second
screenshot auto-refresh, keyboard/full-text input, live-display parity for
additional runtimes, VNC/noVNC, and higher-frame-rate video. MCP transport,
tokens, client/agent identity, target selection, schemas/tools, and workspace
exposure remain Phase 11/12 work and were not introduced by Phase 10.

## Phase 11 Slice 1 modern authenticated read-only MCP foundation

Kodi Lab now hosts exact POST `/mcp` as an ASGI route in the existing FastAPI
lifespan and listener. The outer MCP adapter admits only protocol
`2026-07-28`, JSON request bodies up to 64 KiB, a valid Bearer credential, and
bounded request/concurrency capacity before entering the exact-pinned official
SDK. The SDK performs modern per-request envelope/header validation and
dispatch; Kodi Lab strips any session header defensively and creates no
transport-session or default-target state. `server/discover`, `tools/list`, and
`tools/call` are independent requests. SDK legacy auto-mode cannot bypass the
outer exact-version gate.

`mcp_administration` owns durable clients, verifier-only credentials, immutable
built-in presets, open-ended preset-permission strings, exact managed-target
grants, target preset assignments, and content-free security-administration
events. Browser routes are authenticated Web-admin-only mutations protected by
the existing Host, Origin, session, and CSRF controls. Token plaintext exists
only in the issue/rotation response and transient page DOM; list/detail and a
reload cannot recover it.

The transport-neutral MCP registry is static server code. Every definition
owns permission, effect, target-class eligibility, capability/state gates, rate
class, adapter identity, production eligibility, and target-scope metadata.
`McpAuthorizationService` recomputes durable authority and invokes an adapter
only after the full intersection passes. Consequently a later tool normally
adds one approved Kodi Lab service, compact schema, registry definition,
bounded output/error mapping, and tests; it does not add authentication,
transport, grant, or preset architecture.

The only adapters are `target_list` and `target_status`. Both call the existing
`ManagedRuntimeStatusService`; neither imports Docker, broker, X11, raw Kodi
HTTP/JSON-RPC, or host filesystem code. Discovery is private/immediately stale,
lexical, and filtered only by target-independent client permission. Target
grant/policy/capability/current-state checks occur at call time.

Current target grants and policy assignments reference `managed_instances`,
which deliberately makes unsupported external classes unrepresentable in
Slice 1. The existing `targets` table is already the stable supertype. Phase 13
can rebuild those association foreign keys to `targets.id`, retain every
existing managed UUID, add external-development subtype storage and authority
providers, and leave all MCP client/credential/preset identities unchanged.
Phase 14 can add external-production subtype and production-safe policy. Static
production definitions currently allow only `internal_lab`, and class is never
sufficient without an exact grant and both preset sides.

Structured per-call logs contain only client/credential/tool/optional target
UUIDs, timestamps, and a safe outcome. Durable administration events contain
only bounded semantic UUIDs and event kinds. Tokens, hashes, cookies, arbitrary
arguments/text, file/image content, raw Kodi data, endpoints, and paths are
excluded.

Slice 1 uses Kodi Lab MCP schema `1`, distinct from MCP protocol
`2026-07-28` and package version. It adds no stdio, `2025-11-25`
compatibility, screenshots, remote control, action receipts, lifecycle,
snapshots, logs/diagnostics, raw JSON-RPC, external targets, production
connectivity, or Phase 12 workspace/development tools. Phase 11 is not complete.

Protocol validation uses the official conformance runner pinned to
`@modelcontextprotocol/conformance@0.2.0-alpha.11`. A test-only ASGI wrapper
injects one issued bearer because that runner has no arbitrary-header option;
it does not bypass any MCP protocol/header/dispatch behavior and is not shipped
in the application package. Only Slice-1-relevant stateless, tool-list, cache,
header-routing, and DNS-rebinding checks are in scope. Fixture-only sampling,
subscription, prompt/resource, media-content, MRTR, OAuth-client, and later
catalog scenarios are reported as exclusions rather than implemented.

## Phase 11 Slice 2 canonical stdio proxy

Canonical stdio is installed as `kodi-lab-mcp-stdio`. It is an unprivileged,
ephemeral protocol adapter, not a second MCP server or authority implementation:

```text
MCP client -> stdio adapter -> authenticated loopback HTTP POST /mcp
           -> existing authentication -> authorization -> registry -> service
```

The adapter imports no Kodi Lab persistence, repository, application service,
broker, Docker, Kodi, or tool-definition module. It reads the same durable MCP
bearer from `KODI_LAB_MCP_TOKEN`, attaches it to every HTTP request, and neither
decodes authority from the token nor caches authentication or authorization.
The optional `KODI_LAB_MCP_URL` development override remains exact HTTP
loopback `/mcp`; remote forwarding, redirects, URL credentials, query tokens,
and arbitrary paths are rejected.

The public stdio wire is also exactly `2026-07-28`. Requests preserve their
modern envelope and are given only the matching HTTP routing headers needed by
the pinned SDK. There is no initialize/initialized exchange, legacy fallback,
`Mcp-Session-Id`, GET stream, selected/default target, or transport session.
Official pinned clients prove `server/discover`, `tools/list`, `target_list`,
and `target_status` through the subprocess path. HTTP remains the source of all
tool schemas, ordering, bounded tool results, and authorization errors.

Input framing is newline-delimited and bounded to the HTTP 64 KiB request
limit before JSON parsing. Four request workers consume an eight-item
backpressured queue and correlate replies by JSON-RPC ID. This small process
safety bound does not replace HTTP per-client/global admission. EOF cancels the
task group and active HTTP calls; no daemon, child, durable state, or background
process survives.

The server does not claim a distinct `mcp_stdio_proxy` audit classification:
there is no authenticated server-derived channel for it in this topology, and
a caller header would be spoofable. Existing server audit remains permission
neutral. Transport type does not imply internal-lab, external-development, or
external-production target class.

The independent Slice 1 review also made unknown well-formed credentials run
the constant-time digest comparison primitive against a dummy verifier and
enforced registry target/global scope metadata in common authorization. No
migration was added. Custom-preset creation remains deferred and is still a
Phase 11 completion requirement; the built-in Production Remote Kodi preset
remains unassignable and empty. Phase 11 remains partial.

## Phase 11 Slice 3 read-only target observation

The production registry now contains exactly lexical `addon_list`,
`screenshot_capture`, `target_list`, and `target_status`. Both new tools pass
through the same durable bearer, enabled-client, preset, exact grant, target
policy, static registry, target-class, capability and reconciliation-state
intersection. Discovery remains target-independent: `target.inspect` reveals
the inventory and target tools, while `target.visual` additionally reveals
screenshots. Grants, target policy, exact runtime capability and state remain
invocation-time authority.

`addon_list` calls `ManagedInstanceAddonInventoryService`; the MCP adapter does
not construct Kodi JSON-RPC. It exposes only bounded add-on ID, name, version,
Kodi type, enabled state and trustworthy service-derived provenance class/status.
It defaults to 50 items, caps at 100, preserves deterministic service ordering,
uses the public semantic add-on ID as its cursor, and rejects a structured page
over 64 KiB with `response_too_large`. Paths, install directories, repository
URLs, package caches, raw Kodi documents, internal IDs and provenance inference
from paths remain absent.

`screenshot_capture` calls `ManagedScreenshotService.capture_image`, which is
the existing durable Phase 10 capture path: one capture attempt, the same exact
curated four-runtime capability check, per-instance exclusion, four-capture
global bound, private raw staging, strict PNG validation, atomic latest-cache
publication and old-cache removal. The added result form retains an in-memory
reference to the exact validated PNG published by that successful capture; it
does not reread a racy latest pointer or make an MCP-specific copy. Failure
retains the prior latest screenshot. MCP returns official SDK `ImageContent`
with exact `image/png` plus target/capture UUID, capture time, dimensions, MIME
type and byte size. No image, base64, hash, URL, path or broker identity enters
MCP audit output.

The 64 KiB bound remains an inbound request and ordinary structured-result
bound, not an image-response bound. Validated PNG bytes remain capped at 8 MiB;
the stdio proxy admits only the corresponding base64 maximum plus a fixed 64 KiB
JSON/metadata envelope (11,250,348 bytes). It incrementally reads the HTTP
response to that cap, keeps four active workers and an eight-request
backpressured input queue, serializes stdout writes behind a lock, and waits for
pipe backpressure. There is no unbounded response queue, temp image file, token
exposure or stdio-side screenshot authority.

Migration `0037_mcp_observation_permissions` adds `target.visual` to Add-on
Development, Read-only Inspection and Full Lab Control. All three retain
`target.inspect`; `target.control` is not yet recognized. Production Remote Kodi
remains empty and unassignable. Downgrade removes only the three deterministic
rows and refuses if an assigned client or target policy would lose visual
authority. Phase 11 remains partial: semantic remote control and durable action
receipts, custom presets, the compatibility decision, final client/conformance
matrix and formal closeout remain.

## Phase 11 Slice 4 replay-safe semantic remote control

The production registry now contains exactly lexical `addon_list`,
`remote_action`, `screenshot_capture`, `target_list`, and `target_status`.
`remote_action` is target-scoped, mutating, internal-lab-only, not
production-eligible, and requires `target.control`, exact grant and target
policy, the exact curated semantic-remote capability, running/ready state, and
no reconciliation blocker. Discovery remains target-independent: only clients
whose current preset contains `target.control` see the tool.

The strict input is exactly client-generated UUID `request_id`, explicit
`target_id`, and the existing `BrowserRemoteAction` enum. The MCP layer does not
define a second action vocabulary or accept Kodi methods, parameters, players,
endpoints, text, keys, paths, or timeouts. `BrowserRemoteService` now accepts a
server-created `RemoteActorContext` for Web local administrator or durable MCP
client UUID. Request JSON cannot provide actor identity. The service still owns
runtime proof, mutation coordination, fixed mappings, server-derived active
player selection, one-dispatch/no-retry transport behavior, and exactly one
durable screenshot refresh after a successful navigation action.

Migration `0038_mcp_remote_control` adds `target.control` only to Add-on
Development and Full Lab Control. Read-only Inspection retains inspection and
visual authority only. Production Remote Kodi remains empty and unassignable.
The migration also creates `mcp_remote_action_receipts`, whose UUID primary key
is the invocation identity and whose durable uniqueness is `(client_id,
request_id)`. It stores client, credential-at-first-attempt, target, request,
closed action, timestamps, `pending|completed|ambiguous`, a bounded safe result,
and bounded screenshot-refresh UUID/time/classification. It has deliberately no
client, credential, or target foreign key: deletion preserves replay history,
and a new target UUID cannot inherit old authority. It stores no token, prompt,
arbitrary text, raw Kodi document, pixel/path, endpoint, container, network, or
transport state.

Every call first re-evaluates current authentication and the complete target
authority intersection. A new key is committed `pending` in a short SQLite
transaction before rate admission and before Kodi work. The transaction ends;
the shared 10 actions/second, burst-20 limiter then uses server-derived MCP
client plus target. A second short transaction records the terminal semantic
result. No SQLite transaction spans broker, Kodi, network, coordination, or
screenshot work. A completed exact duplicate returns its stored result without
rate consumption, screenshot refresh, or Kodi dispatch. A pending or ambiguous
duplicate returns `remote_action_ambiguous`; parameter mismatch returns
`request_id_conflict`. Credential rotation does not open a new namespace because
the durable client UUID is primary replay identity.

The crash window is intentionally fail-closed. If the process stops after the
receipt commit and before terminal publication, restart observes `pending` and
never dispatches again. Stable pre-dispatch and acknowledged remote errors are
stored as completed classifications and replayed; uncertain dispatch is stored
ambiguous. An unexpected result-publication failure leaves `pending`. Concurrent
claimants are serialized by the database uniqueness constraint: exactly one may
enter rate admission and the remote service.

Receipts are retained for 30 days. Opportunistic claim-time cleanup removes rows
older than that fixed window; request IDs may be reused only after expiry. A
hard ceiling of 10,000 rows per client and 100,000 globally rejects new work as
`remote_action_receipt_capacity` rather than dispatching without protection.
There is no cleanup daemon and neither MCP nor Web exposes receipt deletion or a
manual replay button.

HTTP and stdio share the same `/mcp` authority, database, client namespace, and
receipt service. Direct deterministic proof covers response-loss replay,
pending/ambiguous restart, concurrent identical and conflicting requests, and
both HTTP-first/stdio-duplicate and stdio-first/HTTP-duplicate directions with
explicit adapter dispatch counts. Phase 11 remains **PARTIAL**: custom presets,
the `2025-11-25` compatibility decision/implementation, final client and
conformance matrix, and formal closeout remain.

## Phase 11 Slice 5 presets and bounded compatibility

Custom presets are clone-only immutable permission snapshots. Web
administration supplies a trimmed 1–120 character unique display name and one
active immutable built-in UUID; the application creates a new random preset
UUID and copies only the built-in's server-known permission rows in one
transaction. The inactive Production Remote Kodi preset cannot be used as a
template. No endpoint can rename, delete, convert, or edit permission rows.
Later built-in permissions therefore do not silently broaden an older clone.
The existing `builtin`, `assignable`, permission, assignment, and content-free
event schema is sufficient, so Alembic remains `0038_mcp_remote_control`.

Custom presets use the existing client and internal-target assignment paths.
Authentication reloads the client preset and its permissions on every request;
target policy, exact grant, class, capability, state, and reconciliation proof
remain invocation-time checks. Reassignment records bounded unassignment and
assignment events containing UUIDs only. Creation records `preset_created` and
never stores form payloads. Custom rename, permission editing, and deletion are
deferred; there is no fallback preset.

Exact legacy `2025-11-25` is enabled only on authenticated stateless HTTP. The
same official SDK ASGI application runs both eras with `stateless_http=True`.
An initial headerless request selects legacy only when its valid JSON-RPC
initialize envelope explicitly offers `2025-11-25`; every later legacy request
carries that exact header. Explicit modern, explicit legacy, unsupported, and
malformed requests have disjoint deterministic handling. Modern malformed
traffic is never retried as legacy. `Mcp-Session-Id` is rejected and stripped,
and no negotiated target, identity, authority, or protocol session is stored.

Both eras enter the same bearer verifier, peer-keyed authentication-failure
limiter, client-keyed request limiter, client/global concurrency limiter,
principal context, registry, authorization service, tool schemas, adapters,
replay receipt namespace, and privacy mapping. Protocol revision is not an
authorization input. Canonical stdio stays exact modern `2026-07-28`; supporting
a handshake-era stdio client would require negotiation state in the proxy and
is not a V1 requirement. There is no second legacy stdio server.

The final matrix uses official Python SDK `mcp==2.1.1` for pinned modern,
modern auto-discovery, canonical stdio, and pinned legacy HTTP. OpenCode
`1.14.39` connects over legacy HTTP with no session identifier and receives its
currently authorized catalog; its legacy stdio handshake is intentionally
unsupported. Codex CLI `0.154.0` is tested but incompatible because it offers
unapproved `2025-06-18` over HTTP and performs a handshake over the modern-only
stdio proxy. No compatibility is claimed for untested Claude, Cline, LM Studio,
Open WebUI, or similar clients.

Official conformance runner `@modelcontextprotocol/conformance@0.2.0-alpha.11`
passes modern tools-list, DNS protection, and all header-validation checks, plus
the applicable tool-cache checks. Modern stateless passes 23/25; the two
remaining checks require the runner-only `test_missing_capability` diagnostic
tool. Cache failures are only unimplemented prompt/resource surfaces. Exact
legacy initialize, ping, tools-list, and DNS scenarios all pass. Fixture-only
tool-call names, prompts, resources, subscriptions, sampling, elicitation,
audio/embedded content, OAuth, tasks, and MRTR remain explicit exclusions, not
production defects or reasons to add tools.

The production registry remains exactly lexical `addon_list`, `remote_action`,
`screenshot_capture`, `target_list`, and `target_status`. Slice 5 adds no Kodi,
broker, Docker, lifecycle, filesystem, raw JSON-RPC, external-target, or Phase
12 authority. Phase 11 is **implementation-complete pending formal review**;
formal completion remains a separate independent task.

## Phase 11 formal completion

**Phase 11 — MCP core: COMPLETE.** Independent review closed the phase against
the canonical V1 boundary at implementation checkpoint
`3faeeb808ca1a86ab5b1a8a0038827fe9b76b57a`, Alembic head
`0038_mcp_remote_control`, and 1,928 collected tests. The prior Slice 5 status
above remains a historical checkpoint rather than the current project state.

The accepted protocol boundary is primary stateless `2026-07-28` over the
existing loopback HTTP listener and canonical modern-only stdio, plus exact
bounded stateless `2025-11-25` HTTP compatibility. Both revisions and both
transports share durable bearer identity, verifier-only credentials, preset and
grant policy, registry authorization, limits, privacy mapping and replay
receipts. Production discovery remains the five-tool catalog, including native
bounded screenshots and replay-safe semantic remote control.

Codex CLI `0.154.0` remains outside the supported two-revision boundary;
OpenCode `1.14.39` has only retained legacy-HTTP connection and authorized
discovery evidence. Phase 12 workflows, external and production target support,
broad diagnostics, and later Kodi tools remain later-phase work.

## Phase 12 Slice 1 MCP development inspection foundation

Phase 12 begins additively without changing the completed Phase 11 protocol,
transport, bearer, preset, admission, registry, replay, or five-tool behavior.
Migration `0039_mcp_development_inspection` adds `workspace.inspect` only to
Add-on Development and Full Lab Control, exact client-to-DevelopmentSource
grants, and single-owner client-to-AgentWorkspace bindings. Existing custom
presets remain snapshots. Read-only Inspection and Production Remote Kodi do not
gain source-code access.

The common authorization service resolves current client permission, exact
source grant, exact owner binding, and binding/workspace source consistency
before a development adapter runs. Revocation therefore denies an otherwise
durable binding on the next request; restoring the grant restores access when
the other conditions remain valid. Credential rotation preserves client
ownership and client disable denies access without deleting binding or bytes.

Four service-backed read-only tools extend the static registry:
`development_source_list`, `workspace_status`, `workspace_tree`, and
`workspace_read`. Source discovery projects persisted path-free registration
metadata without traversal. Status is durable-state-only. Tree/read call the
authoritative Phase 9 service and inherit exact path, descriptor-relative
no-follow, single-link regular-file, UTF-8, 2 MiB file, and integrity-state
rules. Development results are below 48 KiB; tree is non-recursive and at most
200 entries.

The tools share a bounded 10 request/second, burst-20 client-keyed rate class
beneath existing MCP request/concurrency admission and work identically over
modern HTTP, modern stdio, and legacy HTTP. Audit is content-free. The catalog
is now exactly nine lexical tools. Phase 12 remains **PARTIAL**: creation,
editing/CAS, validation, quota/retention, deployment, execution, and promotion
request remain later slices.

## Phase 12 Slice 2 MCP workspace creation and editing

Migration `0040_mcp_workspace_mutation` adds `workspace.edit` only to Add-on
Development and Full Lab Control and adds one bounded
`mcp_development_requests` replay table. Existing custom presets remain
creation-time snapshots. Read-only Inspection and Production Remote Kodi remain
unchanged. Two registry additions produce the exact eleven-tool catalog:
`workspace_create` and `workspace_edit`.

Creation accepts only client-generated request UUID and Web-registered source
UUID. Current enabled client, permission, exact source grant, supported
`local_directory` type, and quota are checked without target authority. The
Phase 9 allocation is extended by a persistence-only transaction hook: one
SQLite immediate transaction creates the workspace, durable creation operation,
global-heavy reservation, `mcp_created` binding, and client/request/operation
mapping. Executor submission occurs only after commit. The workspace is never
visible unowned, and restart between commit and submission leaves exactly one
queued operation for normal startup recovery.

The mapping is unique by `(client_uuid, request_uuid, operation_kind)`, contains
no arbitrary JSON, path, locator, or body, and binds the source through a
normalized SHA-256 fingerprint. Exact duplicate calls return the same workspace
and operation with current state; parameter conflict fails. Thirty-day
opportunistic request-key expiry plus 10,000-per-client and 100,000-global caps
bound the replay index. These bounds do not delete workspace bytes.

Quota counts every bound workspace whose byte state remains present, regardless
of whether the binding was Web-created or MCP-created and regardless of
creating, ready, promoted, abandoned, or reconciliation-required lifecycle.
Limits are four/client, 32/global, and one creating/client. Count and insert run
under the same immediate database write transaction; there is no eviction or
process-local-only reservation.

`workspace_edit` is a flat strict operation discriminator. Create/replace carry
one content field; exact patch carries exact expected and replacement text.
Aggregate text is at most 48 KiB and encoded arguments are conservatively capped
below the MCP 64 KiB body limit. Authorization requires current client,
`workspace.edit`, exact binding, current source grant, source agreement, present
ready state, and exact generation. No target participates.

The adapter invokes only Phase 9 create/replace/exact-patch methods. Existing
path parsing, descriptor-relative no-follow opens, regular-file/UTF-8 checks,
existing-parent requirement, VCS rejection, complete pre/post recapture,
same-directory atomic publication, exact rollback, reconciliation fallback, and
database generation CAS remain unchanged. One process-active edit per workspace
and the Phase 9 cross-process file lock ensure only one same-generation edit can
publish. The loser receives `workspace_generation_conflict`.

Mutation audit remains content-free and path-free. Edits use a client-keyed
two/second burst-four bounded rate class; creation is bounded by request mapping,
quota and the existing global-heavy coordinator. Modern HTTP, modern stdio and
legacy HTTP use the same schemas/services and client namespace. Slice 2 adds no
explicit validation, workspace cleanup/retention automation, deployment,
execution, promotion request, Kodi, broker, Docker, shell, Git, raw filesystem,
external target or production target authority. Phase 12 remains **PARTIAL**.

## Phase 12 Slice 3 structural validation and workspace retention

At the Slice 3 checkpoint, migration
`0041_mcp_workspace_validation_retention` added only durable
`mcp_clients.disabled_at` timing and content-free system retention audit events.
Validation evidence was synchronous and deliberately not persisted in that
slice; Slice 4 later adds the minimal latest successful eligibility row.
`workspace_validate` requires `workspace.inspect`, exact ownership, the current
source grant, byte-present ready state, and exact `expected_generation`; no
target grant participates. Its schema has only workspace UUID and generation.

The unprivileged Phase 9 workspace service remains the filesystem authority. It
recaptures baseline/current trees under shared workspace coordination, checks
durable identities, strict UTF-8 changes, authoritative `addon.xml` parsing,
baseline/current ID, version and extension types, and derives deterministic
added/modified relative paths. Deletion, binary change, type/mode transition,
unsafe objects, malformed or missing `addon.xml`, and unexplained digest drift
fail closed. Results contain no content, source locator, absolute path or raw
error, expose at most 64 relative changed paths, and remain below 48 KiB.
Validation is observational MCP authority, with one active call/client, one per
workspace, and two globally; it adds no edit, test, command or execution power.

Retention is system/Web-admin policy, never MCP permission. MCP-owned abandoned
or safely terminal promoted workspaces become eligible for the existing Phase 9
cleanup service after seven days. A disabled client's ready workspaces remain
quarantined for 30 days, then use the existing safe abandonment transition and
retain the ordinary seven-day byte grace. Re-enable clears `disabled_at` and
immediately restores active ready-workspace retention. Enabled-client ready,
creating, reconciliation-required, ambiguous promotion/verification and failed
cleanup states are retained for administrator attention.

An opportunistic application-activity sweep inspects at most 32 candidates,
performs at most two lifecycle transitions and queues at most two cleanups. It
uses deterministic UUID cursor ordering, rechecks classifications immediately,
shares Phase 9 workspace coordination with edit/validation, and submits only to
the existing cleanup executor. Cleaned bytes stop consuming present-byte quota;
bindings, history and 30-day request-ID retention remain. The exact twelve-tool
lexical catalog is shared by modern HTTP, modern stdio and legacy HTTP. Phase 12
remains **PARTIAL**: deployment, fixed execution and promotion request/human
approval integration are not implemented.

## Phase 12 Slice 4 exact-generation target development loop

Migration `0042_mcp_workspace_deployment_execution` extends the ordinary MCP
authority database with two permissions, latest successful workspace validation
evidence, semantic deployment/execution operations and two operation kinds in
the existing client request map. The data model deliberately contains no host
or container path, raw response, log, directory entry, file content or
container identifier.

The application service, not the transport, is the deployment authority. It
intersects current workspace/source identity with an exact internal disposable
target and its matching DevelopmentSource attachment. Eligibility requires
same-generation structural validation and either a verified canonical source
copy for the exact runtime/container or a prior successful workspace copy that
is still anchored to that canonical container evidence. Exact add-on ID is
stable; generation never floats.

Deployment reserves managed-target, workspace and global-heavy coordination,
then calls only two narrow broker semantics: copy the exact persisted workspace
generation to the server-derived attached-source destination, and verify that
completed workspace deployment's destination provenance. The existing Phase 8
copy engine retains stopped-only, no-follow, ownership, manifest and
overwrite/add behavior; no deletion of destination-only members occurs. MCP
cannot select either broker operation or any argument beneath the four semantic
tool inputs.

The fixed choreography is claim, recheck, mutation marker, stop, sync marker,
copy/readback, start, readiness, enabled inventory ID/version check and terminal
evidence. Unknown state after mutation is durable reconciliation-required and
best-effort stopped; it is not automatically replayed. Sync uncertainty marks
canonical source-copy confidence unknown, while a verified workspace overlay
makes that source confidence stale.

Execution composes only from successful deployment evidence. Fresh ready
runtime, enabled exact ID/version inventory and exact destination provenance are
checked before the existing copied Python video-plugin smoke adapter performs
one fixed directory invocation. A dispatch marker supplies conservative
at-most-once restart behavior. Results expose classification and at most one
item count, not media metadata or raw JSON.

Modern HTTP, modern stdio and bounded legacy HTTP enter one authorization,
service and replay namespace. Current deployment or unresolved operations block
automatic workspace cleanup. Existing workspace status and Web client binding
views show bounded latest validation/deployment/execution state, while Web
retains recovery authority. The exact fourteen-tool catalog still has no
promotion request, general test runner, repository publication, production or
external development target, generic add-on execution, raw JSON-RPC, shell,
Git or Docker surface.

## Phase 12 Slice 5 replay-safe promotion request and human review

Migration `0043_mcp_promotion_request` adds only
`development.request_promotion`, the `promotion_request` operation kind, and a
bounded optional MCP-origin link on the existing Phase 9 promotion plan. It
creates no second plan, apply, replay or verification table. Add-on Development
and Full Lab Control gain the permission; existing custom snapshots and the
read-only/production presets do not.

The tool accepts exactly request, workspace, expected-generation and target
UUIDs. Current authorization intersects enabled client, permission, exact
workspace owner, current source and target grants, internal-disposable target
policy and ready/reconciled target state. Preparation then requires present
ready bytes, exact successful structural validation, exact successful workspace
deployment and exact successful fixed execution for that generation/target.
Source, add-on identity/version and the complete changed-file summary are
server-derived.

The adapter calls the Phase 9 `AgentWorkspacePromotionService.prepare`
boundary. That service remains authoritative for complete change derivation,
conflict detection, source capture, expiry and active-plan uniqueness. The
request map and preparing plan commit together. Exact cross-transport replay
returns the same plan/current state; changed parameters conflict. A durable
mapping whose plan remains in the post-claim preparing state is ambiguous and
never causes another preparation. Active-plan request mappings survive the
ordinary 30-day pruning horizon.

One shared bounded, DB-only pruner applies to all four development mutation
kinds. It deletes expired mappings only when the exact creation, deployment,
execution, or complete review/apply/verification workflow is provably terminal
and replay-safe. Unknown, marker-bearing, ambiguous, active,
reconciliation-required, missing, or inconsistent state is retained and
continues counting toward request capacity.

The proof is deliberately field-complete: contradictory deployment lifecycle,
sync, inventory or digest markers; contradictory execution dispatch,
classification, result or count markers; and inconsistent creation or
promotion/apply/handoff/verification relationships all retain replay identity.
Promotion journal rows are authoritative source-mutation evidence: only an exact
all-`not_attempted` journal can prove pre-application failure, and successful
application requires an exact all-`applied` journal. Missing, extra, mismatched,
rolled-back or uncertain evidence retains identity.
Dependent promotion references are answered by a scalar SQL existence predicate
per candidate rather than loading historical plans. Thus the 256-candidate
outer bound also has finite query and application-object bounds under arbitrary
historical promotion fanout. The same consistency and bounded-reference rules
govern unreferenced terminal deployment/execution history cleanup.

MCP receives only semantic UUIDs, generation, counts, candidate version,
review/expiry and readiness/conflict classifications. Existing Web review
renders the complete exact diff and now identifies the originating client plus
validation/deployment/execution evidence. Approval and confirmed rejection are
session-authenticated, same-origin, CSRF-protected Web administrator actions;
MCP bearer authentication is not accepted there and the MCP registry has no
approval tool.

Client disable or source/target grant revocation immediately removes subsequent
MCP authority and status visibility without erasing the plan. It does not make
the human decision. Web may still inspect/reject and may approve only after the
ordinary exact Phase 9 source/workspace/target/evidence and source-conflict
recheck. The existing backup, atomic apply, rollback, target synchronization,
durable handoff and mandatory verification child remain unchanged. Source apply
success and later verification failure/incompleteness remain distinct durable
facts.

`workspace_status` exposes only bounded latest plan/review, promotion operation,
verification child and final semantic classification. Pending plans and
promotion/verification work retain workspace bytes. New preparation admission
is two per client per ten minutes with bounded keys and one active plan per
workspace. Modern HTTP, modern stdio and bounded legacy HTTP share the exact
fifteen-tool lexical catalog and replay state. Phase 12 is
**IMPLEMENTATION-COMPLETE PENDING FORMAL REVIEW**; this checkpoint does not
declare formal Phase 12 completion and adds no Git, repository, shell, generic
execution, external-target or production-target authority.

## Phase 12 formal completion

**Phase 12 — MCP development workflows: COMPLETE.** Independent review closed
the canonical V1 phase at implementation checkpoint
`dc6f7857b222aff31467222e8d9eff8b96df6328`, Alembic head
`0043_mcp_promotion_request`, and 2,020 collected tests. The Slice 1–5 status
statements above remain historical checkpoints.

The accepted design combines exact Web-managed source grants, one durable MCP
client owner per workspace, Phase 9 no-follow path authority, atomic replay-safe
creation, generation-CAS editing, closed validation, conservative bounded
retention, exact internal-disposable deployment, fixed copied video-plugin
execution, and MCP-only preparation of Web-reviewed Phase 9 promotion. Web human
approval, source-conflict recheck, source apply, handoff and mandatory
verification remain the sole canonical completion path.

D-0277 through D-0279 close replay-retention risk with one bounded central
pruner, coherent complete terminal evidence, bounded scalar historical-plan
references, and exact promotion-journal predicates. Applied, rolled-back,
uncertain, missing, extra, mismatched or otherwise contradictory source-mutation
evidence retains identity; only coherent all-`not_attempted` pre-application
failure or coherent all-`applied` terminal success may expire. Protected rows
remain inside the request caps and are never deleted to recover capacity.

The production registry is the exact fifteen-tool catalog documented in the
Phase 12 record and preserves the completed Phase 11 identity, transport,
permission, grant, registry and replay core. There is still no shell, Git,
repository/ZIP publication, arbitrary filesystem/test/command, generic
JSON-RPC/add-on execution, Docker/raw-broker, external-development target or
production-target authority. Phase 13 begins with external-development-target
architecture review, not implementation or production testing.

## Phase 13 Slice 1 external-development boundary

External Kodi registrations reuse `targets.id` and have exactly one
`external_kodi_targets` subtype row. The target type constraint admits only
`managed` and `external_development`; production is structurally absent. The
subtype separates disabled/enabled/deactivated administration,
unverified/verified/re-verification/identity-mismatch trust, and current
reachability/authentication/capability observation. Append-only bounded probe
observations and semantic events remain distinct from current projection.

Only authenticated Web administrators cross this boundary. Registration,
endpoint and credential changes, fixed read-only probe, exact observation
confirmation, enable/disable and durable deactivation are same-origin and
CSRF-protected. The MCP registry remains the exact fifteen tools and all
existing target adapters and grant/policy administration remain managed-only.

Slice 1 accepts canonical IP literals, not URLs or hostnames. Private RFC1918
IPv4 is allowed except the managed-lab network; IPv6 is allowed only inside
operator-configured ULA prefixes. Server-configured ports default to `8080`.
Loopback is unavailable unless an explicit test setting is enabled. The
verifier constructs the scheme and `/jsonrpc`, connects directly with no
ambient proxy, follows no redirects, and makes only bounded fixed read-only
calls. HTTPS uses normal CA and IP-name validation; there is no `verify=false`
or self-signed pin path in this slice.

Username/password credentials are AES-256-GCM files below the protected data
root, keyed by explicit deployment configuration. Database state holds only a
random opaque UUID reference and key-version-bound ciphertext metadata remains
outside SQLite. No-auth LAN mode is explicit. Password authentication over HTTP
is displayed with a warning because it is transport-visible on that LAN.

## Phase 13 Slice 2 external semantic target adapters

MCP target authorization now resolves one semantic `KodiTargetAdapterService`
after current client permission, exact common target grant and target preset
policy checks. Managed UUIDs select the existing runtime-status, inventory and
browser-remote services. External-development UUIDs select the fixed native
adapter only when the subtype is enabled, verified, not mismatched or awaiting
re-verification, the current structured endpoint still matches confirmed
address evidence, and the required capability is present in both configured
and verified sets.

The registry widens only `target_list`, `target_status`, `addon_list` and
`remote_action` to external development. Discovery uses confirmed stored state
and therefore does not hide a granted target solely because it is temporarily
unreachable. Status performs an on-demand Ping/application identity check;
inventory obtains current application identity and fixed paged
`Addons.GetAddons`; remote performs a fresh identity check immediately before
the existing closed semantic action mapping. There is no background polling.
All external requests construct `/jsonrpc`, ignore environment proxies, reject
redirects, use bounded body/JSON/time limits and send credentials only to the
validated IP-literal endpoint.

The verification probe establishes `addon_inventory` with one bounded
read-only inventory page. It establishes `semantic_remote` only when fixed
read-only `JSONRPC.Introspect` calls prove every method needed by the closed
action mapping. Missing methods leave the capability unverified; Kodi version
is not capability evidence. MCP never triggers capability establishment.

The Phase 11 receipt remains the remote replay authority. Its durable pending
claim precedes the fresh identity check. Current client permission, exact grant,
target policy, state and capability are re-authorized after that check and
immediately before mutation. Response loss after possible send becomes durable
ambiguity and duplicates never resend. Proven connect failure is a bounded
pre-send failure. Native work is limited to eight concurrent operations
globally and one operation per exact external target, with at most 1,024
retained target gates.

Screenshot, workspace deployment, workspace execution and promotion retain
internal managed target-class eligibility. No bridge, pairing, endpoint input,
raw JSON-RPC, lifecycle, filesystem, production or registration authority is
added to MCP. Modern HTTP, modern stdio and bounded legacy HTTP all enter the
same adapter, authorization and replay services.

## Phase 13 Slice 3 authenticated external screenshot bridge

Migration `0045_external_bridge_pairing` keeps optional bridge trust separate
from native target trust. It stores bridge lifecycle/reachability, observed and
pinned identity, opaque secret references, endpoint revision and verified
`screenshot` capability. Bounded request receipts and content-free semantic
events are separate tables. Pairing, native verification and MCP grants are
independent authorities; none implies another.

The new `service.kodi_lab_bridge` is a narrow ordinary Kodi Python add-on. On
first initialization it creates one persistent random instance UUID. Protocol
1 owns one fixed POST endpoint and only identity, capability and bounded GUI
screenshot operations plus the possession-proof pairing exchange. Its
identity includes bridge UUID, protocol, add-on ID/version, Kodi version,
closed capability and canonical build fingerprint. It exposes no generic
JSON-RPC, caller path, filesystem, log, add-on administration, deployment,
execution, restart, profile or lifecycle operation.

Web creates a random 256-bit, target/purpose-bound token with a ten-minute TTL
and returns it once. The operator enters it locally in the add-on. The bridge
first requires the Lab request to prove possession, then proves its own
possession; only then do both peers derive a long-lived secret from the token,
target, bridge and fresh nonces; the visible token is then cleared. Lab stores
only AES-GCM secret references and requires explicit administrator confirmation
of observed identity before activation. Re-pairing rotates authority;
revocation and target deactivation retire it. MCP has no bridge administration.

Separate version-labelled HMAC-SHA-256 request and response subkeys cover
canonical target/bridge identity, protocol, operation, request UUID, timestamp,
nonce, status where applicable and payload digest. Sixty-second time freshness
is combined with request/nonce uniqueness. The add-on durably caps replay
identity at 256 for 24 hours and caches four exact responses for 120 seconds.
Within that ledger, a live screenshot identity is non-evictable; protected
capacity exhaustion rejects new work. Read-only and content-addressed
deployment entries may be displaced only because their handlers independently
make exact replay idempotent and conflicting reuse fail closed. Legacy entries
without an operation classification are protected until expiry.
Lab caps screenshot receipts at 256 per target and 4,096 globally for 24 hours,
prunes only expired terminal rows and preserves pending/ambiguous identity.
Pairing is serialized per target with two global slots; screenshot capture is
serialized per target with four global slots.

`screenshot_capture` keeps its target-only schema and now permits an external-
development class. The adapter advertises `screenshot` only for an enabled,
native-verified target whose configured maximum includes it and whose bridge is
paired, reachable, identity-pinned, endpoint-current and capability-verified.
Exact MCP permission, target grant and target preset remain mandatory.
Unauthorized clients cannot distinguish pairing state. Native Slice 2 tools do
not require a bridge. Deployment, execution and promotion stay internal-only.

## Phase 13 Slice 4 bridge-backed external development

Migration `0046_external_bridge_development` keeps Phase 12 as the sole
workspace-development state machine. Its deployment evidence now has a closed
target-class union: managed operations bind exact runtime revision, while
external operations bind exact bridge, bridge request and terminal receipt.
The same workspace generation, structural validation, request map, global
heavy coordination, execution evidence and promotion request consume either
shape. No new MCP tool or input field exists.

External mutation begins with a Web-established association between one
approved DevelopmentSource and one paired external target. The association is
an exact add-on ID/version plus canonical tree digest/counts, not a version
claim. Deployment checks the baseline or last successful external deployment;
unexplained tree change marks it stale. Baseline establishment and ambiguity
reconciliation are authenticated Web operations only.
If the operator restores the target to the exact registered source, a newer
successful Web baseline supersedes older successful deployment lineage. This
timestamp ordering lets the existing Web trust ceremony recover from drift
without MCP adoption or direct state edits; only a later successful deployment
becomes the next expected tree.

The additive protocol-1 `workspace_deployment` capability is a content-addressed
transaction, not a file API. Lab sends a canonical manifest and referenced
256-KiB chunks under one authorized deployment UUID. The bridge locally derives
the destination, stages regular files, verifies hashes/descriptor, preserves
enablement, keeps one rollback tree, performs best-available directory rename,
and verifies tree plus Kodi inventory. Lab caps transfer bandwidth at 16 MiB/s
and each invocation at two hours in addition to the fixed body and concurrency
bounds. Commit-response loss is resolved only by
the durable receipt. Begin/chunk resume is idempotent and cannot activate a
partial tree; commit is not automatically repeated.

Fixed execution remains native. Native verification proves the exact
`Files.GetDirectory` method and the bridge proves current deployment bytes.
The existing Phase 12 copied-video-plugin invocation then runs behind its
pre-dispatch marker. Pre/post tree and inventory observations bind execution to
the deployment. Consequently the bridge gains no JSON-RPC or execution
mutation authority.

External Kodi may emit interpreter bytecode during that invocation even though
the exact deployed manifest contains none. Bridge readback applies the
canonical bytecode-cache exclusion only to a real, non-recursive
`__pycache__` directory whose direct children are bounded single-link regular
`.pyc` files. Links, subdirectories, other names, direct `.pyc` files and all
source-tree additions remain errors. The bridge neither reads those derived
bytes nor deletes them, and the request still supplies no path or filesystem
operation.

The authority intersection includes client/tool, workspace ownership, source
grant, target grant/policy, external-development class, enabled native identity,
paired bridge identity, configured and observed capabilities, exact generation,
successful validation and current source provenance. Target disablement,
deactivation or bridge revocation blocks new work without deleting history.
**Phase 13 — Remote targets: COMPLETE.** Independent formal review reproduced
and remediated one bridge replay-capacity defect, enforced the documented
native total deadline, and tightened screenshot hardlink rejection. Authorized
Kodi 20.5 LAN proof remains the sole live compatibility claim; deterministic
coverage supports 19.5, 20.5, 21.3 and the Kodi 22 line. Production external
targets and every external lifecycle authority remain deferred to Phase 14 or
later.

The add-on captures the full Kodi GUI with the cross-version
`TakeScreenshot(<path>,sync)` built-in. It generates the complete filename
under its fixed `special://temp/kodi-lab-bridge` namespace, performs bounded
readback and PNG normalization, and deletes the exact transient file in
`finally` before constructing the authenticated response. MCP and Lab provide
no path, filename, directory or URI. A bounded short-TTL scan touches only
strict bridge screenshot names after crash residue; it never recurses or
removes unknown files. The application authenticates response
identity/status/digest before applying the strict normalized PNG
signature/chunk/checksum/encoding/geometry/decoded-size validator and 8 MiB
maximum. External PNG bytes and native paths are not persisted. Audit holds
only UUIDs, outcome, dimensions, size and digest. An exact request UUID/nonce
retry returns the cached result or bounded replay/ambiguity and cannot perform
a second capture.

Bridge connection reuses the external IP-literal/address-class policy with a
separate allowed port, fixed path, no redirects and no ambient proxies. HTTPS
keeps normal certificate validation. HTTP HMAC prevents fabricated or modified
semantic results but does not provide confidentiality, so PNG bytes are
cleartext to LAN observers. Modern HTTP, modern stdio and bounded legacy HTTP
continue through the same registry, authorization, adapter and tool result.
