# Implementation decisions

Decisions settled by `V1-SPEC.md` are not repeated here. Amendments require
explicit authorization; this log records only open-detail implementation
choices.

## D-0001: Python foundation tooling

- Status: accepted for Phase 0
- Decision: require the development host baseline Python 3.12; use a `src/` package layout,
  PEP 621 metadata, Hatchling builds, and uv dependency locking.
- Reason: conventional minimal packaging on the documented host with one
  reproducible command runner.

## D-0002: Foundation quality gate

- Status: accepted for Phase 0
- Decision: use pytest, Ruff formatting/linting, and strict mypy checks.
- Reason: small, complementary tools cover the specification's early unit,
  configuration, formatting, lint, and static-check needs.

## D-0003: SQLite migration framework

- Status: accepted for Phase 0
- Decision: use SQLAlchemy metadata and Alembic forward migrations. Keep the
  initial revision set empty until Phase 1 defines domain storage.
- Reason: this establishes the required migration framework without inventing
  schema before the core domain/storage phase.

## D-0004: Incremental module tree

- Status: accepted for Phase 0
- Decision: add the specification's recommended application, adapter, runtime,
  Kodi, storage, broker, development, diagnostics, and fixture packages only as
  their phases begin.
- Reason: avoids empty or speculative modules while preserving the specified
  boundaries in `ARCHITECTURE.md`.

## D-0005: Managed instance as a Target subtype

- Status: accepted for Phase 1
- Decision: store common identity/state in `targets`; use the same stable UUID
  as the primary/foreign key of `managed_instances`. Target names are not
  unique. Deleting a Target cascades to its owned managed-instance row.
- Reason: the specification defines ManagedTarget as a Target kind and gives
  managed instances a stable internal identity, but does not require distinct
  identities or unique display names. A shared key enforces one managed
  instance per Target without duplicating normalized Target data.

## D-0006: Persist only defined core state

- Status: accepted for Phase 1
- Decision: persist the specification's three health values, simple
  available/unavailable and connected/disconnected Target observations, and
  running/stopped managed-instance state. Store instance type, exact runtime
  revision, host-reboot preference, and optional notes. Defer capabilities,
  policies, presets, fixtures, history, and development associations.
- Reason: this is the smallest durable identity/state slice that supports later
  services without pre-implementing their phases.

The following decisions supplement `V1-SPEC.md` by resolving runtime-catalog
details that it did not specify.

## D-0007: Runtime-definition identity

- Status: approved for Phase 1
- Decision: use an opaque UUID primary key and a required unique lowercase slug.
  Kodi version is required metadata, not identity. Slugs accept lowercase ASCII
  letters, digits, dots, underscores, and hyphens.

## D-0008: Revision identity and ordering

- Status: approved for Phase 1
- Decision: use an opaque revision UUID plus a positive revision number unique
  within its parent definition. Revision number controls ordering; newest
  approved means the approved revision with the greatest number.

## D-0009: Explicit approval lifecycle

- Status: approved for Phase 1
- Decision: revisions are either `not_approved` or `approved`. Approval requires
  a UTC-normalized timestamp and may have a note; v1 stores no approving actor.
  Revocation clears approval timestamp and note.

## D-0010: Trusted Custom Build is independent

- Status: approved for Phase 1
- Decision: an intrinsic boolean records whether trust confirmation is required;
  a separate nullable UTC timestamp records confirmation. Approval never implies
  trust confirmation, and either lifecycle state can change independently.

## D-0011: Finalized immutable revisions

- Status: approved for Phase 1
- Decision: revisions are created only with complete intrinsic provenance and
  expose no generic update operation. Only approval and trust confirmation have
  explicit lifecycle mutation APIs.

## D-0012: Typed provenance and SHA-256

- Status: approved for Phase 1
- Decision: store nonblank source kind/reference, manifest reference, canonical
  `sha256:<64 lowercase hex>` content and manifest digests, UTC build timestamp,
  and an optional nonblank build-log reference. Logs and manifests themselves
  are not stored in SQLite. SHA-256 is the v1 digest algorithm.

## D-0013: Historical deletion restrictions

- Status: approved for Phase 1
- Decision: repositories expose no physical definition/revision deletion.
  Definition-to-revision and instance-to-revision foreign keys use restrictive
  deletion; no revision history cascades away.

## D-0014: Pre-catalog migration refusal

- Status: approved for Phase 1
- Decision: migration `0001` to the runtime-catalog schema succeeds only when
  `managed_instances` is empty. Populated pre-catalog data raises an actionable
  error instead of deleting rows or fabricating catalog provenance. Downgrade is
  likewise limited to empty managed-instance and runtime-revision tables.

## D-0015: Managed-instance runtime-selection policy

- Status: approved for Phase 1
- Decision: application operations identify Targets, runtime definitions, and
  revisions by stable UUID. Exact and newest-approved selection both require an
  approved revision. A revision marked as requiring Trusted Custom Build
  confirmation additionally requires global opt-in supplied to the service and
  an existing per-revision confirmation; selection never grants confirmation.
  Instance type does not alter selection policy.
- Reason: this directly composes specification sections 16, 20, and 21 with the
  persisted approval/trust decisions while keeping policy out of adapters.

## D-0016: Compatibility and transaction boundary

- Status: approved for Phase 1
- Decision: Target and runtime-definition Kodi versions must compare equal after
  removing surrounding whitespace. No version alias, range, or live probing is
  inferred. The application service owns one SQLAlchemy transaction per create
  or reassignment operation and exposes typed application errors.
- Reason: the specification provides exact version metadata but no compatibility
  matrix. Exact normalized comparison is deterministic and can be revised when a
  canonical compatibility policy exists.

## D-0017: Catalog lifecycle command semantics

- Status: approved for Phase 1
- Decision: definition and finalized-revision registration are create-only and
  conflicting identity/slug/revision-number requests fail. New revisions begin
  unapproved and without trust confirmation. Repeating an applicable approval,
  revocation, confirmation, or confirmation-revocation command after it reaches
  the requested state is idempotent and preserves the original lifecycle
  timestamp/note. Trust lifecycle commands are rejected for revisions that do
  not require Trusted Custom Build confirmation.
- Reason: deterministic idempotence supports retries without rewriting lifecycle
  evidence, while rejecting inapplicable trust operations preserves the strict
  domain invariant.

## D-0018: Broker privilege model

- Status: approved for Phase 2
- Decision: the production broker runs as root. Application processes run as
  dedicated user/group `kodi-lab`, receive neither Docker-group membership nor
  direct Docker-socket access, and use the broker as the sole intended privileged
  Docker mediation boundary. Account creation is installation work, not Phase 2.

## D-0019: Kernel peer identity and exact-UID authorization

- Status: approved for Phase 2
- Decision: resolve the production `kodi-lab` username at startup and authorize
  only the UID reported by Linux `SO_PEERCRED`. PID and primary GID are audit
  fields only. Request fields, environment, executable identity, and
  supplementary groups never grant authorization. Tests may inject an expected
  UID.

## D-0020: Root has no implicit broker authorization

- Status: approved for Phase 2
- Decision: UID 0 is not automatically added to the application RPC allowlist.
  Authorization remains an exact comparison with the configured client UID.

## D-0021: No secondary local authentication secret

- Status: approved for Phase 2
- Decision: protected Unix-socket access, kernel peer credentials, and exact-UID
  authorization are the complete local authentication chain. Do not add a token,
  password, API key, TLS layer, or parallel secret store.

## D-0022: Canonical broker socket path

- Status: approved for Phase 2
- Decision: the local-only production stream socket is
  `/run/kodi-lab/broker.sock`, directly inside `/run/kodi-lab`. TCP and other
  network listeners are forbidden.

## D-0023: Runtime-directory and socket metadata

- Status: approved for Phase 2
- Decision: `/run/kodi-lab` is `root:kodi-lab` mode `0750`; `broker.sock` is
  `root:kodi-lab` mode `0660`. The broker must fail if expected ownership or mode
  cannot be established. Tests use current-user-owned temporary directories.

## D-0024: Long-running service activation

- Status: approved for Phase 2
- Decision: v1 uses a conventional long-running `kodi-lab-broker.service`; the
  broker creates its own socket. There is no systemd socket activation. A unit is
  deferred until packaging/install paths are settled and is not installed during
  Phase 2. The intended unit name currently collides with the protected legacy
  service on development host, so coexistence forbids installation until that deployment
  conflict is separately resolved.

## D-0025: Fail-closed socket-path lifecycle

- Status: approved for Phase 2
- Decision: validate the runtime directory and inspect the endpoint without
  following symlinks. Refuse symlinks, non-sockets, unsafe metadata, and live
  listeners. Remove only a verified stale socket inside the validated directory;
  cleanup unlinks only the socket inode created by this broker. Never recursively
  repair or delete path content.

## D-0026: Broker protocol serialization and framing

- Status: approved for Phase 2
- Decision: protocol v1 is strict UTF-8 JSON over an `AF_UNIX` stream, framed by
  a four-byte unsigned big-endian payload length. Each connection carries exactly
  one request and one response and is then closed. The payload maximum is 65,536
  bytes; oversized announced lengths fail before payload allocation/read.

## D-0027: Exact request envelope

- Status: approved for Phase 2
- Decision: requests contain exactly `version`, canonical UUID `request_id`,
  bounded registered `operation`, and operation-specific object `params`.
  Unknown fields, caller identity fields, malformed JSON, duplicate keys,
  non-finite constants, and invalid schemas are rejected.

## D-0028: Structured response and error envelope

- Status: approved for Phase 2
- Decision: success returns `version`, `request_id`, `ok=true`, and typed
  `result`. Failure returns the version, recoverable request ID or null,
  `ok=false`, and a stable code/safe message. Tracebacks, raw exceptions,
  environment data, filesystem internals, and future raw Docker errors must not
  cross the boundary.

## D-0029: Exact protocol-version policy

- Status: approved for Phase 2
- Decision: accept protocol version 1 only. There is no negotiation or automatic
  downgrade; unsupported versions receive a stable error. Future versions must be
  added explicitly.

## D-0030: Connection and schema resource limits

- Status: approved for Phase 2
- Decision: use one request per connection, a 65,536-byte payload limit, a
  64-character operation-name limit, canonical UUID request IDs, and five-second
  read/write timeouts. Each operation bounds its own strings and collections.
  Do not add a generic JSON-depth analyzer or arbitrary recursive request model.

## D-0031: Correlation IDs are not replay protection

- Status: approved for Phase 2
- Decision: request IDs support correlation, logs, diagnostics, and future
  tracing, but are not credentials and do not provide exactly-once delivery.
  Phase 2 has no replay cache. Future mutation operations must be naturally
  idempotent or conflict-detecting with explicit state preconditions.

## D-0032: Initial closed operation set

- Status: approved for Phase 2
- Decision: expose exactly `ping`, `broker_info`, and `capabilities`, each with an
  empty parameter object and a fixed result schema. Metadata is deliberately
  minimal. No execute, shell, command, Docker, raw, file, path, or lifecycle
  operation exists in this slice.

## D-0033: Uniform initial operation authorization

- Status: approved for Phase 2
- Decision: all initial operations require the authorized Kodi Lab peer UID;
  there is no anonymous health operation. Registry entries carry authorization
  metadata for later explicit policy evolution without introducing general RBAC.

## D-0034: Safe broker audit records

- Status: approved for Phase 2
- Decision: journal-suitable records contain request ID, peer PID/UID/primary
  GID, safely parsed operation and version, result/error category, and elapsed
  duration. Authorization failures are recorded. Full bodies, unbounded malformed
  data, secrets, and environment values are excluded.

## D-0035: Fail-closed broker errors

- Status: approved for Phase 2
- Decision: startup fails on unresolved identity or insecure path/metadata;
  connections fail authorization when credentials are unavailable; malformed,
  oversized, unsupported, or unknown requests never reach handlers; and handler
  failures produce only a safe internal error while retaining diagnostic logs.

## D-0036: Managed-container ownership labels

- Status: approved for Phase 2
- Decision: a container belongs to the new Kodi Lab managed scope only when its
  inspect data contains both exact labels `io.kodi-lab.managed=true` and
  `io.kodi-lab.instance-id=<lowercase canonical UUID>`. The literal names are a
  Phase 2 choice because the canonical specification requires deterministic
  ownership metadata but does not spell the keys. Names, images, networks,
  Compose metadata, and legacy labels never establish ownership. Unlabelled and
  wrong-value containers are excluded. A container with the exact ownership
  label but missing or malformed instance identity, or multiple containers with
  the same instance identity, fails the complete inventory as invalid managed
  metadata rather than being treated as valid or silently omitted.
- Reason: exact namespaced labels give the trusted adapter a deterministic scope
  boundary, while whole-result failure prevents ambiguous or corrupted ownership
  state from appearing authoritative. Labels select managed Docker objects; they
  do not authenticate broker callers, which remains an `SO_PEERCRED` exact-UID
  decision.

## D-0037: Bounded read-only managed-container inventory

- Status: approved for Phase 2
- Decision: add only `managed_container_inventory`, with empty parameters, behind
  a read-only adapter using the maintained Docker SDK for Python and a fixed local
  daemon endpoint/filter. It returns at most 128 entries, sorted by managed
  instance UUID, containing only canonical instance UUID, immutable 64-hex
  container ID, immutable `sha256:<64 lowercase hex>` image ID, bounded runtime
  state, and bounded health state. More than 128 matching summaries fails without
  inspection or truncation. No observations are persisted.
- Runtime mapping: Docker `running` is `running`; `created`/`exited`/`dead` are
  `stopped`; `restarting`/`removing`/`paused` are `transitional`; other string
  states are `unknown`. Health `healthy`, `unhealthy`, and `starting` retain those
  meanings; absent health is `not_configured`; other string states are `unknown`.
- Reason: 128 is conservative for a local lab and keeps the worst-case typed
  response well below the existing 65,536-byte protocol frame. Fixed semantic
  output avoids exposing raw inspect data, mutable tags, legacy containers, or a
  caller-directed Docker API. Daemon, read/validation, ownership-integrity, and
  count-limit failures have distinct safe broker error codes.

## D-0038: Optimistic semantic managed-container stop

- Status: approved for Phase 2
- Decision: add only `stop_managed_container` as the first mutation. Its strict
  request contains canonical managed-instance UUID, previously observed immutable
  64-hex container ID, and previously observed normalized runtime state. The
  trusted adapter selects with both exact Kodi Lab ownership labels, requires one
  result, freshly inspects and revalidates ownership/instance/container identity,
  and never discovers by caller-supplied container ID. A running container is
  stopped only when the expected state is `running`, using the broker-controlled
  Docker stop timeout of 10 seconds. Transitional and unknown states are never
  mutated. A second fresh inspect must prove the same owned immutable container is
  stopped before success is returned.
- Idempotency: after all ownership and immutable-identity checks pass, an already
  stopped container returns successful convergence with `stop_performed=false`
  regardless of the caller's expected runtime state. This desired-state exception
  is the only bypass of the normal state-equality precondition. No Docker stop is
  issued in this case.
- Failure/race policy: no match, ambiguity, invalid ownership metadata, immutable
  identity conflict, runtime conflict, non-stoppable state, stop failure, and
  postcondition failure have stable safe errors. The broker does not call Docker
  kill, retry, or accept caller timeout/signal/force controls. Immutable identity
  prevents replacement container B from being stopped for stale container A.
  There remains an unavoidable interval between fresh inspect and Docker stop in
  which another privileged Docker actor can change A's state; the exact-ID stop
  and post-stop verification bound but do not eliminate that Docker-level TOCTOU.
- Resource behavior: Docker calls retain fixed five-second API timeouts, with the
  SDK extending the stop request for its fixed 10-second stop timeout. The typed
  client allows a fixed 35-second response window for this synchronous operation;
  other protocol operations retain the five-second response timeout.

## D-0039: Existing-container two-state reconciliation

- Status: approved for Phase 2
- Decision: add explicit `start_managed_container` and retain explicit
  `stop_managed_container`; do not expose a generic reconciliation or Docker
  lifecycle operation. Both use one private adapter flow for fixed-label
  discovery, exactly-one selection, fresh ownership/instance/immutable-ID
  validation, normalized-state evaluation, mutation, and fresh postcondition
  validation. The only desired states are `running` and `stopped`.
- State rule: when the exact owned container is already in the operation's desired
  state, return successful convergence without mutation regardless of the
  caller's expected state. When mutation is required, the caller's expected state
  must equal the freshly observed state; only `stopped` to `running` and `running`
  to `stopped` may mutate. Transitional and unknown states fail closed.
- Start rule: a start request contains only canonical managed-instance UUID,
  previously observed immutable 64-hex container ID, and normalized expected
  runtime state. The adapter starts only the trusted label-selected immutable ID,
  passes no caller lifecycle options, and freshly verifies the same ownership,
  instance identity, immutable ID, and `running` state after Docker returns.
  There is no retry, polling, restart-policy manipulation, or Kodi readiness
  claim. The typed start client uses a fixed 25-second response window derived
  from bounded Docker API calls.
- Race policy: exact-ID checks and repeated post-mutation validation reject stale
  replacement and false-success cases but cannot eliminate the interval between
  pre-mutation inspect and Docker start/stop when another privileged Docker actor
  can change the same object. This phase adds no lock or lease.

## D-0040: Immutable runtime-container artifact mapping

- Status: approved for Phase 2
- Decision: materialized Docker identity is a separate immutable one-to-one
  `RuntimeContainerArtifact` keyed by runtime-revision UUID and containing only a
  canonical `sha256:<64 lowercase hex>` image ID. `RuntimeRevision.content_digest`
  is never reinterpreted as Docker identity. Registration requires an existing
  revision and conflicts on every second mapping; there is no update/delete,
  pull, build, or materialization API. Migration `0003_runtime_container_artifacts`
  adds the restrictive historical foreign key.

## D-0041: Trusted provisioning resolution and protocol input

- Status: approved for Phase 2
- Decision: `provision_managed_container` accepts only canonical managed-instance
  UUID. A broker-side read-only SQLite resolver loads the Target, existing
  ManagedInstance, exact pinned RuntimeRevision, RuntimeDefinition, and immutable
  RuntimeContainerArtifact. It neither reselects a revision nor re-evaluates
  historical approval/trust. The resolved specification fixes name, image,
  revision, and network; ORM rows and caller substitutions do not cross the
  boundary.

## D-0042: Disposable create-only container policy

- Status: approved for Phase 2
- Decision: the first provisioning slice supports disposable instances only and
  creates without starting. The deterministic name is
  `kodi-lab-managed-<32 lowercase UUID hex>`. Labels are exactly the established
  managed/instance labels plus
  `io.kodi-lab.runtime-revision-id=<canonical UUID>`. Creation uses the immutable
  image ID and image-owned environment/command/entrypoint, publishes no ports,
  creates no mounts/volumes, sets restart policy `no`, sets `privileged=false`,
  and adds no devices, capabilities, host namespaces, custom security profiles,
  or resource limits. Dynamic Docker addressing is used. Start remains a separate
  secured operation. Docker's raw create-only `created` state normalizes to the
  broker's semantic `stopped` state.

## D-0043: Managed Docker network prerequisite

- Status: approved for Phase 2
- Decision: provisioning requires but never creates the bridge network
  `kodi-lab-managed-v1`, with exact required labels
  `io.kodi-lab.managed=true`, `io.kodi-lab.resource=network`, and
  `io.kodi-lab.network-id=managed-v1`, and subnet `10.250.0.0/24`. A dedicated
  2026-09-14 read-only development host revalidation inspected all 19 Docker networks and
  found no overlap; the five protected networks retained their documented
  subnets. Missing or mismatched network state fails closed and legacy networks
  are never substituted.

## D-0044: Provisioning idempotency and rollback

- Status: approved for Phase 2
- Decision: exactly one already-owned container is idempotent only when immutable
  identity, deterministic name, all three labels, image defaults, image ID,
  managed network, no ports/mounts/volumes, restart/security profile, and a
  running-or-stopped normalized state all match. Drift, duplicate ownership, and
  any exact-name collision fail without adoption or repair. New creation must
  freshly verify the same invariants and stopped state.
- Rollback: post-create failure removes only the exact returned immutable ID after
  a second fresh inspect proves all three ownership labels. Removal is non-forced
  because provisioning never starts the container. Failure to prove identity or
  ownership, an unusable create response, or removal failure returns
  `partial_provisioning_requires_reconciliation`; pre-existing containers and
  images/networks/volumes are never removed.

## D-0045: Proof-gated stopped-container deprovision

- Status: approved for Phase 2
- Decision: expose explicit `deprovision_managed_container` with exactly canonical
  managed-instance UUID, previously observed immutable 64-hex container ID,
  canonical expected runtime-revision UUID, and literal expected state `stopped`.
  Fixed managed/instance labels discover exactly one candidate; the caller ID is
  only a precondition. A read-only trusted resolver independently verifies that
  the persisted ManagedInstance still pins the expected revision. Fresh inspect
  must then prove exact immutable ID, deterministic managed name, all identity
  labels including the runtime revision, and normalized stopped state.
- Removal and postcondition: Docker receives only the trusted selected immutable
  ID with `force=false` and volume removal disabled. The broker neither stops nor
  kills a container. Success requires the exact ID to return not-found after
  removal and a second fixed-label lookup to find no remaining container for the
  instance. Missing and ambiguous ownership both fail; absence is not destructive
  idempotent success, and ambiguity is never resolved using the caller ID.
- Drift and persistence: identity/security invariants and stopped state are
  mandatory, while unrelated non-identity Docker drift does not make a provably
  owned stopped container undeletable. Deprovision removes only the container;
  canonical Target, ManagedInstance, runtime revision/artifact, image, network,
  and volumes remain unchanged.
- Race policy: fixed-label lookup, fresh inspect, exact ID/revision validation,
  immediate exact-ID removal, and two absence checks reject observed-A/replaced-B
  deletion and false success. They cannot eliminate changes by another privileged
  Docker or database actor between checks; Phase 2 adds no lock or lease.
