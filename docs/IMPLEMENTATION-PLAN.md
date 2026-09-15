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
