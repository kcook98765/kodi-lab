# Implementation plan

The authoritative phase ordering is in `V1-SPEC.md`, section 47. Work proceeds
in narrow, separately validated steps:

0. Repository foundation
1. Core domain/storage
2. Restricted container broker
3. Single Kodi runtime end-to-end
4. Web UI vertical slice
5. Curated Kodi matrix
6. Fixtures/reset
7. Snapshots
8. Add-on development workflows
9. Agent working copy
10. Browser remote/display
11. MCP core
12. MCP development workflows
13. Remote targets
14. Production policy/approvals
15. Diagnostics/hardening
16. Public release preparation

Each step must remain within its explicit authorization, preserve the legacy
lab until replacement validation and retirement approval, and end at a clean,
tested checkpoint. This initial Phase 0 step establishes documentation,
packaging, typed bootstrap configuration, migration scaffolding, and local
quality checks only.

Phase 1 progress: the first vertical slice now implements persisted Target and
managed-instance identity/state, explicit repositories, SQLite foreign-key
enforcement, and the initial reversible Alembic migration. Runtime control and
the remaining domain/storage areas are not implemented.

The runtime-catalog slice adds logical runtime definitions, finalized immutable
revisions, typed provenance, independent approval/trust lifecycle metadata,
newest-approved selection, and exact managed-instance revision foreign keys.
It performs no source fetching, building, installation, or execution.

The managed-instance application slice composes those repositories behind an
atomic create/reassign service. It owns runtime selection, approval and Trusted
Custom Build policy, Kodi-version compatibility, transaction rollback, and typed
application errors. Outer interfaces and runtime control remain unimplemented.

The runtime-catalog administration slice completed Phase 1 application
orchestration for create-only definition/finalized-revision registration and
explicit approval/trust lifecycle changes. Registration, lifecycle mutation,
managed-instance selection, and error translation share consistent atomic
service boundaries. Phase 1 is complete.

Phase 2 foundation now establishes the restricted local broker boundary:
versioned bounded JSON framing over a protected Unix socket, kernel peer
credentials, exact-UID authorization, a typed client, fail-closed socket-path
handling, safe structured errors/audit fields, and a closed registry. The first
Docker-backed slice adds only empty-parameter `managed_container_inventory`: a
fixed-filter read-only adapter selects exact Kodi Lab ownership labels, validates
canonical instance UUIDs, rejects duplicate identity, and returns at most 128
sorted data-minimized observations. It performs no persistence or Docker/Kodi
lifecycle mutation. The first mutating slice adds only
`stop_managed_container`: exact label-based discovery, immutable container-ID and
runtime-state preconditions, idempotent already-stopped convergence, fixed
10-second Docker stop, and fresh postcondition verification. The next slice adds
explicit `start_managed_container` and consolidates both operations behind one
private two-state reconciliation flow. Desired-state convergence is idempotent;
required mutations demand expected state equal fresh observed state; and both
directions revalidate ownership/identity and postconditions. It adds no create,
restart, remove, kill, exec, readiness polling, or generic mutation. The later
provisioning slice proceeds only after image, network, storage, port, naming, and
rollback contracts were explicitly approved. The development host service-name collision
remains an independent deployment blocker and is not resolved by repository
runtime work.

The managed-container provisioning foundation adds immutable one-to-one runtime
container artifact metadata and migration `0003`, a broker-side read-only trusted
resolver, and fake-only `provision_managed_container`. The operation accepts only
managed-instance UUID and creates or verifies a disposable stopped container with
fixed name/labels/image/network and no ports, mounts, command/environment
overrides, elevated privilege, or guessed resource limits. Full drift checking
and proof-gated rollback prevent adoption or deletion of uncertain/pre-existing
objects. The required managed network is a separately created prerequisite;
persistent storage, real-lab validation, readiness, and deployment remain later
milestones.

The final destructive slice adds only `deprovision_managed_container`: trusted
persisted-revision resolution, label-based exactly-one discovery, immutable
ID/revision and stopped-state preconditions, normal non-volume container removal,
and dual absence postconditions. It removes no database/catalog state, image,
network, volume, or legacy resource, and exposes no generic Docker removal
primitive.

Phase 2 is complete. Fake/security tests cover the protocol, authorization,
socket safety, Docker filtering and normalization, two-state reconciliation,
provisioning drift/rollback, and proof-gated deprovision. Controlled development host
validation additionally exercised peer authorization and the complete real
Docker sequence: provision, stopped inventory, start, running inventory, stop,
stopped inventory, idempotent stop, deprovision, and verified absence. The
unprivileged client could not access the Docker socket, and protected legacy
containers, networks, broker, and socket remained unchanged. The retained
validation state is an unprovisioned disposable ManagedInstance plus the empty
owned `kodi-lab-managed-v1` prerequisite network.

The Phase 2 code closeout does not install production services or assign network
lifecycle ownership. The collision between the intended
`kodi-lab-broker.service` name and the protected legacy service remains a
production-deployment blocker. Persistent-instance storage is also deliberately
deferred; the completed creation contract is disposable-only.

Phase 3 is the canonical **Single Kodi runtime end-to-end** phase. Its first
coherent milestone implements one dedicated Kodi 21.3 Omega stable runtime. The
repository now contains digest-pinned build inputs, a minimal non-root Xvfb
image, immutable artifact and execution-profile registration, process health
normalization, fixed managed-endpoint observation, and an unprivileged bounded
JSON-RPC readiness coordinator. Provisioning remains UUID-only and disposable;
all runtime behavior comes from the image and trusted profile.

The controlled live gate for this milestone is exactly one provision, start,
four-layer readiness observation, and clean broker stop using separate Phase 3
validation state and the existing managed network. The prior nginx fixture is
not Kodi evidence. Production service naming, persistent storage, general
managed-network cleanup, additional Kodi versions, addons, remote display, and
product UI remain later milestones.

Phase 4 begins with one authenticated localhost-only Web vertical slice over the
existing canonical managed instance. It adds the single-admin Argon2id bootstrap,
opaque SQLite-backed sessions, CSRF/origin/Host protection, server-rendered
status UI, a bounded semantic JSON surface, trusted lifecycle orchestration, and
ephemeral per-instance readiness tasks. It exposes only existing
provision/start/stop/deprovision semantics and adds no target/catalog creation,
matrix, generic Docker/JSON-RPC, LAN/Nginx, or production deployment surface.

Phase 5 Step 1 establishes the curated matrix contract without adding another
runtime. Exact Kodi identity now distinguishes stable, alpha, beta, and RC
releases including prerelease ordinals. RuntimeDefinition owns explicit Current,
Legacy, Preview, or Retired support policy. Immutable per-revision validation
evidence under `kodi-runtime-v1` remains separate from approval and build trust.
One shared eligibility policy gates all new pins on exact compatibility, support,
approval/trust, artifact, profile, and current passing validation evidence;
newest-eligible selection may retain an older eligible revision but never changes
release identity. Existing exact pins remain historical authority.

Phase 5 Step 2 implements only Kodi 20.5 stable. Its version-owned Noble image
uses the exact official 20.5 package, minimal non-root headless architecture,
and a Kodi-20-only Roboto compatibility link. A dedicated non-production catalog
registers the Current definition, immutable revision/artifact/profile, and
approval before live work; absence of validation evidence keeps it ineligible.
The implementation checkpoint precedes two independent broker-mediated
start/readiness/clean-stop cycles. Only safe deprovision, verified absence and
legacy isolation permit appending passing `kodi-runtime-v1` evidence and proving
normal Current eligibility.

Phase 5 Step 3 implements only Kodi 19.5 stable as Legacy. Its version-owned
Jammy image uses exact hash-pinned packages from Team Kodi's signed old-stable
Launchpad PPA and preserves the same bounded, non-root headless architecture.
A dedicated non-production catalog registers the Legacy definition and exact
immutable revision/artifact/profile before live work. Two independent readiness
and clean-stop cycles, deprovision, absence, and isolation proof precede passing
`kodi-runtime-v1` evidence. Passing evidence must leave normal selection
ineligible while trusted Include Legacy selects the exact 19.5 revision; 20.5
and 21.3 selection remains exact and unaffected.

Phase 5 Step 4 implements only Kodi 22.0 Beta 2 as Preview. Its version-owned,
multi-stage Ubuntu 26.04 image builds the exact signed Team Kodi release commit
from a digest-verified source archive, records the declared toolchain/options,
and excludes build tools from the final non-root headless runtime. The source
build remains subject to global and per-revision Trusted Custom Build gates.
Two independent readiness and clean-stop cycles must observe exact JSON-RPC
`beta` tagversion `2`, then deprovision, absence, and isolation proof must
precede passing `kodi-runtime-v1` evidence. Passing evidence leaves normal
selection ineligible while trusted Include Pre-release selects only the exact
Beta 2 definition; Beta 1, RC, stable, Current, and Legacy identities retain
their existing exact behavior.

Phase 5 Step 5 makes the proven curated matrix consumable from the authenticated
localhost Web product without adding runtime or catalog mutation. One
application-owned service lists semantic release/tier/validation/availability
DTOs and resolves the newest eligible revision number for a trusted Target's
exact release. Authenticated HTML and JSON accept only request-local Include
Legacy and Include Pre-release preferences; global Trusted Custom Build policy
remains deployment-owned. The browser receives no revision UUID, artifact,
image, evidence path, provenance internals, Docker configuration, approval,
support, validation, or trust mutation authority.

ManagedInstance creation and reassignment remain deferred. Before exposing that
mutation, a later milestone must define Target inventory/creation, ordinary
instance metadata and naming, instance-type choice, and Target-version change
semantics. The completed read-only resolution endpoint proves exact selection
without silently changing Target identity or reinterpreting historical pins.

Phase 6 Step 1 implements the smallest disposable reset and fixture baseline.
One application reset operation accepts only ManagedInstance UUID, reconciles
unprovisioned/stopped/running starting states, recreates from the unchanged exact
RuntimeRevision pin, and always ends provisioned/stopped. Persistent,
transitional, unknown, and ambiguous states fail closed. The process-local keyed
lock prevents duplicate resets within one application process; distributed
coordination and durable reset history remain deferred.

The initial immutable repository-owned `small-mixed-v1` for exact Kodi 21.3
proved archive application and readiness but not a populated library:
`sources.xml` did not establish Kodi's separate video-database content
assignments. Phase 6 Step 1B preserves that fixture and evidence, then adds
`small-mixed-v2`. v2 retains the same synthetic movie and one-episode series and
adds a hash-pinned, Kodi-21.3-generated `MyVideos131.db` containing only two
fixed local-content path rows and zero library or user entities. Kodi's normal
scanner still creates and verifies all movie/show/episode entities from NFO and
media within 60 seconds.

Immutable definition metadata and nullable instance assignment are durable;
payload bytes remain package resources. The existing broker operation applies
only the persisted assignment to one exact stopped owned container using a fixed
validated archive and destination. The database seed adds no broker operation,
SQL API, copy primitive, caller path, or revision change. An unprivileged
fixed-method adapter sequences fixed movie and TV scans and verifies exact
semantic identities and paths before normal stop. This adds no fixture language,
upload, generic copy/exec/JSON-RPC, revision selection, persistent storage, Web
mutation, or production deployment.

Phase 6 Step 2 exposes bounded fixture and reset status through one
application-owned semantic DTO/service. It combines durable exact-pin and
fixture state, fresh broker inventory, and bounded process-local reset activity;
an authenticated dashboard section and GET endpoint consume the same view. The
browser receives no reset, fixture, revision, container, or Docker mutation
authority. Completed reset results remain ephemeral and no migration, broker
operation, durable history, or distributed coordination is introduced.

The next separately authorized Phase 6 milestone should design safe Web reset
mutation as one coherent contract: destructive confirmation and authorization,
operation monitoring, and coordination appropriate for the intended worker
deployment. It must continue accepting only ManagedInstance identity and must
not expose mutation until cross-process behavior is explicitly resolved.

Phase 6 Step 3 implements `ResetOperation`, a SQLite-enforced one-active partial
unique index, digest-bound leases and phase heartbeats, stale-running
reconciliation, and 20 terminal rows of bounded history. Multiple Web processes
may run executors, but only the lease winner calls the exact-pin reset service.
The confirmed localhost POST queues work and returns 202; durable status survives
restart, and active reset blocks Web lifecycle plus trusted fixture/revision
mutation.

The next coherent Phase 6 milestone should validate and materialize a distinct
immutable Small/Mixed fixture version for Kodi 20.5 using its exact generated
video-database schema, without changing v1/v2 or broadening selection authority.

Phase 6 Step 4 adds immutable `small-mixed-v3` for exact Kodi 20.5. Its media,
NFO, artwork, source configuration, scan sequence, and 1/1/1 semantic baseline
are identical to v2, while its exact-image-generated `MyVideos121.db` seed and
purity audit are Nexus-specific. Exact fixture compatibility prevents v2/v3
cross-assignment. The same reset, broker, adapter, durable operation, and Web
request paths are reused without runtime selection or added authority.

The next coherent Phase 6 milestone should test the same fixed Small/Mixed
contract on Kodi 19.5 Legacy with a new exact-schema fixture version. This
extends stable-release portability before spending fixture-version effort on a
moving Preview runtime or broadening the fixture taxonomy.

Phase 6 Step 5 adds immutable `small-mixed-v4` for exact Kodi 19.5. It reuses
the byte-identical synthetic media, NFO, artwork, source configuration, scan
sequence, and semantic 1/1/1 contract while pinning Matrix `MyVideos119.db` and
its schema-aware configuration-only purity audit. v2, v3, and v4 reject every
cross-release assignment. The existing broker operation, reset service,
durable coordinator, and fixed adapter surface are unchanged. Fixture
availability remains independent of Legacy selection: Include Legacy is still
required for normal Kodi 19.5 selection.

The next coherent Phase 6 milestone should expand the fixture taxonomy beyond
Small/Mixed on an already proven stable release. Three stable Kodi generations
now establish the release-specific seed pattern; a new semantic fixture adds
broader product value without binding another immutable fixture version to a
moving Preview schema.

Phase 6 Step 6 adds bounded persisted Mixed/Movies taxonomy through migration
`0009_fixture_content_taxonomy`, with loss-refusing downgrade when Movies data
exists. Immutable `small-movies-v1` defines Small/Movies as three exact
synthetic movies on Kodi 21.3, using one Omega configuration-only Movies seed.
Trusted package semantics drive exact verification and a Movies-only scan plan;
Small/Mixed remains exact 1/1/1 with Movies-then-TV scanning. Reset and broker
authority remain generic and exact-pin preserving.

The next coherent Phase 6 milestone should add Small/TV on Kodi 21.3 to complete
the three Small content categories. That tests zero-movie initialization,
TV-only scan planning, and episode/show relationship verification before
multiplying another taxonomy family across releases.

Phase 6 Step 7 completes the Small content taxonomy. Migration
`0010_fixture_tv_taxonomy` admits exactly TV alongside Mixed and Movies with
loss-refusing downgrade. Immutable `small-tv-v1` defines one show, one season,
and three episodes on exact Kodi 21.3 using a TV-only Omega configuration seed.
Trusted verification proves each show/episode relationship and TV-only scan
planning; a fixed Episode 1 mutation proves reset restores 0/1/2 drift to exact
0/1/3. Reset, broker, durable Web coordination, and exact-pin behavior remain
unchanged.

The next coherent Phase 6 milestone should port the now-proven Small/Movies and
Small/TV semantic families to Kodi 20.5. This tests whether taxonomy-specific
seeds and relationship verification remain portable without the instability of
a Preview runtime or the scale complexity of Medium datasets.

Phase 6 Step 8 completes stable Small-taxonomy portability. Immutable
`small-movies-v2` and `small-tv-v2` use exact Nexus schema 121 configuration
seeds; `small-movies-v3` and `small-tv-v3` use exact Matrix schema 119 seeds.
Movies semantic assets remain exact 3/0/0 and TV assets remain exact 0/1/3
across Kodi 19.5, 20.5, and 21.3. Closed compatibility, family-driven scan
planning, semantic verification, bounded drift operations, broker application,
reset orchestration, durable Web coordination, exact pins, and Legacy policy
remain unchanged.

Dedicated live validation completed all four missing stable cells in order:
Kodi 20.5 Movies, Kodi 20.5 TV, Kodi 19.5 Movies, and Kodi 19.5 TV. Each
fixture passed initial population, bounded drift, restoration, repeat reset,
exact-pin checks, and cleanup without architectural divergence. The complete
19.5/20.5/21.3 by Mixed/Movies/TV Small matrix is therefore proven.

The next coherent Phase 6 milestone should begin Medium taxonomy on Kodi 21.3.
The stable release-specific seed model is now exercised across every Small
content category; dataset scale and deterministic performance are the highest
remaining product risk before spending immutable fixture effort on Preview.

Phase 6 Step 9 introduces the closed Medium size category and immutable
`medium-mixed-v1` for exact Kodi 21.3. Its package declares 25 movies and five
two-season, five-episodes-per-season shows: 25/5/50 and 80 primary entities.
The exact schema-131 configuration seed still contains only the fixed Movies
and TV paths. Verification uses package-owned keyed identity maps, the scan plan
remains Movies then TV, and fixed validation drift removes only Movie 001 plus
Series 01 S01E01. Reset, broker, Web, lease, exact pin, and the 60-second fixture
deadline remain unchanged pending measured live scale validation.

Live scale validation proved initial, restoration, and repeat reset in
16.77/14.89/15.83 seconds with exact 25/5/50 semantics and fixed 24/5/49 drift.
The longest component was normal readiness at roughly 7.35 seconds; each scan
was under 3.8 seconds, archive application under 0.7 seconds, and final stop
under 0.35 seconds. The existing 60-second fixture deadline, ten-minute lease,
and archive bounds therefore remain unchanged.

The next coherent milestone should begin the Phase 7 snapshot vertical slice
with its canonical architecture stop gate. Medium scale is now proven and
Medium/Movies, Medium/TV, stable portability, and Preview portability reuse
already-proven patterns. Snapshots are a first-class v1 feature and introduce
the higher-risk unproven storage, stop/restore, validation, prior-state restore,
and global-heavy-operation coordination contract.

Phase 7 Step 2 implements only disposable snapshot creation. It adds immutable
central root-owned artifacts, deterministic uncompressed tar and canonical
manifest v1, independent application validation including recognized Kodi
video-database integrity checks, durable Snapshot and typed create-operation
records, shared per-instance mutation exclusion, and one global-heavy lease.
Authenticated Web can create, list, and poll safe metadata only. Runtime pins,
fixture assignments, reset baselines, broker narrowness, and protected legacy
resources remain unchanged.

The Phase 7 Step 2 live-validation follow-up classifies exact top-level
`media/` and `system/` as recognized empty-only Kodi runtime scaffolding.
They are accepted and omitted only when empty; any member beneath either fails
closed before publication. This is a third state alongside included and
excluded profile state, not a recursive allowlist or a general exception for
unknown empty directories.

Phase 7 Step 3 implements the disposable snapshot restore vertical slice.
Restore is exact-source, exact-RuntimeRevision, and exact-fixture compatible;
it independently validates before mutation, replaces the old container with a
fresh exact-pin stopped container, uses one fixed broker apply/readback
operation, preserves prior running/stopped state, and never changes fixture
metadata. A restore-specific durable operation uses the existing per-instance
and global-heavy leases, and destructive uncertainty is reconciliation
required rather than automatically retried.

Authenticated Web adds destructive confirmation, a strict confirmation-only
202 POST, and bounded status. No safety snapshot is automatic. Snapshot bytes
remain immutable and unavailable for browser access; persistence,
cross-instance/cross-version behavior, and generic archive authority remain
absent.

Phase 7 Step 4 implements explicit deletion of one user-selected Snapshot.
Queueing atomically marks it deleting and creates a typed durable operation.
The UUID-only broker operation removes the exact descriptor-relative artifact
subtree; application absence proof precedes Snapshot-row removal, while copied
operation identity remains as bounded history. Delete shares the singleton
global-heavy lease with create and restore but takes no instance mutation lease.
Authenticated Web provides permanent-delete confirmation, a strict 202 POST,
and bounded status only. There is no automatic retention, bulk/delete-all,
archive access, instance deletion, or persistent Snapshot behavior.

After live deletion and sibling-artifact isolation are proven, the smallest
next milestone should define instance deletion with explicit user-Snapshot
preservation. That boundary must be resolved before source-bound Snapshot rows
can safely outlive their ManagedInstance; persistent-instance snapshots remain
a larger, separate consistency design.

Phase 7 Step 6 implements disposable ManagedInstance deletion after that
architecture decision. A dedicated durable operation uses the new closed
`INSTANCE_DELETE` mutation kind and container-first ordering. The strict request
requires explicit confirmation plus explicit preserve/destroy policy, and
reloads the complete server-side Snapshot set. Preservation atomically detaches
metadata without touching UUID-derived artifacts; detached Snapshots are
archive-only but remain globally discoverable and individually deletable.
Destruction additionally holds the singleton global-heavy lease and reuses the
proven UUID-only artifact-first deletion core without nested delete operations.

Migration 0015 also bounds ManagedInstance notes and the one-time deletion
history copy to 4,096 Unicode code points, refusing oversized legacy data rather
than truncating it. Typed terminal reset/Snapshot histories retain immutable
instance UUIDs without a live-parent FK. Reconciliation remains read-only and
never automatically resumes stale destructive work. Persistent-instance
deletion/snapshots, clone/import, undelete, archive transfer, cross-instance
restore, and general bulk deletion remain outside Step 6.

Phase 7 Step 8 corrects coordination safety before lifecycle-history work. A
closed cross-kind stale dispatcher now runs at application startup and before
global-heavy or conflicting instance reservations. It validates exact typed
ownership, terminalizes abandoned running reset/create/restore/delete/instance
delete records as reconciliation-required without replacing their last phase,
preserves create quarantine, and releases only proven expired reservations.
Corrupt or orphaned reservations remain and fail closed; no destructive work is
resumed.

A central typed reconciliation gate keeps semantic blocking after lease
release. Reset, restore, and instance-delete uncertainty blocks all instance
mutations; create is source-recovery aware; delete is Snapshot-scoped. Bounded
instance and Snapshot DTOs drive authenticated read-only warnings and status
endpoints with no retry, acknowledgment, finalization, or repair route.
Pruning targets only the newest-20 policy for ordinary succeeded/failed reset
and Snapshot operations; active and unresolved rows are protected and
instance-delete history remains unbounded.

After Step 8 proof, the smallest next Phase 7 product slice is typed
lifecycle-operation history plus a unified read projection and narrow instance
detail/history presentation. Proof-gated reconciliation resolution remains a
separate later architecture decision because it introduces mutation authority.

Phase 7 Step 9 implements that bounded history/presentation slice. Migration
`0016_lifecycle_history` adds one closed typed lifecycle-operation table for
provision/start/stop/deprovision with semantic prior/result state, original
instance identity, a nullable live FK, safe outcome data, and newest-20 ordinary
retention that protects unresolved rows. Synchronous lifecycle authority and
normal broker postconditions remain unchanged; lifecycle is not converted into
a durable queue.

The Step 8 stale dispatcher now terminalizes an exactly owned expired lifecycle
row from trusted semantic observation and releases its exact lease without
resuming work. Stable observed state warns without over-blocking, while
transitional/ambiguous state uses the central gate to block all instance
mutations. A read-only `ManagedInstanceActivityItem` projection deterministically
merges lifecycle, reset, Snapshot create/restore/delete, and instance-delete
history without duplicate persistence.

Authenticated instance detail composes existing DTOs for exact runtime,
fixture, readiness, Snapshots, reconciliation, current escaped notes, and ten
recent items; full history is uncapped within retained data. Deleted-instance
list/detail views are anchored by instance-delete history and immutable original
UUID, omit notes-at-deletion, and link preserved archive-only Snapshots back to
history. No edit, restore, undelete, retry, acknowledgment, finalization,
repair, generic export, or configurable retention control is introduced.

The proof-gated reconciliation-finalization architecture gate concluded that
explicit finalization is not required by canonical v1 or Phase 7. Phase 7 is
complete at Step 9; optional finalization remains unapproved future enhancement
work and is not part of Phase 8.

Phase 8 is the canonical add-on development workflow phase. Step 1 adds only the
global local-directory `DevelopmentSource` catalog. One explicitly configured
development root confines normalized relative locators; descriptor-relative
no-follow validation reads a bounded safe root-level `addon.xml` and derives
stable add-on identity plus registration-time name, version, and extension
observations. UUID, logical name, locator, and parsed add-on ID provide immutable
catalog identity while source content remains mutable. The schema is create/read
only, and the conservative application DTO contains no host path.

At the Step 1 checkpoint, ManagedInstance attachment, deployment, copy/sync,
ZIP installation, repository URL testing, Git semantics, add-on inventory,
source watchers, history, reconciliation, broker/Docker capability, and Web
mutation remained separately authorized future Phase 8 milestones. The later
raw-bind gate rejected direct mutable host-path binding.

Phase 8 Step 2 adds only durable per-disposable-instance DevelopmentSource
attachment. The many-to-many association is mode-neutral, preserves unique
add-on identity per instance, survives reset and Snapshot operations unchanged,
and is removed on successful instance deletion without deleting the global
source. Attach performs confined read-only metadata revalidation and rejects a
changed add-on ID while accepting changed observed name/version without catalog
mutation.

Attach/detach are synchronous database-oriented configuration mutations under
the existing per-instance lease and central reconciliation gate. Typed terminal
history survives instance deletion and joins unified activity. No global-heavy
lease, deployment state, broker/Docker source operation, Web mutation, copy,
sync, ZIP, repository URL, watcher, or persistent-instance workflow is
introduced by Step 2.

Phase 8 Step 3 adds only authenticated read-only source presentation. Global
HTML and JSON list/detail routes use a path-free application projection with
stored registration observations, validation time, and current live attachment
counts/details. Instance detail lists attached sources and links back to source
detail; source detail links current live instances and their retained history.
Deleted instance attachment operations remain visible only through existing
typed deleted-instance history.

GET requests perform no source-filesystem validation or catalog refresh. No
schema, source mutation form, attach/detach route, deployment state, source
browser/content access, broker method, Docker access, or reconciliation behavior
is added. Stopped-only copy/sync deployment requires a separate architecture
gate.

The later administrator product-boundary slice adds the missing catalog and
association mutations without changing the Step 1/2 core. The Development
Sources page now submits only bounded `logical_name` and `relative_locator` to
authenticated same-origin CSRF-protected `POST /api/development-sources`.
Strict schema handling rejects every additional field. The route delegates to
`DevelopmentSourceAdministrationService`; configured-root selection,
descriptor-relative no-follow lookup, `addon.xml` validation, identity parsing,
normalization, and duplicate rejection remain application/validator authority.
Responses and errors are path-free and do not echo locators, configured roots,
raw XML, exceptions, or OS failures.

Instance detail supplies a compact advisory choice of currently unattached
registered source identities and explicit detach controls. Strict-empty-body
`POST .../attach` and `POST .../detach` accept only instance/source UUIDs and
delegate to `DevelopmentSourceAttachmentService`. Existing disposable-only,
revalidation, add-on-ID collision, reconciliation, and mutation-lease rules are
unchanged. Association changes perform no materialization, destination cleanup,
catalog deletion, enablement, smoke, broker, Docker, or Kodi lifecycle action.
Existing typed attachment operation history continues to project into instance
activity. Registration time is presented from the create-only catalog row; no
new activity table or migration is introduced.

Before Phase 8 Step 4 deployment, the runtime-ownership prerequisite extends
every immutable RuntimeExecutionProfile with required bounded `runtime_uid` and
`runtime_gid`. Migration `0019_runtime_execution_ownership` has no default: it
backfills only the exact retained curated revision/profile/image identities
whose ownership was proven from repository-controlled builders and exact pinned
image inspection, and fails on any unknown row. A read-only trusted resolver now
provides ManagedInstance to RuntimeRevision to RuntimeExecutionProfile ownership
without any caller ownership parameter. Existing Snapshot and fixture archive
normalization remains unchanged. No DevelopmentSource deployment authority is
added by this prerequisite.

Phase 8 Step 4 implements the manual stopped-only `local_directory` copy/sync
slice. Migration `0020_development_source_copy_sync` adds separate last-verified
deployment evidence and durable typed operation history, plus closed mutation
and global-heavy coordination kinds. Every request reserves both leases and
uses the central stale/reconciliation dispatcher; unresolved work is never
resumed or recopied.

Application revalidation and broker-owned two-pass no-follow capture close the
source TOCTOU boundary. Fixed recursive bounds, exact VCS administration
directory exclusions, normalized modes, immutable private staging, and
RuntimeExecutionProfile UID/GID produce a deterministic source manifest and
changed/new-only delta. The destination is server derived. Initial deployment
requires absence; sync requires fresh complete destination readback to match
the retained complete destination digest exactly. Previously verified paths
removed from source are preserved without deletion and remain in new full
destination evidence; external destination drift conflicts before apply.

The strict authenticated empty-body POST returns 202 and no caller path,
ownership, destination, archive, policy, restart, or override authority. Success
proves filesystem materialization only. At the Step 4 checkpoint, Kodi
recognition/inventory, ZIP installation, repositories, enablement, JSON-RPC,
watchers, and MCP development authority remained future architecture milestones;
direct mutable host-path binds were later rejected.

Phase 8 Step 5 implements live Kodi-recognized add-on inventory. Authenticated
`GET /api/instances/{instance_id}/addons` and the instance-detail Add-ons section
use a fixed unprivileged `Addons.GetAddons` adapter only after existing trusted
container/runtime/network observation proves Kodi is already running and
process-healthy. The adapter performs a fresh identity check, fixed installed/all
enumeration, strict complete-result normalization, deterministic sorting, and
bounded timeout, response, page, item, and string handling across Kodi 19.5,
20.5, 21.3, and 22.0 Beta 2.

The observation is live, no-store, GET-only, not persisted, and not activity
history; Alembic remains at `0020_dev_source_copy_sync`. Instance detail remains
available when inventory is not. No request can start/wait/restart Kodi or
supply networking, credentials, JSON-RPC method, filters, properties, container,
or path information, and no broker/MCP authority is added.

Provenance is either unknown or precise historical DevelopmentSource evidence:
Kodi recognizes the same ID/version last successfully verified copied for the
same source, instance, revision, and exact current container. This is presented
only as a prior verified copy and never proves current bytes, installation,
enablement, or function. Stale/unknown deployment confidence and every mismatch
remain unknown. At the Step 5 checkpoint, fresh destination readback,
Snapshot/repository provenance, durable observations, enable/disable, dependency
installation, and ZIP installation remained separately gated.

The approved Phase 8 runtime compatibility correction versions Python bytecode
behavior as immutable execution-profile policy. Migration
`0021_runtime_python_bytecode_policy` backfills all retained profiles as
`interpreter_default` without a future-row default. New suppression revisions
are Kodi 19.5 revision 2, Kodi 20.5 revision 2, Kodi 21.3 revision 3, and Kodi
22.0 Beta 2 revision 2. Each reuses its exact predecessor image while adding a
new required profile with `suppress`, then follows ordinary approval, trust, and
`kodi-runtime-v1` evidence gates before becoming newest eligible.

Provisioning alone translates `suppress` into exact
`PYTHONDONTWRITEBYTECODE=1`; callers receive no environment field or Python
switch. Deterministic composition reserves that key to profile policy and both
provisioning drift checks and running-runtime observation verify its exact
presence/value or required absence. Historical instance and Snapshot pins do
not move. The correction changes no DevelopmentSource manifest, destination
readback, exclusion, replacement, or provenance rule. Normal Python imports on
the new revisions can therefore coexist with strict Step 4 unchanged sync;
intentional add-on writes remain conflicts. A containment-safe live-source
projection is optional post-v1 research, not a Phase 8 completion requirement.

Phase 8 copied-add-on enablement adds migration
`0022_dev_source_enablement`, the typed synchronous
`DevelopmentSourceEnablementOperation`, and per-instance
`development_source_enablement` coordination without a global-heavy lease.
The operation validates exact attachment, current runtime/container, retained
Step 4 success and confidence, and a fresh broker-derived complete destination
capture before querying Kodi.

The source descriptor parser now normalizes bounded strict dependency imports
and classifies Python and service extension behavior. Service/auto-start add-ons
and interpreter-default Python execution-capable sources are rejected. Fixed
Kodi inventory/details are cross-checked with source declarations; missing,
disabled, unknown, mismatched, and version-unverified required dependencies
block without calling enable. Optional missing dependencies are allowed. No
dependency installation or implicit dependency enablement is authorized. The
fixed API has no authoritative version-compatibility Boolean, so every actual
required version constraint is fail-closed even on exact string equality;
Kodi's exact neutral `0.0.0` unset-bound sentinel alone maps to no constraint.

The sole mutation sends server-derived ID plus Boolean `true`, then requires a
fresh exact enabled postcondition. Already enabled is idempotent and does not
send the mutation. Post-mutation ambiguity enters durable reconciliation and is
never retried; interruption known to precede mutation is ordinary failure.
Typed activity and newest-20 ordinary retention follow existing conventions.

The authenticated strict-empty-body endpoint returns the completed synchronous
operation with HTTP 200. Instance UI integrates bounded advisory enablement and
dependency states. The implemented ladder stops at enabled: attached,
materialized, recognized, enabled, dependencies generally unproven, working
unproven, tested unproven. At that checkpoint execution/smoke testing, bind
mounts, and ZIP installation remained architecture-gated future milestones.

Phase 8 copied-add-on smoke testing implements the next approved first slice.
Migration `0023_dev_source_smoke_test` adds typed durable history and the
per-instance `development_source_smoke_test` mutation kind, with no current
smoke-state field and no global-heavy lease. Retention keeps newest 20 ordinary
terminal rows per original instance plus all active and unresolved history.

Eligibility is limited to copied suppression-runtime Python video plugins with
one executable descriptor, exact `video` provides, a safe present library, and
only passive metadata companions. The asynchronous worker repeats deployment,
runtime, recognition, enabled-state, dependency, and exact destination
preconditions, then invokes only fixed bounded `Files.GetDirectory`. Correlated
responses receive mandatory post-readback; returned content is discarded.

Timeout or crash after dispatch is reconciliation-required and makes confidence
unknown. It is never retried or resolved from an immediate match. Explicit
ordinary stop remains separate and permitted; once the exact original container
is stopped or absent the warning may become nonblocking without rewriting
history. The Web mutation is strict-empty-body HTTP 202 with a bounded status
URL and a non-egress-isolation warning.

**Smoke passed is bounded historical evidence that one fixed plugin-directory
invocation completed and preserved deployed-source provenance. It is not a
general working/health guarantee.**

The approved uploaded-ZIP first slice is implemented after copied-add-on smoke:

- migration `0024_addon_zip_installation` adds immutable accepted artifacts,
  durable asynchronous import and installation history, current typed evidence,
  and the two new global/per-instance coordination kinds;
- a bounded pre-extraction ZIP validator and private UUID-only storage publish
  the immutable original and normalized canonical payload;
- the narrow broker derives the fixed destination and profile ownership, and
  applies only an accepted artifact under exact stopped-container authority;
- the operation-owned running → stopped → running choreography requires absent
  inventory/destination, two exact filesystem readbacks, exact disabled Kodi
  recognition, and unchanged declared dependencies;
- upload/install Web actions are authenticated, origin/CSRF protected, HTTP 202,
  and expose no filename, member, content, path, extraction, Docker, or Kodi
  installer authority.

**ZIP installation here is Lab-controlled validated materialization, not Kodi's
native Install-from-ZIP GUI/installer path.** Replacement, upgrade, enablement,
smoke, packaging, repository integration, and artifact deletion remain
separately gated. Raw direct bind is rejected rather than deferred v1 work.

The ZIP-specific enablement first slice follows installation with migration
`0025_addon_zip_enablement`. Authority is exact ManagedInstance UUID plus
accepted immutable AddonZipArtifact UUID and retained verified installation
evidence. A fresh ZIP-scoped exact-tree verifier precedes complete inventory,
fixed details, pure dependency preflight, and fixed enable-true. Changed or
unknown payload blocks and degrades only target evidence. Dependency blocks send
no mutation; already enabled is idempotent only after all checks.

Separate typed history, per-instance coordination, newest-20 ordinary
retention, reconciliation, activity, strict-empty-body HTTP 200, and bounded UI
states are included. There is no authoritative enabled-state table. Existing
Snapshot/reset/runtime/container/provenance semantics remain evidence-driven.
ZIP smoke execution follows as migration `0026_addon_zip_smoke_test`. It adds a
separate asynchronous operation, per-instance mutation kind, exact ZIP payload
readback before and after the fixed directory invocation, pure dependency
preflight, bounded status/history, timeout containment through explicit stop,
and no global-heavy lease. It persists no returned plugin content, URI, paths,
raw errors, JSON, or logs.

**ZIP smoke passed is bounded historical evidence that one fixed
plugin-directory invocation completed and the installed tree still exactly
matched the immutable ZIP payload. It is not a general correctness or health
guarantee.**

At the current Phase 8 implementation checkpoint, DevelopmentSource
catalog/validation and
attachment cores, explicit changed/new-only non-deleting copy/sync, inventory,
bytecode policy, DevelopmentSource enablement/smoke, uploaded-ZIP
import/install/enable/smoke, and the repository URL testing first slice are
implemented. Migration `0027_addon_repository_testing` adds canonical public
HTTPS sources, per-instance attachments, pinned DNS/TLS fetch, immutable
snapshots/candidates, server-derived same-origin packages, strict ZIP reuse,
repository artifact/evidence lineage, and repository-specific absent-only
installation. Attached same-ID DevelopmentSources block before lifecycle
mutation. Uploaded-ZIP enable/smoke authority is not widened.

Migration `0028_repository_replacement_override` implements the final canonical
capability: one explicit, 15-minute, exact-state replacement plan for a
repository-download artifact against a currently attached same-ID source. It
supports only absent/unmaterialized and exactly verified copied incumbents;
Class B requires a recent central Snapshot. Immediate approval binds one
attempt, the broker can only overwrite/add within the fixed destination, and
enabled/dependency state must be preserved. Attachment retention, derived
override state, `repository_override_active` copy/sync blocking, evidence
transitions, strict private Web review, and post-marker stopped reconciliation
are included.

**Phase 8 — Add-on Development Workflows: COMPLETE.** The formal completion
review found every current canonical requirement complete at implementation
checkpoint `a595136b9689119b1f623704dd5faffe240a481d`, Alembic head
`0028_repository_replacement_override`, with 1612 collected tests. The supported
and explicitly deferred boundaries are recorded in
`PHASE8-ADDON-DEVELOPMENT.md`.

Containment-safe live projection or equivalent bind-like behavior is optional
post-v1 research and is not a completion blocker. Raw direct Docker bind of
mutable DevelopmentSource host paths is not approved.

## Phase 9 — Agent working copy

Slice 1 implements only workspace catalog, safe creation, immutable baseline,
and bounded read/inspect. Migration `0029_agent_workspaces` adds path-free
`AgentWorkspace` state plus typed asynchronous creation history and the
`agent_workspace_creation` global-heavy kind. One registered local-directory
DevelopmentSource produces each independent UUID workspace through the reused
Phase 8 no-follow bounded capture. A matching second pass and captured add-on
identity are required. Generation zero and the private baseline derive from one
normalized capture and must recapture identically before ready publication.

Authenticated administrator Web routes provide list/detail, strict UUID-only
HTTP 202 creation/status, bounded semantic directory inspection, and strict
UTF-8 text reads capped at 2 MiB. They expose no host/storage path, baseline,
manifest/digest, filesystem ownership, Git, runtime, instance, Docker, or Kodi
control. The ordinary application identity performs all work.

Slice 2 completes the canonical working-copy read, directory-inspect,
write/create/replace, exact patch-edit, and derived change-review capabilities.
Each synchronous text mutation is limited to workspace UUID plus relative path,
requires exact optimistic generation, serializes per workspace, and performs
complete safe pre/post recapture around atomic same-filesystem publication.
The fixed resulting-file bound is 2 MiB. Create is absent-only beneath an
existing directory; replacement/patch requires a single-link UTF-8 regular file.
No delete, rename, mkdir, binary write, type transition, metadata control, or
caller temporary path is added.

Migration `0030_agent_workspace_mutations` adds only content-free generation
activity. Baseline/current comparison returns added and modified files with
bounded previews and classifies a valid root descriptor ID change without
rewriting immutable provenance. Unexpected deletion, binary change, unsafe
object, VCS administration directory, type change, or digest drift fails closed;
uncertain filesystem/metadata agreement becomes reconciliation-required.

Slice 3 implements D-0207, D-0208, and the source-side subset of D-0210.
Migration `0031_agent_workspace_promotions` provides exact three-way private
plans and complete review, 15-minute single-use approval, an unprivileged
permission-gated source apply, exact private backup, multi-file journal,
conditional reverse rollback, restart reconciliation, promoted workspace state,
source deployment-confidence staling, and one exact selected-target sync
handoff. Fixed per-workspace, per-source, target, and global-heavy coordination
protects planning and execution. The Web flow exposes no source/storage paths,
digests, container identities, filesystem metadata, or generic write controls.

Slice 4 implements D-0209 and the verification subset of D-0210. Migration
`0032_promotion_verification` adds single-use promotion verification, exact
same-container stop/sync/start/readiness, fresh deployment evidence, inventory
presence/version/type/enabled proof, and fixed-path bounded log correlation.
Queueing reserves the exact handoff without consumption; only exact sync
readback and fresh evidence atomically consume it. Reserved failures remain
non-reusable, including reconciliation-required ambiguity.
Source success remains immutable across succeeded, failed, incomplete, and
reconciliation-required verification outcomes.

Slice 5 completes D-0210 and the remaining Phase 9 implementation scope.
Migration `0033_agent_workspace_cleanup` adds lifecycle timestamps, independent
present/cleaned byte state, a durable global-heavy UUID-only cleanup operation,
and content-free abandonment/cleanup activity. Ready workspaces may be
explicitly abandoned without deleting bytes. Abandoned and promoted workspaces
may be explicitly cleaned only after all relevant plans and operations are
terminal; reconciliation-required state always blocks. Descriptor-relative
no-follow deletion removes only the exact workspace baseline/tree subtree and
retains all database history, promotion backup, handoff, verification, and log
evidence. There is no automatic, age, pressure, source, status, or bulk pruning.

Completion-audit remediation adds migration
`0034_phase9_completion_remediation`, atomic source-success/verification-child
publication, retained nullable live-target history, deterministic obvious-secret
snippet redaction, and direct privilege-policy coverage for the root-only Phase
9 validator. The second completion review's two remaining blockers are also
remediated: verification publication is a mandatory service-construction
dependency with execute-only later dispatch, and redaction now covers the full
bounded attributable log buffer before its retained-window cut.

**Phase 9 — Agent Working Copy: COMPLETE.** The final formal completion review
validated implementation HEAD `e25a502597b94ffa46f0710059f9a09cc0d6086b`,
Alembic head `0034_phase9_completion_remediation`, and 1770 collected tests.
The completed canonical workflow is registered local source → independent
Lab-managed working copy → bounded read/inspect/edit/patch → complete
three-way Web review and one-shot confirmation → exact canonical-source
promotion with backup/rollback/reconciliation → mandatory durable
promotion-bound selected-target verification → explicit abandonment and
UUID-only cleanup with retained history.

Phase 9 adds no shell, generic container/filesystem, root, Docker, Git, or MCP
authority. Registration remains read trust; source promotion requires separately
provisioned least-privilege OS write authority and makes that writable source
part of the application trusted-computing base. Source success cannot commit
without exactly one durable verification child and reserved single-use handoff,
and complete bounded attributable log content is redacted before retained-output
truncation. MCP exposure and autonomous agent identities remain Phase 11/12
work. Git/remotes/PR creation, delete/rename/mkdir and binary editing, direct
workspace deployment, arbitrary test execution, optional add-on-data writes,
automatic retention, backup/log-evidence pruning, and broader rollback UX remain
future non-blocking work. Phase 10 begins below with Slice 1 only.

Phase 10 is **Browser remote/display**. Slice 1 implements only managed
screenshot capture for exact Kodi 21.3 stable under D-0211, D-0212, D-0213,
D-0216, and the screenshot subset of D-0218. It adds fixed broker-owned Xvfb
capture with a private UUID-derived artifact handoff, unprivileged strict
fixed-RGB-to-PNG conversion, migration `0035_managed_screenshots`, a durable
latest-only private cache with failed-refresh retention, authenticated Web APIs,
and manual instance-detail presentation. Capture is synchronous, one per
instance and four globally, with exact pre/post lifecycle identity checks.

This slice does not implement D-0214/D-0215 remote behavior, D-0217 interactive
display, or D-0219 MCP handoff. It also adds no browser remote, navigation or
player action, auto-refresh, keyboard/text input, noVNC/VNC, WebSocket/display
port, tokens, client/session identity, agent/workspace authority, source
promotion change, general log access, lifecycle command, shell, Git, or generic
Docker/filesystem/process surface.

Phase 10 Slice 2 completes managed screenshot parity across the exact current
curated revisions for Kodi 19.5 Legacy, 20.5 Current, 21.3 Current, and 22 Beta
2 Preview. `ManagedScreenshotService`, the broker operation, Web routes, UI,
latest-only cache, deletion behavior and migration `0035_managed_screenshots`
remain common. The internal raw artifact now carries bounded server-observed X11
layout metadata; conversion admits only the closed 1280x720 TrueColor depth-24,
32-bpp, 32-bit-padded RGB888 representation. All four pinned images live-proved
that same LSB-first representation, valid non-uniform PNG output, authenticated
delivery and atomic replacement. Kodi 20.5 also proved failed-refresh retention.

No migration or new public API was required. The mandatory screenshot portion
of canonical Phase 10 is complete. D-0214/D-0215 browser remote and D-0217
optional mediated interactive display remain separate work; D-0219 MCP remains
Phase 11/12 handoff. Slice 2 adds none of those authorities and adds no generic
exec, X11, artifact-transfer, Docker, filesystem, lifecycle, log, workspace,
token, or agent surface.

### Phase 10 Slice 3A completion checkpoint

Fixed semantic browser remote controls are implemented and live-proven on exact
Kodi 21.3 only. D-0214 defines the 16-value closed enum and fixed internal
Input/Application/Player mappings. D-0215 defines the short per-instance lease,
exact identity binding, one-attempt/no-retry semantics, server-derived active
player, and separate remote/screenshot outcomes. The remote subset of D-0218
defines bounded semantic audit/errors and a 10 actions/second, burst-20
Web-session/instance token bucket.

Every successful navigation action immediately reuses the latest-only managed
screenshot service and updates the browser image without page reload. Playback
and volume intentionally omit capture because canonical V1 requires
post-navigation refresh. There is no migration or remote-history table. No
keyboard/text input, interactive display/noVNC, MCP, tokens, agent authority,
general logs, or cross-version remote parity is included. Remote parity across
Kodi 19.5, 20.5, 21.3, and 22 Beta 2 remains a separate review milestone.

### Phase 10 browser-remote parity completion checkpoint

The existing closed browser remote is now enabled and live-validated on the
four exact curated revisions: Kodi 19.5, 20.5, 21.3, and 22 Beta 2. Every action
uses the same server-owned JSON-RPC mapping on all four; no runtime-specific
mapping was required. `Player.GetActivePlayers` returned the expected empty
list for the idle fixture on every runtime, producing the bounded
`remote_no_active_player` result without caller-selected player authority.

Each successful navigation action produced exactly one new screenshot capture
UUID with a newer timestamp. Hash comparisons proved visible framebuffer changes
where navigation changed the UI, without OCR. Volume and mute succeeded, direct
browser-to-Kodi JSON-RPC remained absent, and disposable cleanup and protected
isolation passed for every runtime. There is no migration. D-0214, D-0215, and
the browser-remote portion of D-0218 are complete across the curated matrix.
D-0217 mediated interactive display remains unimplemented and is not started by
this checkpoint.

### Phase 10 Slice 4 implementation-complete checkpoint

Exact curated Kodi 21.3 now has one authenticated same-origin view-only live
display under D-0217 and the display subset of D-0218. The browser explicitly
opens a WebSocket that receives validated binary PNG only, at no more than one
frame per second. There is one stream per instance, two globally, a 15-minute
hard lifetime, a two-second send bound, and zero queued frames. Session and
runtime identity are revalidated every interval; process-local cancellation
lets lifecycle and other conflicting mutations proceed immediately.

The stream reuses `capture_managed_kodi_screenshot` and the common application
raw-frame decode, PNG encode, and validation path. Ephemeral frames are returned
in memory after raw staging cleanup and never create or update screenshot
metadata, publish a durable PNG, replace the latest cache, or emit per-frame
audit. Manual screenshots and post-navigation durable refresh remain unchanged.
The visual projection adds only `display_supported`, `display_available_now`,
and `display_backend`; all non-21.3 runtimes are `display_unsupported`.

The UI defaults to the latest screenshot, starts live display only explicitly,
keeps the semantic HTTP remote visible, and restores the durable screenshot on
close. Client messages are rejected and the frame has no keyboard or pointer
authority. There is no migration, new broker operation, VNC/noVNC, image/runtime
rebuild, MCP, token, agent authority, general logs, higher-FPS video, or
additional-runtime display parity.

The required V1 Phase 10 implementation now consists of four-runtime managed
screenshots, four-runtime semantic browser remote, and one mediated interactive
display path. It is **implementation-complete pending formal review**. Optional
future items—3-second auto-refresh, keyboard/full-text, additional-runtime
display parity, VNC/noVNC, higher-FPS video, and MCP—are not Phase 10 completion
requirements and remain unimplemented.

### Phase 10 formal-review resource-accounting remediation

The two resource-accounting defects found by the formal completion review are
remediated without changing Phase 10 authority or protocol. Browser-remote rate
buckets now have monotonic 15-minute idle expiry, explicit logout retirement,
and a hard 4,096-bucket ceiling that rejects a new key before dispatch when no
expired bucket can be safely removed. Live-display registry capacity is now
cleanup-protected from acquisition through WebSocket acceptance and stream
teardown; acceptance failure releases both instance and global capacity and
does not emit a successful-start audit event. There is no migration. Phase 10
remains implementation-complete pending a rerun of the formal completion
review; this remediation does not declare Phase 10 complete.

### Phase 10 formal completion checkpoint

**Phase 10 — Browser remote/display: COMPLETE.** The independent closeout audit
validated implementation checkpoint
`9821463b22c1d03a619571455f24e22853c0134e`, Alembic head
`0035_managed_screenshots`, and 1884 collected tests. It independently
revalidated the bounded remote limiter and exception-safe display capacity,
including many-key cardinality pressure and actual WebSocket accept failure.

The completed V1 scope is four-runtime managed screenshot parity, four-runtime
closed semantic browser-remote parity, and an explicit view-only mediated live
display for exact curated Kodi 21.3. Authentication, CSRF/same-origin and trusted
Host checks, strict request schemas, server-built JSON-RPC, at-most-once remote
dispatch, lifecycle-winning coordination, exact runtime/container binding,
session revalidation, zero-frame-queue backpressure, private ephemeral frame
handling, and bounded audit/privacy semantics remain part of the completed
boundary. No display-session, browser-remote, or Phase 11 schema was added.

Approximately three-second screenshot auto-refresh, keyboard/full-text input,
additional-runtime live-display parity, VNC/noVNC, and higher-frame-rate video
remain optional future work. Phase 11 MCP architecture may begin only as a new
separately reviewed milestone; Phase 10 introduced no MCP transport, token,
client/agent identity, session-default target, schema/tool, or workspace
exposure.

## Phase 11 Slice 1 checkpoint

Phase 11 Slice 1 implements only the modern authenticated read-only MCP
foundation under D-0219 through D-0225 and D-0228/D-0229. Official Python SDK
`mcp==2.1.1` is exact-pinned for reviewed protocol `2026-07-28` behavior.
Existing FastAPI serves exact POST `/mcp`; an outer version gate prevents the
SDK's legacy fallback path. There is no GET stream, initialize/initialized,
`Mcp-Session-Id`, transport session, selected/default target, new listener,
nginx/systemd change, CORS expansion, or stdio adapter.

Migration `0036_mcp_core_foundation` adds deterministic built-in presets,
open-ended permissions, durable clients, separate SHA-256 verifier-only
credentials, exact managed-target grants, target-side preset assignments, and
bounded security-administration events. Web administration provides client,
preset, grant/policy, and issue/revoke/atomic-rotation lifecycle with show-once
tokens. Admission has bounded peer/client token buckets and exception-safe
per-client/global concurrency.

The static registry and common authorize-then-invoke service support later
semantic tools, permissions, target classes, capabilities, states, and rate
classes without replacing transport or identity. Production inventory contains
only lexical `target_list` and `target_status`, both over
`ManagedRuntimeStatusService`, with explicit target UUID and bounded path-free
output. Kodi Lab MCP schema version is `1`.

Phase 11 remains **PARTIAL**. The next separately approved slices still own
canonical authenticated stdio, any reviewed `2025-11-25` compatibility,
inventory/screenshots, semantic remote control and durable action receipts,
and richer Kodi operations. Phase 12 owns working-copy/development tools;
Phase 13 external development targets; Phase 14 external production policy;
Phase 15 diagnostics. No later capability is claimed by this checkpoint.

The applicable official protocol check uses exact runner
`@modelcontextprotocol/conformance@0.2.0-alpha.11` through the test-only
authenticated wrapper in `scripts/phase11_mcp_conformance_app.py`. The runner's
fixture-only sampling/subscription probes and unimplemented prompts, resources,
media, MRTR, OAuth-client, and later tool-catalog scenarios are explicit
exclusions, not Phase 11 conformance claims.

## Phase 11 Slice 2 checkpoint

Phase 11 Slice 2 independently revalidated the Slice 1 modern protocol,
credential lifecycle, exact grants/privacy, preset intersection, static registry,
future target migration path, and bounded admission. Narrow remediation makes
unknown well-formed credentials execute the constant-time digest comparison
primitive against a dummy verifier and makes common authorization enforce the
registry's target/global scope classification.

Installed `kodi-lab-mcp-stdio` is the canonical second transport. It is a thin
unprivileged `2026-07-28` stdio-to-loopback-HTTP proxy: every request reaches
the existing `/mcp` endpoint with the bearer from `KODI_LAB_MCP_TOKEN`. It has
no database/service construction, tool registry, local authentication or
authorization, grant/target cache, session/default target, durable state,
daemon, broker, Docker, Kodi, or filesystem authority. An optional endpoint
override is limited to exact plain-HTTP loopback `/mcp`, with redirects off.
Frames retain the 64 KiB request bound and local concurrency/queueing are small
and backpressured. EOF cancels outstanding work.

Pinned official SDK interoperability now covers direct HTTP and stdio for
`server/discover`, identical `tools/list`, `target_list`, and `target_status`,
including equivalent unknown/unauthorized decisions and next-request grant,
disable, expiry, and revoke behavior. The official conformance runner remains
direct-HTTP-oriented; stdio proxy conformance is demonstrated by the official
SDK plus raw modern wire assertions rather than a claim that the runner models
the proxy topology.

There is no migration; Alembic remains `0036_mcp_core_foundation`. Production
inventory remains exactly `target_list` and `target_status`. Custom-preset
creation remains deferred and must be resolved before Phase 11 completion; the
Production Remote Kodi preset remains inactive/unassignable. Phase 11 remains
**PARTIAL**: add-on inventory, native MCP screenshots, semantic remote control,
durable replay/action receipts, any separately approved `2025-11-25`
compatibility, final client/conformance matrix, and closeout remain.

## Phase 11 Slice 3 checkpoint

Phase 11 Slice 3 adds only read-only target observation. Production inventory is
exactly lexical `addon_list`, `screenshot_capture`, `target_list`, and
`target_status` over the existing modern HTTP authority and canonical stdio
proxy. The new tools use the common registry and authorize-then-invoke boundary;
there is no handler-local authentication, grant logic or alternate stdio
service path.

`addon_list` reuses `ManagedInstanceAddonInventoryService`, defaults to 50 and
caps at 100, preserves stable add-on-ID ordering, and returns bounded semantic
identity/type/enabled/provenance fields only. `screenshot_capture` reuses the
durable `ManagedScreenshotService` and fixed broker primitive, returns official
native `image/png` content plus compact metadata, and updates the same latest
cache. Failed capture retains the previous cache; the per-instance and global
four-capture limits remain service-owned and no retry is added.

Migration `0037_mcp_observation_permissions` adds `target.visual` to Add-on
Development, Read-only Inspection and Full Lab Control. Production Remote Kodi
remains empty/unassignable and `target.control` remains reserved. The stdio
proxy's former 128 KiB HTTP-response ceiling is replaced by an incrementally
enforced 8 MiB-PNG base64 envelope plus 64 KiB overhead; its four workers,
eight-item request queue and stdout backpressure remain bounded.

Phase 11 remains **PARTIAL**. Slice 3 adds no remote action, playback or GUI
mutation, replay receipt, custom-preset administration, legacy `2025-11-25`
compatibility, lifecycle, snapshot, settings, library/music query, add-on
execution, logs/diagnostics, external target or Phase 12 workflow. The remaining
Phase 11 gates are semantic remote control with durable replay safety, custom
presets/compatibility resolution, final client/conformance matrix and formal
closeout.

Applicable official runner coverage remains tool listing, DNS rebinding and
HTTP header validation. The runner's image scenario is fixture-specific and
expects a non-production `test_image_content` tool; Kodi Lab instead verifies
the production screenshot tool with the pinned official SDK across direct HTTP
and the canonical stdio subprocess.

## Phase 11 Slice 4 checkpoint

Phase 11 Slice 4 adds only replay-safe semantic remote control. Production
inventory is exactly lexical `addon_list`, `remote_action`,
`screenshot_capture`, `target_list`, and `target_status`. `remote_action` accepts
only UUID `request_id`, explicit `target_id`, and the existing closed 16-action
enum. It is registry-classified mutating, requires `target.control`, admits only
internal managed targets with exact semantic-remote capability and current
running/ready/reconciled state, and is not production-eligible.

Migration `0038_mcp_remote_control` adds `target.control` to Add-on Development
and Full Lab Control only and creates the durable action-receipt table. Read-only
Inspection is unchanged; Production Remote Kodi remains empty/unassignable.
Receipts are unique by durable client UUID plus request UUID, record credential
UUID only for first-attempt audit, survive target deletion without cascading,
and contain only bounded semantic state/result/refresh metadata. Retention is 30
days with claim-time cleanup, a 10,000/client ceiling and a 100,000 global
ceiling.

Authorization precedes both new claims and duplicate reads. A new `pending`
receipt commits before the existing shared remote limiter and before one call to
the refactored actor-aware `BrowserRemoteService`; completion publishes in a
separate transaction. Exact completed duplicates bypass the limiter and return
stored results. Pending or uncertain duplicates return
`remote_action_ambiguous`; parameter mismatch returns `request_id_conflict`.
Process restart and concurrent claim tests prove no memory-only assumption.
Direct HTTP/stdio tests prove both replay directions with explicit one-dispatch
counts. Navigation retains one durable refresh, playback retains server-derived
active-player behavior, and raw JSON-RPC remains absent.

Phase 11 remains **PARTIAL**. Slice 4 adds no lifecycle, snapshot, raw JSON-RPC,
settings, add-on execution, library/music, external/production target,
diagnostic, Phase 12, legacy protocol, or custom-preset capability. Remaining
gates are custom presets, decision and any approved implementation for
`2025-11-25`, the final client/conformance matrix, and formal closeout.

## Phase 11 Slice 5 checkpoint

Slice 5 completes the remaining implementation gates without expanding the
production tool catalog. Custom presets clone one active immutable built-in into
a new UUID and trimmed bounded unique display name. Permissions are an immutable
creation-time snapshot; there is no granular editor, rename, delete,
custom-from-custom chain, Production Remote Kodi template, or silent inheritance
of future permissions. Existing client and internal-target assignment paths
apply the clone on the next request and record bounded create, assign, and
unassign audit events. Existing tables already represent this safely, so no
migration is added and Alembic remains `0038_mcp_remote_control`.

Primary HTTP and canonical stdio remain exact modern `2026-07-28`. Exact legacy
`2025-11-25` is additionally supported on the same authenticated POST `/mcp`
route with pinned SDK stateless mode. The initial headerless legacy initialize
must explicitly offer that version; later traffic carries it. There is no
cross-era fallback, `Mcp-Session-Id`, GET stream, application session, selected
target, protocol-specific credential, protocol permission, or authority cache.
Both eras share request-size, failed-auth, authenticated-client, concurrency,
registry, grant, policy, class, capability/state, replay, audit, and privacy
boundaries. Legacy stdio is deliberately unsupported rather than adding proxy
negotiation state or a second server.

Pinned official SDK `mcp==2.1.1` covers modern explicit, modern auto-discovery,
canonical stdio, and legacy HTTP with catalog/schema equivalence, both replay
directions, and next-request grant, policy, preset, disable, revoke, and privacy
effects. OpenCode `1.14.39` connects over exact legacy HTTP; its legacy stdio
handshake is unsupported. Codex CLI `0.154.0` is tested-incompatible because it
offers excluded `2025-06-18`; no untested-client claim is made.

Official conformance runner `@modelcontextprotocol/conformance@0.2.0-alpha.11`
passes modern tools-list (3/3), DNS (2/2), and header validation (14/14), plus
the applicable tools-cache checks. Modern stateless is 23/25 with only the
absent runner diagnostic `test_missing_capability` accounting for two checks;
caching failures are absent prompt/resource methods. Legacy initialize (2/2),
ping (2/2), tools-list (3/3), and DNS (2/2) pass. Other canned fixture tools and
unimplemented optional surfaces remain classified exclusions.

Production remains exactly lexical `addon_list`, `remote_action`,
`screenshot_capture`, `target_list`, and `target_status`. Slice 5 adds no Kodi
capability, external target, production policy, lifecycle, snapshot, raw
JSON-RPC, settings, library/music, add-on execution, diagnostics, or Phase 12
workflow. Phase 11 is **implementation-complete pending formal review**. Only an
independent formal completion review may mark it COMPLETE.

## Phase 11 formal completion checkpoint

**Phase 11 — MCP core: COMPLETE.** Independent formal review accepted
implementation checkpoint `3faeeb808ca1a86ab5b1a8a0038827fe9b76b57a`,
Alembic head `0038_mcp_remote_control`, and 1,928 collected tests. The preceding
Slice 5 pending-review text remains the historical implementation checkpoint.

The completed core is primary stateless `2026-07-28` HTTP, canonical modern-only
stdio, and bounded stateless `2025-11-25` HTTP compatibility, all using the same
durable clients, verifier-only credentials, built-in or clone-only custom
presets, exact grants, target-policy intersection, deterministic five-tool
registry, bounds, errors, privacy rules and replay namespace. Native screenshots
and semantic remote control complete the observation/control boundary.

Codex CLI `0.154.0` is a documented unsupported client-version combination, not
a V1 gate; OpenCode `1.14.39` retains only its proven legacy-HTTP connection and
authorized discovery claim. Phase 12 editing/deployment/promotion, Phase 13–14
external and production targets, Phase 15 diagnostics, and later Kodi tool
expansion remain future milestones.

## Phase 12 Slice 1 checkpoint

Alembic `0039_mcp_development_inspection` seeds `workspace.inspect` for Add-on
Development and Full Lab Control, preserves old custom preset snapshots, and
adds exact client/source grants plus one-owner workspace bindings with
database-enforced source consistency and guarded downgrade.

Web administration owns source grant/revoke and eligible existing-workspace
binding. MCP owns none of those mutations. The common resolver requires enabled
client, current permission, current exact source grant, exact owner binding,
matching workspace source, and operation-compatible state.
`development_source_list`, `workspace_status`, `workspace_tree`, and
`workspace_read` reuse persisted catalog metadata and Phase 9 services, use
compact paginated schemas, 48 KiB development results, and the bounded
10/second burst-20 read class. Modern HTTP, modern stdio, and legacy HTTP share
the same nine-tool lexical catalog and behavior.

Phase 12 remains **PARTIAL**. Slice 1 has no workspace creation,
create/replace/patch editing or CAS, validation, quota/retention mutation,
deployment, execution, promotion request, source registration/deletion,
external/production targets, shell, Git, Docker, or raw filesystem authority.

## Phase 12 Slice 2 checkpoint

Alembic `0040_mcp_workspace_mutation` seeds `workspace.edit` only into Add-on
Development and Full Lab Control and adds the bounded reusable
`mcp_development_requests` identity table. Existing custom presets do not
inherit. Read-only Inspection and Production Remote Kodi are unchanged.

`workspace_create` accepts only request/source UUIDs. It reuses the Phase 9
creation service and atomically commits the workspace, creation operation,
global-heavy reservation, `mcp_created` binding, and client-scoped replay
mapping before submitting the existing worker. Exact replay cannot allocate a
second workspace. Present-workspace quotas are four/client and 32/global, with
one creating/client, transactionally enforced without eviction. Request keys
retain 30 days and cap at 10,000/client and 100,000/global.

One `workspace_edit` tool discriminates create, replace, and exact full-file
patch. It requires exact generation and the complete current client/permission,
source-grant, binding/source, present-ready-state intersection. Aggregate edit
text is 48 KiB. Phase 9 remains authoritative for paths, UTF-8, 2 MiB files,
existing parents, no-follow traversal, special-object rejection, full recapture,
atomic write, rollback/reconciliation, workspace exclusion, and generation CAS.
Edits use the bounded two/second burst-four class.

Modern HTTP, modern stdio, and legacy HTTP share the same replay namespace and
edit semantics. The catalog is exactly eleven lexical tools. Phase 12 remains
**PARTIAL**: structural validation, workspace retention/cleanup enforcement,
internal deployment, fixed execution, and promotion request/human-approval
integration remain future slices. No validation, deployment, execution,
promotion, cleanup automation, target expansion, shell, Git, Docker, or raw
filesystem authority is added here.

## Phase 12 Slice 3 checkpoint

Alembic `0041_mcp_workspace_validation_retention` adds restart-safe client
disable timing without a validation-history table. `workspace_validate` reuses
Phase 9 complete recapture, authoritative descriptor parsing and structural
change derivation at one exact generation. Its only inputs are workspace UUID
and `expected_generation`; it requires `workspace.inspect`, current source
grant and exact ownership, returns bounded semantic identity/change results,
and rejects deletions, binary changes, type transitions and unsafe objects.
Admission is one validation/client, one/workspace and two globally.

System retention reuses Phase 9 abandonment and cleanup. Safe abandoned and
promoted bytes retain seven days. Disabled-client ready workspaces quarantine
for 30 days, then abandon safely and retain seven further days; re-enable clears
quarantine eligibility. Enabled ready and reconciliation-required workspaces do
not age-delete. Every opportunistic sweep is capped at 32 inspections, two
transitions and two queued cleanups and shares workspace coordination with edit
and validation. MCP gains no lifecycle or cleanup tool.

At this Slice 3 checkpoint, modern HTTP, modern stdio and legacy HTTP shared the
exact twelve-tool catalog and validation schema. Exact internal deployment,
fixed add-on execution, promotion request and Web human-approval integration
remained later slices.

## Phase 12 Slice 4 checkpoint

Alembic `0042_mcp_workspace_deployment_execution` adds exactly
`workspace.deploy` and `development.execute` to Add-on Development and Full Lab
Control, latest exact-generation validation evidence, path-free deployment and
execution operations, and deploy/execute kinds in the existing development
request map. Existing custom presets, read-only and production presets remain
unchanged.

`workspace_deploy` accepts only request/workspace/generation/target UUIDs and
requires exact ownership, current source and target grants/policy, latest
successful validation of the same generation, stable add-on ID, matching
DevelopmentSource attachment, internal disposable target class and canonical
baseline or safe prior exact workspace evidence. A transport-neutral service
owns target/workspace/global coordination and the fixed stop, overwrite/add
copy, start, readiness, inventory-verification and evidence choreography.

`workspace_execute` accepts the same closed input and requires a successful
exact current deployment. It derives add-on ID/version and invokes only the
existing fixed copied Python video-plugin `Files.GetDirectory` smoke shape.
Other add-on types are unsupported; it accepts no add-on, URL, action,
parameters, timeout or raw JSON-RPC input.

Both operations commit their durable request before mutation/dispatch. Exact
duplicates replay across modern HTTP, modern stdio and bounded legacy HTTP;
conflicting parameters fail and uncertain post-marker requests never
redispatch. Deployment uses one active client/workspace/target plus
global-heavy coordination and a five-minute client/target interval. Execution
uses one active target and a one-minute client interval. Current deployment and
unresolved evidence blocks retention; workspace status and existing Web
administration project bounded latest state.

The catalog is exactly fourteen tools. Phase 12 remains **PARTIAL**: promotion
request, Web human-review/approval integration and formal completion review are
not implemented.

## Phase 12 Slice 5 implementation-complete checkpoint

Alembic `0043_mcp_promotion_request` adds exactly
`development.request_promotion` to Add-on Development and Full Lab Control,
extends the existing request-map operation set, and attaches bounded MCP origin
and exact deployment/execution evidence to the existing Phase 9 promotion plan.
Custom snapshots and read-only/production presets remain unchanged; there is no
parallel promotion table.

`promotion_request` accepts only request/workspace/generation/target UUIDs. It
requires exact ownership, current source and target grants, internal managed
disposable policy, present ready bytes, successful same-generation validation,
successful exact deployment and successful fixed execution. The server derives
source, add-on identity/version and complete change counts, then invokes only
the Phase 9 plan-preparation service.

The plan and request mapping commit together. Exact duplicates replay the same
plan across modern HTTP, modern stdio and bounded legacy HTTP; changed arguments
conflict and a post-claim preparing marker is ambiguous without retry. Existing
one-active-plan/workspace and expiry rules apply. New preparations are limited
to two/client/ten minutes with bounded keys, and request pruning retains an
active plan's replay identity.

All four development mutation kinds now share one bounded lifecycle-aware
pruner. Thirty-day-old mappings are deleted only for provably terminal,
replay-safe creation/deployment/execution or complete promotion/verification;
uncertainty and unresolved work retain identity and count toward capacity.
Terminal proof rejects every contradictory or incomplete progress marker, and
promotion-reference inspection uses scalar existence queries rather than
materializing historical plan collections.

The existing compact Web promotion page shows MCP origin, exact generation,
server-derived change counts, validation/deployment/execution state, conflict
classification and expiry alongside the existing exact diff. Approval and
confirmed rejection remain Web-only under authentication, Trusted Host,
same-origin, CSRF and literal confirmation. Client disable or grant revocation
blocks MCP without deleting the plan; Web independently rechecks canonical plan
safety and does not inherit MCP authority.

Approval uses the unchanged Phase 9 conflict recheck, backup, atomic source
apply, rollback, target sync/reload, inventory/readiness/log verification,
durable handoff and mandatory verification child. Source success and a later
verification failure remain separate. `workspace_status` projects only bounded
latest plan/operation/verification/final classification. The exact catalog is
fifteen lexical tools and contains no approval tool.

Phase 12 is **IMPLEMENTATION-COMPLETE PENDING FORMAL REVIEW**. Formal completion
requires a separate read-only completion review; Slice 5 does not authorize
Git, repository/GitHub publication, shell, arbitrary commands, generic
execution, external targets or production targets.

## Phase 12 formal completion checkpoint

**Phase 12 — MCP development workflows: COMPLETE.** Independent formal review
accepted implementation checkpoint `dc6f7857b222aff31467222e8d9eff8b96df6328`,
Alembic head `0043_mcp_promotion_request`, and 2,020 collected tests. The
preceding Slice 1–5 status text remains historical.

The completed workflow uses exact source grants and one durable workspace owner;
bounded inspect/read; atomic replay-safe creation; generation-CAS create,
replace and exact patch; closed validation; seven-day terminal retention plus
disabled-client quarantine; exact internal-disposable deploy; fixed copied
Python video-plugin execution; and promotion request into the Web-only Phase 9
review, apply and mandatory-verification path. The exact production catalog is
`addon_list`, `development_source_list`, `promotion_request`, `remote_action`,
`screenshot_capture`, `target_list`, `target_status`, `workspace_create`,
`workspace_deploy`, `workspace_edit`, `workspace_execute`, `workspace_read`,
`workspace_status`, `workspace_tree`, and `workspace_validate`.

D-0277 supplies centralized lifecycle-aware request retention; D-0278 requires
coherent terminal evidence and bounded scalar promotion references; D-0279
makes the exact promotion journal authoritative mutation evidence. Protected
identity fails closed under ambiguity and capacity pressure. Modern HTTP,
modern stdio and bounded legacy HTTP share the same services and replay state.
No shell, Git/GitHub, repository or ZIP publication, arbitrary command/test,
generic execution, raw filesystem, external target or production target is
added. Phase 13 is the separate external-development-target architecture gate.

## Phase 13 Slice 1 checkpoint

**Slice 1: COMPLETE. Phase 13: PARTIAL.** Migration
`0044_external_development_targets` adds the external-development subtype,
durable observations/events, closed capability storage and generic target
foreign keys while preserving every existing managed UUID, grant and preset
assignment. Destructive downgrade refuses populated external state.

Web administrators can register a structured private-LAN IP endpoint, store or
rotate a protected credential, run the fixed native read-only Kodi probe,
inspect its bounded evidence, explicitly confirm it, enable/disable it and
durably deactivate it. New targets are disabled/unverified and probe success is
not activation. Endpoint, credential or important observed identity changes
block effective use pending confirmation.

Hostname/DNS, self-signed TLS pinning, MCP use of external targets, bridge
pairing, screenshot, workspace deployment, fixed execution, external lifecycle
control, LAN-development proof and production policy remain future work. The
exact fifteen-tool MCP catalog and managed-only target eligibility are frozen
through Slice 1.
