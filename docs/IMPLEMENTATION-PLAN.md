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

The runtime-catalog administration slice completes Phase 1 application
orchestration for create-only definition/finalized-revision registration and
explicit approval/trust lifecycle changes. Registration, lifecycle mutation,
managed-instance selection, and error translation now share consistent atomic
service boundaries. Phase 1 is ready for closeout validation; Phase 2 remains
ready to begin.

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

Controlled real-Docker validation has exercised provision, inventory, start,
stop, and idempotent stop through the actual peer-authorized broker while leaving
one stopped disposable fixture. The next Phase 2 slice adds only
`deprovision_managed_container`: trusted persisted-revision resolution,
label-based exactly-one discovery, immutable ID/revision and stopped-state
preconditions, normal non-volume container removal, and dual absence
postconditions. It removes no database/catalog state, image, network, volume, or
legacy resource, and exposes no generic Docker removal primitive.
