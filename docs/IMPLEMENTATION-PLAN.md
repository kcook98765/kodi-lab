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
