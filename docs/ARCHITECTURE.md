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
