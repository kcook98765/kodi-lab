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

The next Phase 7 milestone is the proof-gated reconciliation finalization
architecture gate. It must define evidence sufficient to finalize each
unresolved typed operation without adding generic repair or retry authority;
this plan does not authorize its implementation.
