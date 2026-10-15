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

- Status: approved for Phase 2; Linux V1 host-unit choice superseded by D-0369
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

## D-0046: First Kodi runtime artifact

- Status: approved for Phase 3
- Decision: the first runtime is Kodi 21.3 Omega stable, built as a dedicated
  repository-owned image from digest-pinned Ubuntu 26.04 and the exact official
  Ubuntu package `2:21.3+dfsg-1ubuntu1`. The declarative build manifest records
  the base digest, package sources/version, repository input hashes, build
  parameters and timestamp, immutable result image ID, and build-log digest.
  Image build/pull remains a controlled development workflow and is not a broker
  operation. Upstream repository metadata prevents a claim of bit-for-bit rebuild
  reproducibility even though declared inputs and the result identity are fixed.
- Runtime layout: Kodi runs as user `kodi` on internal Xvfb display `:99` with
  software rendering and a small fixed entrypoint using `dbus-run-session`.
  A minimal seeded profile enables unauthenticated JSON-RPC HTTP only on internal
  port 8080. The image declares no volume or exposed port and has no VNC, noVNC,
  host display, GPU/device, capability, or legacy-profile dependency.

## D-0047: Immutable trusted runtime execution profile

- Status: approved for Phase 3
- Decision: migration `0004_runtime_execution_profiles` adds one immutable,
  restrictive one-to-one `RuntimeExecutionProfile` per finalized revision.
  For this milestone it fixes runtime kind Kodi, application `Kodi`, version
  `21.3` tag `stable`, JSON-RPC port 8080/path `/jsonrpc`, Docker-health process
  observation, fixed Kodi JSON-RPC probes, total timeout 90 seconds, poll cadence
  one second, and per-request timeout three seconds. Registration is create-only;
  every second registration conflicts, and no update/delete API exists.
- Boundary: no command, environment, mount, host port, network, Docker option,
  JSON-RPC method, or arbitrary endpoint is represented by this profile or
  supplied by provisioning callers. Material behavior changes require a new
  revision and profile.

## D-0048: Process and endpoint observation

- Status: approved for Phase 3
- Decision: the image healthcheck is a fixed repository-owned `pgrep` check for
  `kodi.bin`; it observes only the expected Kodi process and does not probe the
  network. A new semantic broker operation accepts only canonical managed-instance
  UUID and previously observed immutable container ID. Trusted persisted state
  supplies the revision and profile. Fixed-label discovery and fresh inspect must
  prove ownership, revision, running state, required health data, exact sole
  attachment to `kodi-lab-managed-v1`, and one canonical IPv4 address in
  `10.250.0.0/24`.
- Result: the broker returns only instance/revision/container identities,
  normalized container and Kodi-process states, managed IPv4 address, and the
  profile-owned port 8080. The request has no network or port input and raw Docker
  inspect/network dictionaries do not cross the boundary. The broker performs no
  exec and no application-layer HTTP.

## D-0049: Unprivileged fixed Kodi readiness

- Status: approved for Phase 3
- Decision: unprivileged code uses the trusted broker observation to perform only
  `JSONRPC.Ping` and `Application.GetProperties` for `name` and `version`.
  Requests use fixed `/jsonrpc`, validated JSON-RPC envelopes and IDs, a 64 KiB
  response bound, no redirect handling, and a three-second connection/read
  timeout. There is no public generic JSON-RPC call or endpoint-selection API.
- Readiness: container state, Kodi process health, JSON-RPC state, and usability
  remain separate. Usable requires running container, healthy process, valid
  pong, application name `Kodi`, and exact 21.3 stable identity. Polling uses one
  total 90-second monotonic deadline at one-second cadence. Terminal protocol or
  identity failures return immediately; retryable absence/unavailability retains
  its most specific last failure beneath `readiness_timeout`.

## D-0050: Phase 3 validation isolation and shutdown

- Status: approved for Phase 3
- Decision: live validation uses distinct state under
  `<validation-state>`, an ephemeral broker socket under
  `/run/kodi-lab-phase3-validation`, and the existing managed network. It does not
  reuse the retained Phase 2 nginx database or install production services.
  Exactly one disposable managed runtime may be provisioned and started.
- Shutdown continues to use the Phase 2 `stop_managed_container` contract with
  its fixed 10-second Docker stop timeout. The entrypoint propagates termination
  to Kodi and adds no kill/restart fallback. Protected legacy containers,
  networks, profile data, broker, and service remain outside this workflow.

## D-0051: Localhost-only FastAPI Web profile

- Status: approved for Phase 4
- Decision: the initial product Web process uses FastAPI, Jinja2 server-rendered
  HTML, local static assets, and minimal vanilla JavaScript. Product configuration
  accepts only `127.0.0.1` and port 8100; port 8180 is the sole alternate for
  ephemeral validation. Wildcard, IPv6-wildcard, and LAN binds fail validation.
  Nginx, LAN exposure, HTTPS, CORS, and frontend build tooling remain deferred.

## D-0052: Single administrator credential and local bootstrap

- Status: approved for Phase 4
- Decision: v1 has exactly one fixed `local-admin` credential record. The narrow
  `kodi-lab-admin set-password` local command prompts twice through hidden input
  and writes only an Argon2id password hash and update timestamp. There is no
  browser bootstrap, signup, reset, user list, role, OAuth, or SSO surface.
  Password replacement atomically invalidates every existing Web session.

## D-0053: Opaque server-side Web sessions

- Status: approved for Phase 4
- Decision: successful login creates independent 256-bit random session and CSRF
  values. The browser receives only the session bearer token; SQLite stores its
  SHA-256 digest, the session-bound CSRF token, creation/last-use timestamps, and
  absolute expiry. Idle expiry is 12 hours, absolute expiry seven days, and
  last-use writes are limited to five-minute intervals. Logout and expiration
  delete the server record. The `kodi_lab_session` cookie is HTTPOnly,
  SameSite=Strict, Path `/`, has no Domain, and uses `Secure=false` only for this
  loopback HTTP milestone.

## D-0054: Browser mutation and login abuse controls

- Status: approved for Phase 4
- Decision: logout and every lifecycle POST require a session-bound synchronizer
  token plus an exact same-origin Origin or Referer check. Login also requires
  same-origin context and uses a small per-process, per-source failed-attempt
  backoff before further Argon2 work. Trusted Host validation prevents an
  arbitrary Host header from defining the accepted origin. CORS is absent.
  Broader exposure requires a new cookie, proxy, origin, Host, and throttling
  review.

## D-0055: Semantic Web status and lifecycle authority

- Status: approved for Phase 4
- Decision: one application status service combines canonical Target,
  ManagedInstance, RuntimeRevision, broker inventory/process observation, and
  ephemeral readiness state into a bounded DTO. The browser receives no
  container ID, image, network, address, labels, or raw inspect data. Lifecycle
  routes accept only canonical instance UUID plus CSRF and reload inventory,
  revision, and immutable container preconditions server-side immediately before
  invoking explicit typed BrokerClient methods.

## D-0056: Ephemeral readiness registry

- Status: approved for Phase 4
- Decision: successful start returns after broker start and schedules at most one
  effective in-process readiness task per instance. Generation checks discard
  stale completion, stop/deprovision signal cancellation, and the existing
  coordinator retains its 90-second bound. Task states are idle, checking,
  usable, failed, timed out, or cancelled. No result is persisted, and process
  restart reconstructs storage/broker status with readiness not checked.

## D-0057: Initial authenticated Web surface

- Status: approved for Phase 4
- Decision: server-rendered routes are `GET/POST /login`, `POST /logout`, and
  `GET /`. Same-origin JSON routes list/get existing managed instances and expose
  only provision/start/stop/deprovision. Polling is two seconds; WebSockets, SSE,
  API tokens, generic broker calls, generic JSON-RPC, target creation, catalog
  administration, and matrix controls are absent. Safe typed failures map to
  authentication/CSRF/not-found/conflict/dependency HTTP classes without raw
  exceptions.

## D-0058: Exact Kodi release identity and definition support tier

- Status: approved for Phase 5
- Decision: one RuntimeDefinition represents one exact typed Kodi release. The
  canonical stored form is `major.minor` for stable releases and
  `major.minor-channel.ordinal` for alpha, beta, and release-candidate builds.
  RuntimeDefinition independently stores one product support tier: `current`,
  `legacy`, `preview`, or `retired`. Stable definitions may be current, legacy,
  or retired; prerelease definitions may be preview or retired. Rebuilds remain
  RuntimeRevisions beneath the same definition.
- Initial policy: Kodi 21.3 and 20.5 stable are Current, Kodi 19.5 stable is
  Legacy, and Kodi 22.0 Beta 2 is Preview. This classification is explicit
  catalog state, not external release discovery or a forever-version list.

## D-0059: Monotonic support lifecycle and historical preservation

- Status: approved for Phase 5
- Decision: support changes use an explicit administration operation. The
  initial state machine permits current to legacy/retired, legacy to retired,
  and preview to retired. Repeating the current state is idempotent; retired is
  terminal until a future unretire policy is approved. Stable cannot become
  preview and prerelease cannot become current/legacy. A stable successor is a
  distinct RuntimeDefinition, never a mutated prerelease identity.
- Historical behavior: tier changes neither delete nor rewrite definitions,
  revisions, artifacts, profiles, validation evidence, or ManagedInstance pins.
  Retired definitions cannot supply new pins, but existing exact pins remain
  operable in the absence of the separately deferred quarantine policy.

## D-0060: Append-only runtime validation evidence

- Status: approved for Phase 5
- Decision: `RuntimeValidationEvidence` is immutable administrative metadata
  with UUID, revision UUID, bounded contract identifier, passed/failed result,
  start/completion/recorded timestamps, bounded evidence reference, and optional
  safe summary. Records have a restrictive revision foreign key and no update or
  delete API. The recognized required contract is `kodi-runtime-v1`.
- Effective state: absence under the required contract is unvalidated; the
  newest applicable record by recorded time and deterministic UUID tie-break is
  validated when passed and failed when failed. Evidence for older contracts
  remains historical but cannot satisfy the current contract. Validating is
  transient job state, not catalog truth. Registration records evidence only and
  never runs validation.

## D-0061: Shared new-pin eligibility and newest-eligible selection

- Status: approved for Phase 5
- Decision: one application policy evaluates exact Target/definition release
  compatibility, tier permission, revision approval, Trusted Custom Build
  policy, immutable artifact, immutable execution profile, profile/definition
  identity agreement, and latest passing `kodi-runtime-v1` evidence. It returns
  eligible or one bounded typed reason.
- Selection: normal policy permits Current only; trusted Include Legacy and
  Include Pre-release options add Legacy and Preview respectively. Retired is
  never selectable. Exact selection must pass every gate. Newest-eligible scans
  revision numbers descending and may choose an older eligible revision when a
  newer revision fails a gate, but never crosses RuntimeDefinition identity.
  Existing pins are not retroactively re-evaluated; explicit reassignment is.

## D-0062: Generalized immutable execution-profile identity

- Status: approved for Phase 5
- Decision: RuntimeExecutionProfile retains explicit major/minor, release
  channel, and nullable prerelease ordinal fields and exposes the same typed
  release identity as Target and RuntimeDefinition. Stable requires no ordinal;
  alpha/beta/RC requires a positive ordinal. Registration requires exact
  agreement with the parent definition.
- Execution bounds: the proven 8080 `/jsonrpc`, Docker healthcheck, Kodi JSON-RPC,
  90-second deadline, one-second cadence, and three-second HTTP timeout remain
  the 21.3 values, while bounded per-revision values are no longer hard-coded to
  21.3 forever. Kodi identity probing preserves and compares observable
  `tagversion`, so Beta 1 and Beta 2 cannot collapse.

## D-0063: Phase 5 build, Web, and next-runtime boundaries

- Status: approved for Phase 5
- Decision: runtime build directories remain version-owned; the proven Kodi
  21.3 image is not refactored before cross-version evidence exists. Future Web
  catalog DTOs show exact release, channel/ordinal, support tier, effective
  validation, revision, eligibility, and bounded ineligibility reason; browsers
  receive no raw image or catalog mutation authority. This milestone adds no
  matrix UI or validation endpoint.
- Next target: Kodi 20.5 stable is the next separately authorized runtime because
  it is Current, exercises backward compatibility, and has less packaging/build
  divergence than Kodi 19 or Kodi 22 prerelease.

## D-0064: Isolated validation-pin bootstrap

- Status: approved for Phase 5 implementation
- Decision: a trusted validation workflow may bootstrap exactly one stopped,
  disposable ManagedInstance pin directly in its dedicated non-production
  validation database after exact definition/revision, artifact, execution
  profile, approval/trust, and compatibility state are established. This narrow
  bootstrap resolves the necessary validation-before-selection dependency; it
  is not an alternative product creation path.
- Boundary: browsers and broker requests receive no bootstrap authority or raw
  revision/image fields. Normal ManagedInstance creation and reassignment still
  use shared current eligibility, while the validation fixture uses the exact
  pre-registered immutable revision solely for provision, two readiness cycles,
  clean stops, and deprovision before evidence registration.

## D-0065: Source-bound Kodi 19.5 package provenance

- Status: approved for Phase 5 implementation
- Decision: the Kodi 19.5 version-owned build may use only the exact Jammy
  packages `6:19.5+git20221224.1006-f8fdeb6b1b-0~jammy` from Team Kodi's
  `kodi-old-stable` Launchpad PPA. The manifest records the source URL, Launchpad
  signing fingerprint, and exact downloaded package hashes; the build verifies
  those hashes and installed versions. Runtime-manifest validation accepts this
  alternate epoch/release shape only when bound to that named source.
- Boundary: this narrow legacy-source rule does not authorize arbitrary PPAs,
  dynamic package selection, or use of the Kodi 19 source for any other runtime.
  Ubuntu base and dependency packages remain distribution-owned and the final
  image identity remains immutable.

## D-0066: Pinned official-source Kodi 22 prerelease build

- Status: approved for Phase 5 implementation
- Decision: Kodi 22.0 Beta 2 is built only from Team Kodi's official
  `22.0b2-Piers` release tag, which points to the verified-signed commit
  `e513e0ff4331fc25fd2454659a9dd3e6b7670146`; the source archive and pinned
  Ubuntu base are digest-verified. The version-owned manifest records the
  source, toolchain inputs, CMake options, copied assets, build log, and
  resulting immutable image without claiming bit-for-bit reproducibility.
- Trust: although the source is official, compiling it into a repository-owned
  image is classified through the existing Trusted Custom Build policy. Global
  Custom Build opt-in and per-revision confirmation therefore remain mandatory
  in addition to approval, validation, and Preview selection policy.
- Runtime boundary: the multi-stage build keeps compilers and build tooling out
  of the final image. Kodi's internal TagLib 2.3.1 build is enabled so the
  Beta 2 source-owned Matroska fast-scan patch is applied. This source-build
  path is local to Kodi 22.0 Beta 2 and adds no broker build or execution
  capability.

## D-0067: Authenticated semantic matrix and Target-scoped resolution

- Status: approved for Phase 5 implementation
- Decision: `RuntimeMatrixService` is the sole application composition boundary
  for curated runtime presentation and read-only Target-scoped selection
  resolution. It delegates every revision decision to the shared eligibility
  policy, reports the newest eligible revision number and bounded semantic
  reasons, and exposes no revision UUID, artifact, image, evidence reference,
  provenance internals, or Docker configuration.
- Web policy: authenticated GET routes accept only strict Include Legacy and
  Include Pre-release booleans. These are request-local selection preferences,
  never catalog mutations. Trusted Custom Build enablement remains deployment
  configuration and cannot be supplied by the browser.
- Creation boundary: this slice does not create or reassign ManagedInstances.
  Canonical documents establish one ManagedInstance subtype per Target but do
  not yet define the browser Target inventory/creation metadata, naming,
  instance-type choice, or Target-version transition workflow. The Web surface
  therefore previews trusted exact-release resolution without inventing those
  mutation semantics. Existing pins remain status/history authority and are not
  re-evaluated as new selections.

## D-0068: Disposable reset preserves exact catalog identity

- Status: approved for Phase 6 Step 1
- Decision: `DisposableInstanceResetService` is one bounded destructive
  application operation accepting only ManagedInstance UUID. It supports only
  disposable instances, preserves Target and ManagedInstance identity, the exact
  RuntimeRevision pin and all non-container catalog state, and never calls a
  newest-eligible selector. Persistent reset returns
  `persistent_reset_unsupported`; transitional, unknown, or ambiguous ownership
  fails closed.
- Completion and failure: success always means one freshly provisioned stopped
  container. A running old container is normally stopped before stopped-only
  deprovision; prior running state is not restored. Reset is not atomic: after
  successful old-container removal, provision failure leaves the durable pin
  intact and the instance operationally unprovisioned. Fixture failures retain
  the fresh container stopped when that postcondition can be established, or
  require explicit reconciliation.

## D-0069: Immutable small-mixed-v1 fixture catalog and assignment

- Status: approved for Phase 6 Step 1
- Decision: the only predefined fixture is repository-owned `small-mixed-v1`,
  version 1, Small/Mixed, compatible only with exact Kodi 21.3 stable. It contains
  one fictional movie and one fictional one-episode series with deterministic
  NFO, generated artwork, and tiny generated playable media. The manifest fixes
  UUID, complete file hashes, aggregate content digest, compatibility, and the
  container-relative layout. Any byte change requires a new fixture version.
- Persistence: immutable `FixtureDefinition` metadata is stored separately from
  binary package resources. A ManagedInstance has zero or one restrictive
  fixture reference. Trusted administration explicitly registers packaged
  definitions and assigns or removes compatible disposable assignments. Runtime
  reassignment refuses an incompatible retained assignment and never silently
  removes it or changes the fixture to make selection succeed.

## D-0070: Fixture copying is a narrow semantic broker capability

- Status: approved for Phase 6 Step 1
- Decision: `apply_assigned_fixture` accepts only ManagedInstance UUID and the
  expected immutable container ID. The broker resolves the disposable instance,
  exact revision, assignment, compatibility, package bytes, and digest from
  trusted state. It proves exactly one owned stopped container and exact revision
  label, independently revalidates the sole packaged archive, and copies it only
  to `/home/kodi/.kodi` with fixed uid/gid 1000 archive metadata.
- Boundary: **fixture copying is a narrow semantic broker capability, not a
  generic file-copy primitive.** No caller fixture ID, path, destination, bytes,
  URL, mount, command, script, exec, or JSON-RPC field exists. Safe archive names
  are manifest-validated; symlinks, hardlinks, absolute names, traversal, and
  content disagreement fail closed. Root performs no Kodi JSON-RPC.

## D-0071: Fixed unprivileged fixture initialization and proof

- Status: approved for Phase 6 Step 1
- Decision: after stopped archive application, reset uses normal broker start,
  the existing readiness coordinator, and a Kodi-21.3-only unprivileged adapter.
  The adapter has fixed calls for the fixture scan and movie/show/episode reads,
  waits at most 60 seconds, verifies stable synthetic metadata rather than Kodi
  row IDs, then reset normally stops the container. Whole or directory scan
  behavior is fixed by the implementation and is never caller-selectable.
- Validation support: one fixed test-only semantic mutation may remove only the
  known synthetic fixture movie after resolving its Kodi row ID internally. It
  accepts no title, path, media ID, method, or parameters and exists solely to
  prove reset removes disposable drift.

## D-0072: Reset coordination and exposure boundary

- Status: approved for Phase 6 Step 1
- Decision: reset uses a keyed non-blocking in-process lock per ManagedInstance
  and never holds a SQLite transaction across Docker, readiness, or scan work.
  This is not a distributed lease; broker proof gates remain the cross-process
  fail-closed defense. Multi-worker mutation exposure requires a later durable
  coordination decision.
- Boundary: fixture definition and assignment are durable, while reset results
  are ephemeral and no reset-job/history table is introduced. Web reset and
  fixture administration are deferred; existing authenticated Web lifecycle and
  matrix surfaces remain unchanged.

## D-0073: Kodi-generated configuration seed for small-mixed-v2

- Status: approved for Phase 6 Step 1B
- Decision: finalized `small-mixed-v1` remains byte-for-byte immutable historical
  evidence of the first design. Its live scan returned no entities because
  `sources.xml` makes a source visible but does not create the separate video
  database path/content/scraper assignments used by Kodi's scanner.
  `small-mixed-v2` therefore adds the immutable `MyVideos131.db` produced from
  the exact Kodi 21.3 image and schema. The seed contains only two fixed `path`
  rows, for Movies and TV Shows, using Kodi's installed `metadata.local` scraper;
  every movie, show, episode, file, artwork, play-state, and user-state table is
  empty before scan.
- Materialization boundary: one development-only, argument-free helper applies
  fixed SQL to a stopped fresh Kodi-generated database. No runtime or broker SQL
  operation exists. The seed hash and configuration are manifest-pinned and
  read-only audited before packaging. It is copied through the existing
  `apply_assigned_fixture` operation to its fixed Kodi database location; no
  broker request field or Docker privilege is added.
- Scanner boundary: Kodi still creates all media entities and relationships from
  the repository-owned NFO/media/artwork. Initialization scans the fixed movie
  directory first, then the fixed TV directory after the first scan completes,
  because Kodi does not run concurrent library scans. The single 60-second
  semantic deadline and fixed JSON-RPC allowlist remain in force.

## D-0074: Application-owned ephemeral reset status and read-only Web projection

- Status: approved for Phase 6 Step 2
- Decision: one `ResetStatusService` composes ManagedInstance type, the exact
  persisted RuntimeRevision pin, fixture assignment and compatibility, fresh
  broker inventory, and a bounded process-local reset activity registry into a
  semantic DTO. Reset orchestration publishes only its real phase transitions
  and bounded outcomes through an injected registry; completed state is
  ephemeral, bounded in memory, and disappears on process restart.
- Web boundary: authenticated HTML and
  `GET /api/instances/{instance_id}/reset-status` consume the same DTO. They
  expose reset eligibility, safe reason, fixture identity/category/version,
  exact pin, current container semantics, activity/outcome, and reconciliation
  warning without container identity, image, paths, exceptions, or mutation
  authority. No reset/fixture mutation route, durable job/history, distributed
  lock, migration, or broker operation is added.

## D-0075: Reset-specific durable operation lease and bounded retention

- Status: approved for Phase 6 Step 3
- Decision: `ResetOperation` is reset-only durable coordination, not a generic
  job payload. SQLite enforces one queued/running row per ManagedInstance with a
  partial unique index. Acquisition and each phase or terminal update use
  conditional writes bound to the SHA-256 digest of an opaque high-entropy
  worker token and a fresh ten-minute lease. Meaningful phases renew the
  heartbeat; readiness polling does not write SQLite.
- Crash and retention: queued work may be acquired after restart. Expired
  started work is never resumed and becomes reconciliation-required with its
  last safe phase retained. The latest 20 terminal rows per instance are kept;
  active rows are never pruned. No generic queue, cancellation, retry, or worker
  framework is introduced.

## D-0076: Confirmed localhost Web reset queues outside request lifetime

- Status: approved for Phase 6 Step 3
- Decision: authenticated confirmation shows exact release/revision, fixture,
  destructive disposable-state warning, no-upgrade promise, and stopped
  completion. The CSRF/same-origin POST accepts only literal confirmation plus
  the route instance UUID, revalidates current status, creates or returns the
  sole active operation, submits it to the reset executor, and returns 202.
- Boundary: durable state is authoritative across processes; local activity is
  supplemental. Web lifecycle and trusted fixture/revision changes reject an
  active reset. Web still reaches Docker only through the broker client. Stale
  acknowledgement remains read-only until its audit/evidence semantics are
  defined; there is no auto-resume or generic repair control.

## D-0077: Shared fixture semantics use exact-release Kodi database seeds

- Status: approved for Phase 6 Step 4
- Decision: repository-owned media, NFO metadata, artwork, fixed source paths,
  scan sequencing, and semantic verification may be byte-identical across
  proven Kodi releases. The configuration-only video database seed may not be
  shared across releases: each immutable fixture version pins the exact
  Kodi-created database filename, schema, seed hash, and compatible release.
- Kodi 20.5 result: `small-mixed-v3` uses `MyVideos121.db` created by the exact
  validated Kodi 20.5 image. Its development-only fixed materializer adds the
  same two `metadata.local` path/content rows as v2; the schema-aware audit
  requires every other Nexus table to remain empty. The existing broker
  operation, reset service, scanner methods, durable reset coordination, and
  Web authority remain unchanged.

## D-0078: Bounded fixture content taxonomy and trusted semantic specifications

- Status: approved for Phase 6 Step 6
- Decision: persisted `FixtureContentCategory` supports exactly `mixed` and
  `movies`. Migration `0009_fixture_content_taxonomy` widens the constrained
  column to `VARCHAR(16)` while preserving all definitions, assignments, pins,
  and reset operations. Downgrade refuses while any Movies definition exists;
  it never rewrites or deletes taxonomy data. TV remains unsupported.
- Small/Movies contract: canonical V1 names the Small size and Movies content
  category but does not define a count. The first executable Small/Movies
  definition is therefore exactly three deterministic synthetic movies. Its
  trusted package declares exact entity identities and the Movies-only scan
  plan. Callers cannot supply expectations, counts, paths, or scan choices.
- Boundary: reset resolves the immutable package specification for the assigned
  FixtureDefinition and passes it to the fixed unprivileged adapter. The reset
  service does not branch on fixture slug; the broker operation, archive-copy
  authority, exact RuntimeRevision pin, Web reset request, and durable reset
  coordination are unchanged.

## D-0079: Complete Small taxonomy with a TV-only semantic fixture

- Status: approved for Phase 6 Step 7
- Decision: migration `0010_fixture_tv_taxonomy` extends the closed persisted
  content taxonomy to exactly `mixed`, `movies`, and `tv`. Existing data and the
  bounded `VARCHAR(16)` column are unchanged. Downgrade refuses while any TV
  FixtureDefinition exists and never converts or deletes taxonomy data.
- Small/TV contract: canonical V1 names Small and TV but defines no count. The
  executable v1 dataset is one show, one season, and three episodes. Trusted
  package expectations require the exact show, each episode's stable identity,
  show relationship, season 1, episode numbers 1 through 3, and zero movies.
- Scan and drift boundary: trusted specification selects only the fixed TV
  scan. The validation-only episode mutation resolves Episode 1 from packaged
  expectations and invokes fixed `VideoLibrary.RemoveEpisode`; it accepts no
  ID, title, path, method, or parameters. Reset orchestration and broker
  authority remain unchanged.

## D-0080: Medium size taxonomy and bounded Medium/Mixed definition

- Status: approved for Phase 6 Step 9
- Decision: migration `0011_fixture_medium_taxonomy` extends the closed
  persisted size taxonomy to exactly `small` and `medium`, using bounded
  `VARCHAR(16)`. Existing definitions, assignments, pins, and ResetOperations
  are preserved. Downgrade refuses while a Medium FixtureDefinition exists and
  never converts or deletes taxonomy data. Large remains unsupported.
- Medium/Mixed contract: canonical V1 names Medium but defines no counts. The
  executable v1 dataset is exactly 25 movies and five shows, each with two
  seasons of five episodes: 25 movies, 5 shows, 50 episodes, and 80 primary
  library entities. The package lists every stable identity and relationship.
- Scale boundary: verification indexes returned entities by package-owned
  stable identity rather than using nested searches. The scan plan remains the
  fixed Movies-then-TV sequence and retains the 60-second fixture deadline.
  The package-defined validation drift removes only Movie 001 and Series 01
  S01E01. Reset orchestration, exact-pin selection, broker authority, Web reset,
  and the ten-minute durable lease are unchanged.

## D-0081: Disposable-only immutable snapshot creation

- Status: approved for Phase 7 Step 2
- Decision: v1 snapshot creation supports only disposable ManagedInstances.
  Each capture receives a new server-generated UUID and records the exact source
  instance, RuntimeRevision, Kodi release, fixture assignment, capture time, and
  prior running/stopped state. Persistent capture, restore, and delete remain
  unsupported.

## D-0082: Closed Kodi profile capture scope

- Status: approved for Phase 7 Step 2
- Decision: capture is rooted at `/home/kodi/.kodi` and includes only
  `userdata`, `addons`, `fixture-media`, and `.smb`. It excludes runtime temp,
  add-on packages/temp, thumbnails, texture databases, logs, crash/core files,
  and PID/lock artifacts. Unknown top-level entries and every link, socket,
  FIFO, device, or other non-regular/non-directory object fail capture.

## D-0083: Deterministic bounded archive and canonical manifest

- Status: approved for Phase 7 Step 2
- Decision: v1 uses an uncompressed deterministic tar with sorted relative
  members, normalized Kodi uid/gid, zero mtime, and safe mode bits. Manifest v1
  is canonical UTF-8 JSON with archive identity and a path/type/size/mode/digest
  record for every member. SHA-256 independently identifies archive and
  manifest bytes.
- Bounds: archive 16 GiB, individual file 8 GiB, 250,000 members, 512 UTF-8
  path bytes, and 64 MiB manifest. Capture requires free space of at least
  `max(2 GiB, estimated archive bytes + 1 GiB)` and fails closed when free space
  cannot be established.

## D-0084: Root-owned central atomic snapshot storage

- Status: approved for Phase 7 Step 2
- Decision: the broker publishes only to
  `KODI_LAB_DATA_ROOT/snapshots/<snapshot-uuid>/`, containing exactly
  `manifest.json` and `state.tar`. The final directory is root:`kodi-lab` 0750
  and both files are 0440. UUID-derived staging and rename provide atomic
  publication. Labels never affect a path; successful bytes are immutable.

## D-0085: Broker capture is one semantic authority

- Status: approved for Phase 7 Step 2
- Decision: `capture_managed_instance_snapshot` accepts exactly the snapshot
  UUID, ManagedInstance UUID, and expected immutable container ID. Broker-side
  trusted state resolves revision, fixture, fixed source, storage root,
  ownership, exclusions, and bounds. One exact owned stopped disposable
  container with matching revision is required. No generic copy, tar, exec,
  destination, path, Docker field, archive, or member-list authority is added.

## D-0086: Snapshot and create-operation persistence

- Status: approved for Phase 7 Step 2
- Decision: `Snapshot` is retained without automatic pruning and moves through
  creating, available, failed, or quarantined. `SnapshotCreateOperation` remains
  a separate bounded durable record with queued/running/succeeded/failed/
  reconciliation-required states, real capture phases, ten-minute ownership,
  safe failure text, and bounded terminal operation history. It is not a
  generic job model.

## D-0087: Narrow mutation and global-heavy leases

- Status: approved for Phase 7 Step 2
- Decision: one durable per-instance `ManagedInstanceMutationLease` excludes
  the closed kinds reset, snapshot create/restore, lifecycle, fixture
  assignment, and runtime reassignment. Reset retains its typed operation while
  using this lease. A separate singleton lease permits one snapshot create or
  future restore globally. Ordinary lifecycle work on other instances is not
  globally serialized.

## D-0088: Capture preserves prior runtime state

- Status: approved for Phase 7 Step 2
- Decision: a stopped instance remains stopped. A running instance is normally
  stopped and proven stopped before capture, then restarted through existing
  broker lifecycle and normal Kodi readiness. Capture succeeds only after
  independent archive/manifest validation, recognized Kodi video-database
  `PRAGMA integrity_check`, unchanged revision/fixture pins, and required
  prior-state restoration.
- Split outcome: valid bytes remain available if restart/readiness later fails,
  while the create operation reports failure or reconciliation required. Valid
  artifacts are never deleted to disguise source-state recovery failure.

## D-0089: Snapshots are secret-bearing and Web metadata-only

- Status: approved for Phase 7 Step 2
- Decision: authenticated same-origin CSRF-protected Web routes create, list,
  and poll status only. An optional immutable label is trimmed, limited to 120
  Unicode scalar values, and excludes controls and path separators. Browser
  responses expose safe identity/status metadata but no paths, member names,
  raw manifest, bytes, preview, download, import, restore, or delete authority.

## D-0090: Runtime and fixture semantics remain independent

- Status: approved for Phase 7 Step 2
- Decision: snapshot capture records but never changes the exact
  RuntimeRevision or current FixtureDefinition assignment. Snapshot creation is
  not an upgrade mechanism. Reset continues to restore the assigned fixture
  baseline and does not consume snapshots. There is no automatic snapshot
  retention or deletion.

## D-0091: Empty-only Kodi runtime scaffolding

- Status: approved for Phase 7 Step 2 follow-up
- Decision: exact top-level directories `media` and `system` beneath
  `/home/kodi/.kodi` are recognized empty-only Kodi runtime scaffolding.
  Each may exist only as an empty directory and is omitted from both archive
  and manifest. Any member beneath either directory, of any type, fails capture
  with a bounded typed error before publication.
- Boundary: recognized empty-only scaffolding is distinct from included and
  excluded state. It is not a recursive allowlist or a general exception for
  empty directories. Every other unknown top-level profile entry, whether
  empty or populated, continues to fail closed. Both source-enumeration passes
  apply the same classification so newly appearing scaffolding content cannot
  be silently omitted.

## D-0092: Source-bound exact-compatible disposable restore

- Status: approved for Phase 7 Step 3
- Decision: an available Snapshot may be restored only to its recorded source
  disposable ManagedInstance. The current RuntimeRevision and fixture
  assignment, including null assignment, must exactly equal the captured
  identities before any Docker mutation. Restore never selects or rewrites a
  revision or fixture. Persistent, cross-instance, cross-revision, and
  cross-fixture restore remain unsupported.
- Separation: restore replaces captured mutable Kodi state. Reset continues to
  recreate the current assigned fixture baseline and ignores snapshots.

## D-0093: Fresh-container restore and one semantic broker apply

- Status: approved for Phase 7 Step 3
- Decision: restore validates the immutable central artifact, preserves the
  current running/stopped state, removes the exact stopped old container,
  provisions a fresh container from the unchanged exact pin, and applies the
  snapshot only to `/home/kodi/.kodi`. It never applies a fixture first.
- Boundary: `apply_managed_instance_snapshot` accepts exactly Snapshot UUID,
  source ManagedInstance UUID, and expected fresh container ID. The broker
  resolves the UUID-derived artifact and fixed destination, independently
  revalidates it, admits only the known fresh runtime seed profile, extracts no
  unsafe type, and reads the result back to prove manifest hashes, modes,
  inventory, and recognized SQLite integrity. No generic copy, tar, path,
  destination, exec, archive, or Docker authority is introduced.

## D-0094: Restore-specific durability and destructive reconciliation

- Status: approved for Phase 7 Step 3
- Decision: `SnapshotRestoreOperation` is a distinct queued/running/succeeded/
  failed/reconciliation-required record with bounded semantic phases, prior
  source state, and a ten-minute lease. It uses the existing per-instance
  mutation and singleton global-heavy leases; create and restore cannot run
  globally together, while reset and lifecycle conflict only on their target
  instance. It is not a generic job table.
- Failure: before old-container removal, an operation that stopped a running
  source attempts normal start/readiness recovery. After removal, provision,
  apply, verification, or prior-state recovery uncertainty requires
  reconciliation. A structurally restored container whose readiness fails is
  retained stopped when provable. Expired running restores are never resumed
  destructively and become reconciliation-required.

## D-0095: Explicit destructive confirmation without automatic backup

- Status: approved for Phase 7 Step 3
- Decision: restore creates no hidden safety snapshot. Authenticated
  same-origin, CSRF-protected Web confirmation warns that current mutable state
  is discarded and no automatic backup exists. POST accepts only
  `confirmed=true`, revalidates current eligibility, returns 202, and exposes
  only bounded operation status. Snapshot artifacts and manifests remain
  immutable, secret-bearing, and unavailable to Web download or browsing.

## D-0096: Explicit artifact-first snapshot deletion

- Status: approved for Phase 7 Step 4
- Decision: only an authenticated administrator may explicitly delete one
  Snapshot. Available, failed, and quarantined records atomically enter the
  non-restorable `deleting` state before filesystem work. Successful deletion
  removes the exact artifact first, independently proves absence, and only then
  removes the Snapshot row. There is no TTL, retention cap, pruning, bulk
  deletion, or automatic disk-pressure cleanup.
- History: `SnapshotDeleteOperation` is a separate bounded durable record. It
  copies the Snapshot and source ManagedInstance UUIDs and therefore survives
  removal of the Snapshot row; create and restore operation histories retain
  their immutable target UUIDs for the same reason. The primary Snapshot table
  has no deletion tombstone.

## D-0097: UUID-only semantic artifact removal

- Status: approved for Phase 7 Step 4
- Decision: `delete_managed_snapshot_artifact` accepts only a canonical
  Snapshot UUID. The broker proves the trusted record is `deleting`, derives
  the fixed `KODI_LAB_DATA_ROOT/snapshots/<uuid>` path, verifies storage and
  target identity, and recursively removes only that descriptor-relative UUID
  subtree without following links. No path, filename, wildcard, recursive
  option, shell, Docker, or generic filesystem authority is exposed.
- Absence: an absent artifact is acceptable only after the Snapshot is already
  `deleting`, covering the proven crash window after filesystem removal. An
  available Snapshot whose artifact is absent cannot be silently deleted.

## D-0098: Delete-specific global coordination and reconciliation

- Status: approved for Phase 7 Step 4
- Decision: snapshot delete joins create and restore under the singleton
  global-heavy lease, but takes no per-instance mutation lease because it does
  not change Kodi, container, runtime, or fixture state. Snapshot `deleting`
  also independently blocks restore and duplicate deletion. Ordinary source
  lifecycle and reset are not globally serialized by deletion.
- Failure: metadata is never removed without independently proven artifact
  absence. Partial or uncertain removal leaves the Snapshot `deleting` and the
  operation failed or reconciliation-required; it never rolls back to
  available without proof that deletion did not begin. Expired running delete
  work is inspected and terminalized reconciliation-required, never
  destructively resumed.

## D-0099: Per-snapshot Web deletion is confirmation-only

- Status: approved for Phase 7 Step 4
- Decision: authenticated same-origin, CSRF-protected Web confirmation exposes
  safe Snapshot metadata and warns that deletion is permanent, disables future
  restore, and does not alter the source Kodi instance. POST accepts only
  `confirmed=true`, revalidates the source-instance association and state,
  queues durable work, and returns 202. While deleting, restore and duplicate
  delete actions disappear; after success the Snapshot disappears while safe
  operation history remains. No download, content view, retention control,
  bulk delete, or delete-all route exists.

## D-0100: Detached Snapshot source binding

- Status: approved for Phase 7 Step 6
- Decision: Snapshot source ownership has an independently closed binding:
  `attached` requires a live nullable source ManagedInstance FK, while
  `deleted_source` requires that FK to be null. Every Snapshot retains an
  immutable non-FK original source ManagedInstance UUID. Detachment also copies
  the bounded source display name and deletion timestamp. Artifact UUID, path,
  bytes, digests, runtime/fixture references, capture time, label, and validity
  state do not change or move.

## D-0101: Detached Snapshots are archive-only in v1

- Status: approved for Phase 7 Step 6
- Decision: `deleted_source` Snapshots cannot be restored. Eligibility rejects
  them as `source_instance_deleted` before mutation/global-heavy leases, Docker,
  or broker work. Detachment supplies no clone, import, cross-instance restore,
  original-instance recreation, or undelete semantics.

## D-0102: Explicit instance-deletion Snapshot policy

- Status: approved for Phase 7 Step 6
- Decision: disposable ManagedInstance deletion requires both explicit
  confirmation and an explicit `preserve_snapshots` Boolean, with no default.
  The server reloads and applies that policy to the complete authoritative
  Snapshot set; the browser supplies no Snapshot UUID list. Creating/deleting
  Snapshot state or any active reset/create/restore/delete/mutation blocks
  queueing before partial work.

## D-0103: Container-first durable instance deletion

- Status: approved for Phase 7 Step 6
- Decision: `ManagedInstanceDeleteOperation` is a dedicated typed durable
  disposable-only workflow, never a direct Web repository deletion or generic
  job. It owns `INSTANCE_DELETE` per-instance mutation authority. The exact
  running/stopped container is stopped when necessary, removed, and proven
  absent before preserved metadata is detached or destructive Snapshot work
  starts.

## D-0104: Deletion history and bounded notes survive

- Status: approved for Phase 7 Step 6
- Decision: reset and Snapshot create/restore/delete histories retain their
  immutable ManagedInstance UUID without requiring a live parent. The delete
  operation itself retains original instance, revision, fixture, Snapshot
  counts/bytes/progress, prior container state and bounded reconciliation data.
  It does not copy credentials, paths, health, mutable runtime state, Snapshot
  contents, or manifests, and it is not automatically pruned in this milestone.

## D-0104A: ManagedInstance notes are bounded at 4,096 code points

- Status: approved for Phase 7 Step 6
- Decision: `ManagedInstance.notes` and the one-time
  `ManagedInstanceDeleteOperation.notes_at_deletion` copy accept `None`, empty,
  or at most 4,096 Unicode code points. They are never trimmed, normalized,
  rewritten, or truncated; oversized input and migration data are rejected.
  Application validation is authoritative and SQLite uses a compatible
  `length(...) <= 4096` check. Notes are potentially sensitive: confirmation
  exposes presence only, and Snapshot metadata/history never receives them.

## D-0105: Global preserved browsing and exact deletion

- Status: approved for Phase 7 Step 6
- Decision: authenticated global preserved-Snapshot browsing exposes only safe
  source, runtime, fixture, time, size, label, UUID, binding, and validity
  metadata, with Delete but no Restore. Detached deletion reuses the proven
  `SnapshotDeleteOperation` and UUID-only artifact-first broker core without a
  live source FK or generic filesystem authority. Its retained history keeps
  the immutable original source UUID.

## D-0106: Instance-deletion reconciliation is fail closed

- Status: approved for Phase 7 Step 6
- Decision: expired or stale destructive instance deletion is never resumed.
  Read-only inspection reports instance/container presence, policy, remaining
  attached/detached/deleting rows, artifact presence, and durable progress.
  Preserve success atomically detaches every Snapshot, removes instance/Target
  metadata, releases authority, copies bounded notes, and terminalizes history.
  Delete success retains artifact-first ordering for every UUID before the same
  final metadata transaction.

## D-0107: Expired destructive reservations use one closed stale dispatcher

- Status: approved for Phase 7 Step 8
- Decision: application startup and every global-heavy or conflicting
  per-instance reservation invoke one application-owned dispatcher over the
  closed persisted operation-kind enums. It supports reset, Snapshot create,
  restore and delete, and ManagedInstance delete. Before changing anything it
  proves operation kind/UUID, instance identity, expiry, and token-digest
  ownership, including paired leases. Unknown, orphaned, terminal-owner,
  mismatched, or impossible reservations fail closed as coordination
  corruption and are not deleted. The dispatcher never resumes broker, Docker,
  or filesystem work.

## D-0108: Lease expiry is separate from semantic reconciliation

- Status: approved for Phase 7 Step 8
- Decision: an exactly proven expired coordination reservation is released so
  it cannot block unrelated work forever, while its running durable operation
  becomes `reconciliation_required` and retains its last semantic phase.
  Snapshot create keeps its quarantine rule. A central typed reconciliation
  gate, not an expired lease, blocks mutations whose trusted state is
  uncertain. Snapshot-delete reconciliation is Snapshot-scoped; safe
  quarantined create state does not block unrelated instance lifecycle, but
  ambiguous source-state recovery does.

## D-0109: Reconciliation presentation is bounded and read-only

- Status: approved for Phase 7 Step 8
- Decision: authenticated instance and Snapshot reconciliation projections
  expose only typed operation kind/UUID/state, last semantic phase, safe
  reason, semantic observed state, timestamps, and blocked actions. Multiple
  unresolved records are explicit and fail closed. Container IDs, lease
  digests, broker payloads, paths, Snapshot members, manifests, and raw errors
  are excluded. Views offer no retry, acknowledge, finalize, clear, or repair
  action.

## D-0110: Unresolved operation safety records are never pruned

- Status: approved for Phase 7 Step 8
- Decision: reset and Snapshot create/restore/delete pruning retains the newest
  20 ordinary `succeeded`/`failed` rows per immutable original instance and
  operation type. `queued`, `running`, and `reconciliation_required` rows are
  never pruning candidates. `ManagedInstanceDeleteOperation` remains
  unbounded. This is operation-history retention only and never prunes Snapshot
  artifacts.

## D-0111: Lifecycle history is typed synchronous operation state

- Status: approved for Phase 7 Step 9
- Decision: provision, start, stop, and deprovision each create exactly one
  `ManagedInstanceLifecycleOperation` while holding the existing per-instance
  lifecycle mutation lease. The closed operation kind has no arbitrary action,
  queue, worker, Docker command, raw broker response, log, path, network field,
  or lease material. The existing synchronous lifecycle service remains the
  sole mutation authority.
- Semantics: the row starts `running` before broker mutation and records a
  trusted post-lease prior state from the closed vocabulary `unprovisioned`,
  `stopped`, `running_usable`, `running_not_ready`, `transitional`, or
  `ambiguous`. It terminalizes `succeeded`, `failed`, or
  `reconciliation_required` with observed resulting state, timestamps, and at
  most one bounded safe reason. Success additionally requires the exact
  kind-specific postcondition.

## D-0112: Lifecycle history survives instance deletion with bounded retention

- Status: approved for Phase 7 Step 9
- Decision: every lifecycle row retains the immutable original ManagedInstance
  UUID and bounded name snapshot. Its optional live ManagedInstance FK uses
  `ON DELETE SET NULL`, so deletion cannot erase or block history. There is no
  instance tombstone or generic event table.
- Retention: per original UUID, pruning retains the newest 20 ordinary
  succeeded/failed lifecycle rows. Running and reconciliation-required rows
  are never pruning candidates.

## D-0113: Interrupted lifecycle uses the Step 8 stale dispatcher

- Status: approved for Phase 7 Step 9
- Decision: an exactly matched expired lifecycle lease and running typed row
  are handled by the existing cross-kind dispatcher. Trusted current inventory,
  process, and readiness observations produce only a closed semantic result;
  the operation becomes reconciliation-required and the exact lease is
  released atomically. No lifecycle, broker, or Docker action resumes.
- Blocking policy: for interrupted provision, start, stop, and deprovision,
  `unprovisioned`, `stopped`, `running_usable`, or `running_not_ready` is an
  explicit warning but does not itself block a new mutation; each new request
  still enforces its normal state precondition. `transitional` or `ambiguous`
  blocks all instance mutations. Multiple unresolved records continue to fail
  closed.

## D-0114: Unified activity is a deterministic read projection

- Status: approved for Phase 7 Step 9
- Decision: `ManagedInstanceActivityItem` projects retained lifecycle, reset,
  Snapshot create/restore/delete, and instance-delete records without
  persisting a duplicate activity row. Ordering is requested time descending,
  operation UUID descending, then a fixed source-kind tie break. The live
  detail view uses ten items; full history is not silently limited.
- Exposure: items contain bounded kind, operation/original-instance UUID,
  timestamps, typed outcome, safe semantic summary, reconciliation flag, and
  only necessary Snapshot/lifecycle/deletion subtypes. They exclude notes,
  container/image identity, leases, paths, broker errors, manifests, members,
  credentials, and media identity.

## D-0115: Instance and deleted-history presentation is narrow and read-only

- Status: approved for Phase 7 Step 9
- Decision: authenticated live detail composes existing runtime, fixture,
  Snapshot, readiness, reconciliation, and activity DTOs and shows the current
  bounded notes value with exact whitespace and HTML escaping. Notes have no
  edit route. Dashboard cards link to detail rather than duplicating history.
- Deleted history: a global view is anchored only by surviving typed
  `ManagedInstanceDeleteOperation` records and grouped by immutable original
  UUID. Detail projects all retained typed history and preserved Snapshot
  backlinks. It exposes neither notes-at-deletion nor restore, undelete,
  acknowledgment, finalization, retry, repair, export, or retention controls.

## D-0116: Phase 8 input taxonomy starts with local directories only

- Status: approved for Phase 8 Step 1
- Decision: `DevelopmentSource` has one closed source kind,
  `local_directory`. ZIP installation and repository URL testing remain
  separate later Phase 8 workflows. A Git checkout may later be registered as
  an ordinary local directory, but registration records no repository, remote,
  branch, commit, or revision semantics.
- Boundary: Step 1 adds no ManagedInstance association, deployment, copy/sync,
  bind mount, broker, Docker, Web mutation, add-on inventory, watcher, history,
  or reconciliation authority.

## D-0117: Development-source locators are confined beneath one explicit root

- Status: approved for Phase 8 Step 1
- Decision: `KODI_LAB_DEVELOPMENT_ROOT` is unset by default because no safe
  general host path exists. Registration is unavailable until an administrator
  explicitly configures one normalized non-root absolute directory. Requests
  carry only a canonical POSIX-style relative locator with no empty, dot,
  parent, absolute, NUL, backslash, or overlong component.
- Resolution: validation opens the configured absolute path and every locator
  component descriptor-relatively with `O_DIRECTORY`, `O_NOFOLLOW`, and bounded
  path rules. Missing, unreadable, non-directory, linked, or escaped state fails
  closed. The absolute effective path is never persisted or returned in the
  ordinary application DTO. There is no filesystem browser or generic host-path
  authority.

## D-0118: DevelopmentSource is immutable registration identity and observation

- Status: approved for Phase 8 Step 1
- Decision: a global reusable registration has server-generated UUID, trimmed
  NFC logical name, closed source kind, canonical relative locator, parsed
  add-on ID, observed name/version, sorted unique extension points, and UTC
  registration/validation timestamps. Logical name is bounded to 120 Unicode
  code points; observed name and version are bounded to 256 and 128.
- Uniqueness: logical name, relative locator, and parsed add-on ID are each
  globally unique. Canonical V1 calls sources global/reusable and does not
  establish alternative trees for one add-on ID; one registration therefore
  supplies one unambiguous future attachment identity.
- Mutability: parsed add-on ID is stable identity and is never caller supplied.
  Name, version, and content are registration-time observations of a mutable
  directory. Later filesystem changes do not rewrite catalog data. An add-on ID
  change requires a future explicit replacement/re-registration workflow.

## D-0119: Registration validates only bounded root addon.xml metadata

- Status: approved for Phase 8 Step 1
- Decision: registration opens exact root-level `addon.xml` without following
  links, requires one regular file with link count one, bounds it to 1 MiB, and
  parses UTF-8 XML without executing source code. DTD and entity declarations
  are rejected before Python's non-networking ElementTree parser runs.
- Metadata: the root must be `addon`; ID, name, version, and at least one
  extension point are required. IDs use 1 to 128 lowercase ASCII letters,
  numbers, dots, underscores, or hyphens, begin with a letter or number, and
  reject consecutive dots. This follows Kodi's documented lowercase ID
  character set while remaining safe as future derived destination identity.
  Extension points use the same bounded identifier alphabet, are limited to 64,
  and persist as sorted unique canonical JSON.
- Boundary: dependencies are not stored and create no compatibility policy.
  Registration does not recursively inspect the source, so VCS directories and
  full tree safety remain deployment decisions. A reusable read-only validator
  may observe current metadata but never updates the persisted registration.

## D-0120: Attachment is durable mode-neutral product metadata

- Status: approved for Phase 8 Step 2
- Decision: `ManagedInstanceDevelopmentSource` is a separate many-to-many
  association between one disposable ManagedInstance and one global
  DevelopmentSource. The pair is unique, and the application independently
  enforces one attached source per registered add-on ID on each instance.
- Meaning: attachment records association only. It proves no deployment,
  freshness, installation, enablement, Kodi loading, or materialization mode.
  Reset and Snapshot create/restore leave rows unchanged and never deploy.
  Successful instance deletion removes only its associations; global source
  registrations survive.

## D-0121: Attachment changes have typed synchronous durable history

- Status: approved for Phase 8 Step 2
- Decision: attach and detach create terminal succeeded/failed
  `DevelopmentSourceAttachmentOperation` records rather than generic events or
  queued work. Successful association change and terminal history are one
  transaction. Each row retains immutable original ManagedInstance UUID,
  nullable live FK, source UUID, bounded logical-name/add-on-ID snapshots,
  timestamps, and an optional bounded safe failure reason.
- Retention and exposure: history survives ManagedInstance deletion and joins
  the deterministic activity projection with fixed metadata-only summaries.
  It contains no source locator, host path, source content, deployment state,
  broker response, or lease material.

## D-0122: Attachment administration revalidates identity under instance authority

- Status: approved for Phase 8 Step 2
- Decision: one application service owns `attach_source`, `detach_source`, and
  `list_attached_sources`. Attach reuses the descriptor-relative Step 1
  validator and requires the current parsed add-on ID to equal the immutable
  registered ID. Changed name/version is allowed and does not rewrite catalog
  observations; missing, invalid, or identity-changed sources fail with typed
  errors. Detach performs no filesystem or runtime cleanup.
- Coordination: attach/detach acquire the closed
  `development_source_attachment` per-instance mutation lease, use no global
  heavy lease, and consult the central reconciliation gate. They are allowed
  regardless of provisioned/running state for disposable instances. Persistent
  attachment, deployment, broker source operations, and Web mutation remain
  deferred.

## D-0122A: Runtime execution profile is authoritative for filesystem ownership

- Status: approved prerequisite for Phase 8 Step 4
- Decision: every immutable `RuntimeExecutionProfile` requires numeric
  `runtime_uid` and `runtime_gid` values from 0 through 2147483647. They identify
  the UID/GID that Kodi-managed profile and add-on files must use inside that
  exact curated runtime execution environment. Zero remains representable even
  though every currently curated v1 profile uses 1000/1000.
- Authority: ownership is create-only catalog data attached to the exact runtime
  revision's execution profile. It is never inferred during an operation from
  a live container, Dockerfile, image metadata, archive, host account, username,
  group name, or snapshot convention, and callers cannot supply or override it.
  Changing ownership semantics requires a new revision/profile under existing
  catalog versioning and trust rules.
- Migration: `0019_runtime_execution_ownership` backfills only five explicitly
  validated retained profiles: Kodi 19.5 revision 1, Kodi 20.5 revision 1, Kodi
  21.3 revisions 1 and 2, and Kodi 22.0 Beta 2 revision 1. Each exact
  revision/profile/image identity is mapped to its proven 1000/1000 ownership.
  Unknown or mismatched rows fail upgrade; there is no default. Populated
  downgrade is refused rather than discarding ownership semantics.
- Boundary: existing fixture and Snapshot UID/GID constants remain their
  established archive-format contracts. This decision adds no deployment,
  archive conversion, broker mutation, Web field, or DevelopmentSource route.
  Phase 8 copy/sync must resolve ownership through ManagedInstance, its pinned
  RuntimeRevision, and this trusted profile.

## D-0123: Step 4 is stopped-only filesystem materialization

- Status: approved for Phase 8 Step 4
- Decision: copy/sync accepts only an already attached `local_directory`
  DevelopmentSource and an exactly provisioned, stopped, disposable
  ManagedInstance. It is manually requested and changes only filesystem bytes.
  It never stops, starts, or restarts Kodi, invokes JSON-RPC, enables an add-on,
  installs a ZIP/repository, creates a bind mount, watches files, or replaces an
  untrusted destination. Success proves materialization, not Kodi recognition.

## D-0124: Recursive source capture is closed, bounded, and no-follow

- Status: approved for Phase 8 Step 4
- Decision: descriptor-relative traversal admits only directories and readable
  single-link regular files. Symlinks, hardlinks, special files, unsafe or
  invalid UTF-8/control/backslash names, traversal, and inconsistent objects
  fail closed. Exact `.git`, `.hg`, and `.svn` directories are excluded at any
  depth only after proving that the excluded object is a safe directory.
- Bounds: 25,000 regular files, 50,000 total entries, 1 GiB included bytes,
  256 MiB per file, 512 UTF-8 bytes per relative path, 255 per component, depth
  32, and 16 MiB canonical manifest. These are not caller configurable.

## D-0125: Deployment identity is a deterministic immutable capture

- Status: approved for Phase 8 Step 4
- Decision: the broker constructs a root-private normalized stage and hashes a
  canonical ordered manifest containing versioned policy, path, type, mode,
  size, and regular-file SHA-256. Directories become 0755; files become 0644
  plus source execute bits. Ownership comes only from D-0122A; source ownership,
  ACLs, xattrs, special bits, and mtimes are not preserved.
- TOCTOU: after application metadata revalidation, the broker independently
  resolves and captures the registered tree, validates add-on identity, then
  performs a second complete walk/hash. Any difference fails before destination
  mutation. Ordinary API, UI, history, and logs expose no member paths/hashes.

## D-0126: Existing destinations require exact retained provenance

- Status: approved for Phase 8 Step 4, clarified 2026-09-16
- Decision: an absent server-derived destination permits complete initial
  materialization. The broker may materialize its fixed `.kodi/addons` ancestor
  hierarchy beneath fixed `/home/kodi` when the curated stopped image has not
  created it; this adds no caller path or generic filesystem authority. An
  existing destination permits synchronization only when
  retained successful same-instance/source/add-on evidence has a trusted exact
  runtime/container context and a fresh complete bounded readback exactly
  matches its retained full destination digest. A state row alone is not proof;
  mismatch or unknown provenance is a typed replacement conflict with no
  override.
- Sync: only new files and changed bytes or normalized execute modes enter the
  delta. Unchanged paths do not. No path is deleted; file/directory replacement
  therefore conflicts. Destination-only preservation means an entry present in
  the previously verified full destination but absent from the current source
  manifest, normally because a formerly deployed source file was removed from
  source. It remains byte/mode/ownership identical and is included in the new
  full destination evidence. An independently introduced or altered destination
  entry is unexplained drift, invalidates provenance, and remains a replacement
  conflict; synchronization never adopts it.
- Verification: every current source entry must match bytes, normalized mode,
  and revision-derived ownership; retained destination-only entries must remain
  unchanged; no other entry may appear; container/revision/stopped identity must
  remain exact. Source and full destination digests may differ after a no-delete
  sync. Partial or unverifiable application becomes `reconciliation_required`
  without rollback deletion.

## D-0127: Deployment evidence is separate from attachment

- Status: approved for Phase 8 Step 4
- Decision: `DevelopmentSourceDeployment` is last-successful copy evidence for
  one instance/source pair. It stores source and complete verified destination
  digests separately, immutable operation/runtime/container context, add-on and
  observed metadata snapshots, verification time, and confidence
  `verified_at_last_operation`, `stale`, or `unknown`. Absence means never
  deployed; old evidence is never described as current.
- Invalidation: reset, Snapshot restore, and every runtime/container replacement
  mark evidence stale before destruction while preserving attachment. Ambiguous
  interrupted deployment marks it unknown. Snapshot create alone does not
  invalidate. Instance deletion removes current evidence, retains the global
  source, and preserves detached typed deployment history.

## D-0128: Copy/sync is a durable dual-coordinated operation

- Status: approved for Phase 8 Step 4
- Decision: `DevelopmentSourceDeploymentOperation` has fixed `copy_sync`
  request kind; queued, running, succeeded, failed, and
  reconciliation-required states; closed acquire through finalize phases; and
  initial, synchronized, or unchanged success classification. Only safe
  aggregate counts and transferred bytes are retained. The newest 20 ordinary
  terminal rows are retained; active and reconciliation rows are never pruned.
- Coordination: every copy/sync reserves both the per-instance
  `development_source_deployment` mutation and global-heavy lease. Expired work
  is never resumed or recopied: the Step 8 dispatcher releases only exactly
  proven leases, marks the operation reconciliation-required and confidence
  unknown, and exposes a bounded read-only observation. Central reconciliation
  blocking is not bypassed.

## D-0129: The broker owns one narrow semantic copy/sync capability

- Status: approved for Phase 8 Step 4
- Decision: `copy_sync_attached_development_source` accepts only ManagedInstance,
  DevelopmentSource, expected immutable container, and deployment-operation
  UUIDs. The broker independently resolves root/locator/attachment/add-on,
  runtime revision/profile UID/GID, exact stopped container, fixed destination,
  policies, manifests, delta, and staging. It accepts no path, archive, member
  list, destination, ownership, mode, Docker option, shell command, or arbitrary
  payload. Corrupt persistence fails closed. No generic filesystem API is added.

## D-0130: Copy/sync is a strict manual Web mutation

- Status: approved for Phase 8 Step 4
- Decision: authenticated same-origin CSRF-protected
  `POST /api/instances/{instance_id}/development-sources/{source_id}/copy-sync`
  accepts only an empty strict body and returns 202 with operation identity and
  status link. UI eligibility is advisory and limited to attached, disposable,
  exactly stopped/provisioned, non-conflicting instances; POST revalidates all
  authority server-side. Presentation says never deployed, last copy verified,
  verification stale, or verification unknown, never installed/enabled/loaded/
  working/current.

## D-0131: Live Kodi is the add-on inventory source of truth

- Status: approved for Phase 8 Step 5
- Decision: inventory is the complete installed-add-on set returned by live
  Kodi `Addons.GetAddons` for the exact ManagedInstance. Filesystem contents,
  Kodi databases, DevelopmentSource attachments, and copy/sync evidence are not
  inventory sources.
- Semantics: attachment, filesystem materialization, Kodi recognition,
  Kodi-reported enabled state, and functional/working state remain distinct.
  Recognition and enablement do not imply that an add-on works.

## D-0132: Inventory uses one fixed bounded query and strict normalization

- Status: approved for Phase 8 Step 5
- Decision: the application alone issues `Addons.GetAddons` with type
  `unknown`, installed `true`, enabled `all`, and properties name, version, and
  enabled. Required base fields are addonid and type. Callers select neither
  JSON-RPC method, endpoint, filter, nor property.
- Integrity: application identity and every page are live and bounded to three
  seconds per exchange and six seconds total. Pages contain at most 64 items,
  the complete inventory at most 2,048, and each response at most 64 KiB;
  redirects are rejected. Any malformed field, non-Boolean enabled state,
  duplicate ID, inconsistent pagination, unsupported envelope, oversized
  string, or exceeded bound fails the whole observation.

## D-0133: DevelopmentSource attribution is historical copy evidence only

- Status: approved for Phase 8 Step 5
- Decision: `development_source_at_last_verified_copy` is permitted only when
  the recognized add-on ID and exact version match retained successful copy/sync
  evidence for the same source, instance, RuntimeRevision, and current trusted
  container, with confidence `verified_at_last_operation`.
- Wording: this means Kodi recognizes the identity/version Kodi Lab last
  verified copying from that source. It does not prove current destination byte
  equality and is presented as `Prior verified copy`, never installed from,
  current source, or currently verified.

## D-0134: Stale or mismatched evidence cannot attribute current provenance

- Status: approved for Phase 8 Step 5
- Decision: stale/unknown confidence or a source, runtime, container, identity,
  or version mismatch yields provenance `unknown`, even when Kodi recognizes
  the add-on. Safe stale historical wording may be shown separately.
- Exclusions: attachment, matching ID alone, Snapshot restore, fixture
  membership, filesystem location, and repository guesses never establish
  provenance. Existing reset and Snapshot invalidation behavior is unchanged;
  no Snapshot provenance is introduced.

## D-0135: Inventory is a live read-only non-durable surface

- Status: approved for Phase 8 Step 5
- Decision: authenticated inventory is GET-only, `no-store`, queried only when
  the exact provisioned managed container is already running and process-healthy,
  and requires a fresh successful JSON-RPC identity/inventory exchange. It
  never provisions, starts, waits for readiness, restarts, stops, or otherwise
  changes Kodi.
- Persistence and authority: observations are not persisted and do not enter
  activity history, so no migration is added. Unprivileged application code
  uses the existing broker-validated managed endpoint; no inventory broker or
  MCP authority exists. The browser supplies only ManagedInstance UUID.

## D-0136: Python bytecode policy is closed runtime execution semantics

- Status: approved Phase 8 runtime compatibility correction
- Decision: every immutable `RuntimeExecutionProfile` has exactly one required
  `python_bytecode_policy`: `interpreter_default` or `suppress`.
  `interpreter_default` means Kodi Lab does not impose Python bytecode-write
  suppression. `suppress` means Kodi Lab configures the managed Kodi process
  environment with exact `PYTHONDONTWRITEBYTECODE=1` before embedded Python
  initialization.
- Authority: the policy is semantic catalog state, not a generic environment
  map. No browser, application request, broker request, or other caller can
  submit an environment key/value, Python option, bytecode flag, or override.

## D-0137: Bytecode policy changes require new runtime revisions

- Status: approved Phase 8 runtime compatibility correction
- Identity: Python bytecode policy is immutable execution behavior. Changing it
  requires both a new `RuntimeExecutionProfile` and a new monotonically numbered
  `RuntimeRevision`; an existing revision/profile is never edited in place.
  Existing profiles are backfilled as `interpreter_default`. A suppression
  revision may reuse the exact immutable image and build provenance of its
  predecessor because the trusted container configuration supplies the changed
  execution semantics.
- Curated revisions: Kodi 19.5 revision 2
  (`51950000-0000-4000-8000-000000000005`), Kodi 20.5 revision 2
  (`52000000-0000-4000-8000-000000000005`), Kodi 21.3 revision 3
  (`3a000000-0000-4000-8000-000000000006`), and Kodi 22.0 Beta 2 revision 2
  (`52200000-0000-4000-8000-000000000005`) use `suppress`. Their retained
  predecessors continue to mean `interpreter_default`.
- Environment invariant: trusted provisioning composes image-owned environment
  values with the profile-derived bytecode policy deterministically. The
  profile owns the reserved `PYTHONDONTWRITEBYTECODE` key. `suppress` requires
  exactly one `PYTHONDONTWRITEBYTECODE=1`; `interpreter_default` requires the key
  to be absent. Duplicate/conflicting definitions and missing, wrong, injected,
  or otherwise non-profile-derived policy state fail container drift validation.
  Caller-created equivalent environment state is never adopted as authority.
- Historical behavior: normal newest-eligible selection chooses a validated,
  approved suppression revision because its revision number is greater. Existing
  ManagedInstance and Snapshot pins remain on their exact historical revision;
  identical image identity does not create cross-revision restore compatibility.

## D-0138: Step 4 destination provenance remains exact

- Status: approved Phase 8 runtime compatibility correction
- Decision: bytecode compatibility is corrected at managed runtime execution,
  not by changing stopped-only copy/sync. Step 4 gains no `.pyc` exclusion,
  `__pycache__` exemption, runtime-artifact allowlist, automatic adoption of
  post-execution destination additions, or weakened destination readback.
  Arbitrary additions and intentional add-on writes beneath the deployment tree
  remain unexplained destination mutation and replacement conflicts.
- Invariant: suppression prevents normal CPython bytecode generation in a
  DevelopmentSource deployment tree. Pre-existing packaged or system `.pyc`
  outside that tree are neither deleted nor rejected by this policy. Future
  bind-mounted-source benefit is plausible but remains unvalidated and outside
  this correction.

## D-0139: Enablement authority is exact and enable-only

- Status: approved for Phase 8 copied-add-on enablement
- Decision: authority is the exact ManagedInstance, exact attached
  DevelopmentSource, and exact identity/version retained from its successful
  copy. There is no generic add-on-ID authority. The server derives the ID and
  the fixed Kodi mutation always sends literal Boolean `true`.
- Exclusions: disable, toggle, arbitrary `addonid`, and generic
  `Addons.SetAddonEnabled` passthrough are not present.

## D-0140: Enablement requires current bytes and excludes auto-start behavior

- Status: approved for Phase 8 copied-add-on enablement
- Decision: before Kodi mutation, retained Step 4 evidence must be successful,
  `verified_at_last_operation`, and exact for the current revision/container.
  A narrow broker observation derives the fixed destination and repeats Step
  4's complete destination capture; only an exact retained-digest match and
  descriptor identity/version may pass.
- Safety: trusted root `addon.xml` extension metadata rejects `xbmc.service`
  and other explicitly classified immediate-execution points before mutation.
  Ordinary video plugins remain eligible. The broker accepts no path, walk,
  file, digest, archive, or generic filesystem authority, and browser results
  expose no paths or digests.

## D-0141: Dependency declarations and live state are fail-closed

- Status: approved for Phase 8 copied-add-on enablement
- Decision: trusted source `<requires><import>` declarations are normalized to
  ID, exact optional `version`/`minversion` strings, and a strict Boolean
  optional flag, then cross-checked against Kodi's fixed add-on detail result.
  Disagreement is `dependency_state_unknown`. Kodi's fixed dependency detail
  representation serializes an unset range bound as the exact neutral
  `0.0.0` sentinel; the reported string is retained, but only that sentinel is
  treated as an absent declaration bound during cross-checking. A live Omega
  21.3 detail result also repeats a lone declared `version="3.0.0"` as both
  `version` and `minversion`. That exact duplicate is treated as an implicit
  bound; a different second bound or identity remains unknown.
- Preflight: required external dependencies must be recognized and enabled.
  Required missing or disabled dependencies block; optional missing is allowed.
  Kodi Lab never installs or automatically enables dependencies. Closed Kodi
  API/runtime dependency IDs use Kodi's own representation and existing
  inventory where possible; unclassifiable cases remain unknown.

## D-0142: Dependency versions are exact strings, not ordered versions

- Status: approved for Phase 8 copied-add-on enablement
- Decision: Kodi Lab implements neither SemVer nor generic ordering for ordinary
  add-ons and preserves declared/reported strings exactly. The fixed Kodi APIs
  expose declarations and ordinary installed versions but no authoritative
  compatibility Boolean, so ordinary required version constraints remain
  `dependency_version_unverified`, including exact string equality.
- Reviewed system API exception: `xbmc.python` is a Kodi system add-on omitted
  from `Addons.GetAddons`; a direct `Addons.GetAddonDetails` lookup is rejected.
  For the exact supported stable Matrix 19.4, Nexus 20.5, and Omega 21.3
  identities, the reviewed Kodi descriptors establish installed API versions
  3.0.0, 3.0.1, and 3.0.1 respectively, each with ABI floor 3.0.0. A required
  numeric three-component `version` is satisfied only within that inclusive
  ABI-to-installed interval. Unknown releases, malformed versions, explicit
  `minversion`, or contradictory inventory remain blocked. System API enable
  state, type, origin, and repository are not ordinary inventory properties;
  the exact reviewed runtime identity and system descriptor provide the proof.
  Bounded `recognized`/`enabled` status for this case expresses that reviewed
  system API presence, not a JSON-RPC enabled Boolean.
  Sources: Kodi's `addon.xml` dependency contract and the `xbmc.python`
  descriptors at `19.4-Matrix`, `20.5-Nexus`, and `21.3-Omega` in `xbmc/xbmc`.
- States: bounded internal status is present-enabled, missing, disabled,
  optional-missing, version-unverified, or unknown. Kodi Lab does not claim
  generic dependency satisfaction.

## D-0143: Enablement is a synchronous typed coordinated operation

- Status: approved for Phase 8 copied-add-on enablement
- Decision: `DevelopmentSourceEnablementOperation` records running, succeeded,
  failed, or reconciliation-required state and enabled, already-enabled,
  dependency-blocked, rejected, failed, or reconciliation-required result.
  It retains bounded identity/outcome fields only and follows newest-20
  ordinary terminal retention while protecting active/unresolved rows.
- Coordination: per-instance `development_source_enablement` conflicts with
  lifecycle, reset, Snapshot create/restore, copy/sync, attach/detach, instance
  deletion, runtime reassignment, and another enablement. It takes no
  global-heavy lease. Pre-mutation interruption is failure; once mutation may
  have reached Kodi, untrusted completion is reconciliation-required and is
  never retried, toggled, or rewritten to success automatically.
- Eligibility: Python execution-capable sources require the exact current
  suppression profile. Interpreter-default is rejected without changing the
  revision. Safe non-Python sources do not acquire an unnecessary Python rule.

## D-0144: Enablement has one strict authenticated Web action

- Status: approved for Phase 8 copied-add-on enablement
- Decision: authenticated, same-origin, CSRF-protected
  `POST /api/instances/{instance_id}/development-sources/{source_id}/enable`
  accepts a strict empty body and returns the synchronous typed operation with
  HTTP 200. The POST repeats every authority, byte, recognition, runtime, and
  dependency check.
- Presentation: instance DevelopmentSource state distinguishes not copied,
  copied/stopped, not recognized, recognized disabled/enabled, dependency
  blocked, stale/unknown provenance, and unsupported auto-start behavior.
  Enable is advisory and appears only for an apparently eligible disabled
  source. No install, disable, toggle, dependency, network, repository, plugin
  execution, path, digest, or raw Kodi control is exposed.

## D-0145: Copied-add-on smoke authority is a closed first slice

- Status: approved for Phase 8 copied-add-on smoke testing
- Decision: authority exists only for one copied add-on whose trusted descriptor
  has exactly one executable `xbmc.python.pluginsource` extension, exactly
  `video` in `provides`, and a safe relative declared library present in the
  freshly verified destination. Only passive `xbmc.addon.metadata` companions
  are allowed, and Kodi must report the same type for the exact ID/version.
- Exclusions: service, script, binary, PVR, weather, lyrics, visualization,
  screensaver, module-only, auto-start, and mixed executable sets are
  `unsupported_addon_type`. ID prefixes grant no authority. The current exact
  RuntimeRevision/Profile must suppress Python bytecode.

## D-0146: Smoke invocation has exact fail-closed preconditions

- Status: approved for Phase 8 copied-add-on smoke testing
- Decision: every attempt re-proves the disposable instance, attached source,
  retained successful Step 4 copy, `verified_at_last_operation`, exact current
  revision/container, suppression policy, already-running healthy/process-ready
  Kodi, exact recognized and enabled ID/version/type, no blocking
  reconciliation, eligible descriptor, dependency preflight, and a fresh full
  destination match.
- Dependencies: D-0141/D-0142 are rerun from current trusted declarations,
  fixed Kodi details, and complete inventory. Required missing, disabled,
  mismatched/unknown, or version-unverified dependencies block before dispatch;
  optional missing is allowed. Nothing is installed or enabled.
- Lifecycle: the operation never starts, enables, stops, restarts, resets,
  deprovisions, kills, or retries Kodi. Disabled is a typed precondition failure.
  Destination verification accepts only semantic instance/source/container
  identity; callers supply no path or digest.

## D-0147: Smoke execution is one fixed durable directory invocation

- Status: approved for Phase 8 copied-add-on smoke testing
- Decision: the sole adapter constructs `Files.GetDirectory` with the
  server-derived plugin root, media `video`, an empty property list, and limits
  `0..1`. Callers provide no URI, route, query, path, arguments, method, builtin,
  timeout, media, or properties. No generic JSON-RPC execution exists.
- Bounds: the invocation deadline is 10 seconds, preflight retains existing
  bounded timing, and durable ownership has a 90-second hard bound. Work is
  queued with HTTP 202 outside the normal Web request.
- Result: `passed` requires dispatch, a correlated non-error result within the
  deadline, a valid bounded zero-or-one-item directory response, and matching
  post-readback. Zero items is valid. This proves neither playback, network or
  repository success, nor general function, and is never called `Working`.

## D-0148: Post-dispatch provenance controls confidence and ambiguity

- Status: approved for Phase 8 copied-add-on smoke testing
- Decision: every correlated result or Kodi error is followed by exact complete
  destination readback. Match is `matched`; exact mismatch is `changed`;
  unavailable or incomplete verification is `unknown`. Returned plugin items
  are validated in memory and discarded.
- Confidence: match preserves `verified_at_last_operation`; mismatch yields
  `source_tree_changed` and `stale`; unavailable verification yields `unknown`.
  A correlated Kodi error with a matching tree is `execution_failed` while
  provenance remains verified. Add-on-data writes outside the installation
  tree do not themselves fail the operation.
- Ambiguity: timeout or Kodi/container/process loss after dispatch does not
  prove execution ended. History becomes `reconciliation_required`, execution
  is timeout/ambiguous, and confidence becomes unknown regardless of an
  immediate match. No retry or lifecycle automation is allowed. The warning
  blocks conflicting work while the exact original container runs. Explicit
  ordinary stop is allowed; once it is stopped or absent the warning may be
  nonblocking, but history is never rewritten and no mark-resolved action exists.

## D-0149: Smoke history is typed, durable, and per-instance coordinated

- Status: approved for Phase 8 copied-add-on smoke testing
- Decision: `DevelopmentSourceSmokeTestOperation` separately records operation
  state, closed phase, overall result, execution result, and post provenance,
  plus bounded identity, duration, count, dependency classification, timestamps,
  and immutable original preconditions. It stores no items, labels, URLs, paths,
  plugin URI, raw JSON, logs, digests, or raw Kodi error text.
- Coordination: `development_source_smoke_test` conflicts with lifecycle, reset,
  Snapshot create/restore, fixture/runtime reassignment, copy/sync,
  attach/detach, enablement, deletion, and another smoke. Snapshot deletion is
  unrelated and there is no global-heavy lease. Abandoned post-dispatch work is
  retained reconciliation and is never resumed or converted to success.
- Retention/activity: retain the latest 20 ordinary terminal operations per
  original instance plus all active and reconciliation-required rows. Activity
  exposes bounded historical summaries, never current working state.

## D-0150: Smoke Web authority and presentation remain narrow

- Status: approved for Phase 8 copied-add-on smoke testing
- Decision: authenticated same-origin CSRF-protected
  `POST /api/instances/{instance_id}/development-sources/{source_id}/smoke-test`
  accepts a strict empty body and returns HTTP 202 with bounded operation/status
  URLs. Authenticated status exposes no plugin-returned content or Kodi controls.
- Presentation: the advisory action appears only when eligibility seems met;
  POST revalidates all authority. Results are Smoke passed, Execution failed,
  Timed out, Dependency blocked, Source tree changed, Unsupported add-on type,
  or Reconciliation required—never Working, Healthy, or Fully tested.
- Network: the action warns that the managed network is not egress-isolated,
  execution inherits existing access, and plugin code may contact external
  services. This slice adds no network toggle, namespace, firewall, repository
  control, or isolation claim.

## D-0160: Uploaded ZIP is a separate workflow

- Status: approved for the Phase 8 uploaded-ZIP first slice
- Decision: only origin `uploaded_zip` is supported. ZIP artifacts are not
  DevelopmentSources. ID, name, version, logical label, multipart filename,
  and payload never imply Git, DevelopmentSource, repository, publisher, or
  trust origin.

## D-0161: ZIP format and limits are closed

- Status: approved
- Bounds: compressed ZIP 256 MiB; total uncompressed 1 GiB; regular files
  25,000; all entries 50,000; one file 256 MiB; normalized path 512 UTF-8
  bytes; component 255 bytes; depth 32; `addon.xml` 1 MiB; central directory
  16 MiB; canonical manifest 16 MiB.
- Ratio protection: each member may expand to at most the greater of 1 MiB or
  200 times compressed size; the archive may expand to at most the greater of
  4 MiB or 100 times compressed size.
- Parser: bounded EOCD and central-directory validation precedes member
  processing. Only stored and deflate are accepted. Multi-disk, ZIP64,
  encryption, data descriptors, comments, unsupported compression or extra
  fields, absolute/traversal/backslash/control/non-NFC/overlong names,
  duplicate normalized names, case collisions, links and special Unix types,
  inconsistent headers, overlap, invalid offsets, CRC/length mismatch are
  rejected. Nested archives are opaque files and are never recursed.

## D-0162: Accepted ZIP artifacts are immutable private records

- Status: approved
- Decision: `AddonZipArtifact` stores a UUID, bounded sanitized logical label,
  SHA-256 and byte/member aggregates, parsed ID/name/version/type/dependencies,
  canonical payload-manifest digest, import timestamp, `uploaded_zip` origin,
  and accepted validation state. Browser filenames are neither retained nor
  presented.
- Storage: request bytes stream to a UUID-only 0600 O_EXCL no-follow stage with
  an enforced compressed ceiling and fsync. Validation publishes the original,
  manifest, and normalized payload by same-filesystem atomic rename under the
  private data root. Publication and later install both verify digests. Failed
  stages are removed only by server UUID. Accepted artifacts have no deletion,
  download, browse, or member-list surface and are retained pending a later
  explicit lifecycle policy.

## D-0163: Layout and descriptor identity are exact

- Status: approved
- Decision: one logical top-level directory is required, its name equals the
  parsed add-on ID, and `addon.xml` is directly beneath it; an explicit root
  directory entry is optional and every extra root entry is forbidden. `.git`,
  `.hg`, and `.svn` components are forbidden case-insensitively. Repository
  descriptors are rejected.
- Type: the first slice accepts only a non-repository, non-service Python video
  plugin with one `xbmc.python.pluginsource`, exact `video` provides, a present
  safe relative library, and optional passive metadata. Service/auto-start and
  mixed executable extensions are rejected. Installation requires a
  suppression RuntimeRevision/Profile.

## D-0164: Installation is Lab-controlled materialization

- Status: approved
- Decision: ZIP installation means materializing the already-validated private
  normalized payload at the managed add-on destination under Lab authority. It
  is not Kodi's native Install-from-ZIP GUI/installer path. `InstallAddon`,
  `InstallFromZip`, repositories, generic builtins/JSON-RPC, and GUI automation
  are absent.
- Normalization: established Step 4 file/directory modes apply; runtime UID/GID
  comes only from immutable RuntimeExecutionProfile. ZIP ownership, timestamps,
  ACLs, xattrs, and special bits are discarded.

## D-0165: The operation owns running-instance choreography

- Status: approved
- Decision: a healthy already-running disposable instance is required so live
  inventory can exclude bundled/system/same-ID add-ons. One coordinated
  operation proves runtime, suppression, inventory absence and dependency
  baseline; stops the exact container; reproves stopped identity and destination
  absence; applies and reads back; restarts that exact container; waits normal
  readiness; requires exact disabled recognition and unchanged dependencies;
  repeats exact readback; and leaves the instance running. There is no user
  stop/install/user start model, enablement, or smoke execution.

## D-0166: Installation is absent-only

- Status: approved
- Decision: both complete live inventory and the fixed destination must show
  the ID absent before stop/application, with destination absence reproved while
  stopped. Any prior ZIP, DevelopmentSource, future bind, repository/system,
  manual, unknown, or partial same-ID state blocks. There is no replacement,
  upgrade, overwrite, override, retry application, or idempotent byte reapply.

## D-0167: ZIP evidence and exact readback are distinct

- Status: approved
- Decision: `AddonZipInstallationOperation` and current
  `AddonZipInstallationEvidence` bind artifact, live/original instance,
  RuntimeRevision, exact container, ID/version, manifest identity, timestamp,
  confidence, and `installed_recognized_disabled`. DevelopmentSource evidence
  is never reused.
- Verification: the canonical normalized manifest must match every destination
  path, file digest/bytes, type, normalized mode and runtime UID/GID with no
  missing or unexpected entry, once stopped and again after recognition.
  Recognition must be exact ID/version/`xbmc.python.pluginsource` and
  `enabled=false`; unexpected enablement is ambiguity, never success.

## D-0168: Dependencies and repositories are not mutated

- Status: approved
- Decision: missing required, disabled required, and optional-missing
  dependencies do not block disabled materialization. For artifact-declared
  dependencies only, bounded pre/post presence, enablement, and version state
  must remain identical. Installation never looks up, installs, enables, or
  infers satisfaction of a dependency. ZIP enablement is separately gated.

## D-0169: Import and install have durable coordination

- Status: approved
- Decision: import/validation is an asynchronous artifact-global operation with
  a global-heavy lease. Installation is asynchronous with both the per-instance
  `addon_zip_installation` mutation lease and global-heavy lease. Its closed
  phases cover queue, preflight, stop, apply, stopped verification, restart,
  readiness, recognition, final verification, and terminalization.
- Conflicts: installation excludes lifecycle, reset, Snapshot create/restore,
  runtime/fixture changes, DevelopmentSource attachment/copy/enable/smoke,
  another ZIP installation, and instance deletion.
- Failure: known pre-application failure is ordinary and restores a container
  stopped by this operation only when exact restoration is deterministic. Any
  uncertainty after the durable application marker leaves the exact instance
  stopped, history reconciliation-required, current evidence unknown, and
  performs no retry, adoption, deletion, overwrite, or automatic restart.
  Read-only reconciliation classifies absence, exact/partial payload, expected
  or unexpected recognition, container/revision drift, and dependency drift;
  it never offers manual success.

## D-0170: Snapshot, reset, and provenance remain conservative

- Status: approved
- Decision: active install conflicts with Snapshot create/restore. Later
  Snapshots may capture bytes but carry no ZIP lineage; restore stales current
  ZIP evidence and never reassociates restored bytes. Reset invalidates current
  evidence before recreation while artifact and typed history survive, with no
  automatic reinstall.
- Inventory: `Prior verified ZIP install` requires exact retained successful
  evidence for ID/version, instance, revision, and current container. It is
  historical provenance only and never claims current byte equality; every
  mismatch, stale/unknown evidence, or merely uploaded artifact is unknown.

## D-0171: Broker and Web authority are narrow

- Status: approved
- Broker: callers provide only instance UUID, artifact UUID, server operation
  UUID, and exact-container precondition. The broker resolves artifact,
  normalized payload, destination, ownership, and container. Paths, bytes,
  UID/GID, mode, flags, command, URL, and Docker options are unrepresentable.
- Web: authenticated same-origin CSRF-protected bounded multipart
  `POST /api/addon-zips` accepts exactly one ZIP plus logical label, ignores its
  filename, and returns HTTP 202 import status. Strict-empty-body
  `POST /api/instances/{instance_id}/addon-zips/{artifact_id}/install` returns
  HTTP 202 operation status. UI/API expose only safe label/identity/aggregate,
  origin, validation, and operation state—not filename, members, paths,
  contents, XML, or full digests.

## D-0172: Replacement and execution remain deferred

- Status: approved
- Decision: ZIP replacement, upgrade, rollback, and ZIP smoke remain deferred;
  ZIP enablement was separately approved by D-0173 through D-0178.
  DevelopmentSource packaging, repository integration, and successful-artifact
  deletion/cleanup surfaces are explicit future gates. Raw direct bind remains
  rejected by the completed bind gate.

## D-0173: Uploaded-ZIP enablement has artifact-and-installation authority only

- Status: approved and implemented for the Phase 8 ZIP-enable first slice
- Decision: authority is exactly ManagedInstance UUID plus accepted
  AddonZipArtifact UUID. The server resolves immutable artifact, successful ZIP
  installation evidence and operation, exact ID/version/type, normalized
  payload, RuntimeRevision/Profile, current container, and fixed destination.
  DevelopmentSource authority is never accepted or inferred.
- Scope: enable only. There is no disable, toggle, caller add-on ID, generic
  enablement, repository action, dependency installation, or ZIP smoke. Kodi
  must already be running, healthy, freshly ready, and suppression-profiled;
  enablement performs no lifecycle mutation.

## D-0174: ZIP enablement requires fresh immutable-payload verification

- Status: approved and implemented
- Decision: before Kodi mutation, a ZIP-enable-only broker verifier resolves
  artifact, successful installation lineage/evidence, immutable normalized
  payload, destination, revision/container, and profile ownership from semantic
  UUIDs and the exact-container precondition. No generic path, digest, hash, or
  filesystem observation authority exists.
- Outcomes: complete byte/digest/path/type/mode/UID/GID equality with no missing
  or unexpected entry is `matched`. Exact change is `changed` and makes only
  that artifact evidence stale. Unavailable/unclassifiable verification is
  `unknown` and makes it unknown. Both block with no adoption or retry.

## D-0175: ZIP dependency preflight reuses only pure evaluation machinery

- Status: approved and implemented
- Decision: immutable artifact declarations are cross-checked with fixed Kodi
  details, then evaluated against complete inventory by the smallest pure
  comparison/status machinery also used by DevelopmentSource enablement. ZIP
  application authority, evidence, orchestration, and history remain separate.
- Result: required missing, disabled, mismatched, unclassifiable, or actually
  version-constrained dependencies block before enable; optional missing is
  allowed. Constraints are `dependency_version_unverified`. Kodi Lab never
  installs/enables a dependency, queries a repository, or infers compatibility.

## D-0176: ZIP enablement is fixed enable-true plus exact readback

- Status: approved and implemented
- Decision: the established adapter constructs only `Addons.SetAddonEnabled`
  with server-derived ID and literal Boolean `true`. `result: OK` is
  insufficient; a fresh query must prove exact ID/version/type and enabled true.
- Idempotence/ambiguity: already enabled succeeds only after every precondition,
  returns `already_enabled` and `enable_performed=false`, and sends no mutation.
  Possible post-dispatch mutation without trustworthy final state becomes
  reconciliation-required and makes target ZIP evidence unknown. There is no
  retry, toggle, rollback disable, or historical success rewrite.

## D-0177: ZIP enablement has separate durable history and coordination

- Status: approved and implemented
- Decision: `AddonZipEnablementOperation` stores bounded semantic identity,
  immutable original and nullable live instance, artifact/installation refs,
  ID/version snapshots, RuntimeRevision/container precondition, state/phase,
  payload outcome, prior/result enabled Booleans, enable-performed marker,
  dependency/failure classes, and timestamps. It stores no Web paths/digests,
  raw JSON-RPC/errors, repository URLs, or authoritative current enabled state.
- Coordination: `addon_zip_enablement` conflicts with lifecycle, reset, Snapshot
  create/restore, fixture/runtime reassignment, DevelopmentSource
  attach/detach/copy/enable/smoke, ZIP install/enable, future ZIP smoke, and
  deletion. Snapshot deletion and unrelated import do not conflict; no global
  lease is used. Retain newest 20 ordinary terminal rows plus all active and
  reconciliation-required history.
- Invalidation: existing Snapshot/reset/deprovision/runtime/container rules
  invalidate ZIP installation evidence; history never restores authority.
  Ordinary same-container stop/start does not itself invalidate lineage.

## D-0178: ZIP-enable Web and presentation authority is strict and private

- Status: approved and implemented
- Decision: authenticated same-origin CSRF-protected
  `POST /api/instances/{instance_id}/addon-zips/{artifact_id}/enable` accepts a
  strict empty body and synchronously returns HTTP 200 typed status. It accepts
  no add-on ID, mutation flag, dependency option, path, digest, or Kodi input.
- Presentation: ZIP-install provenance remains `Prior verified ZIP install`;
  live enabled Boolean and bounded disabled/enabled, stale, unknown,
  dependency-blocked, or reconciliation state are separate. Safe activity never
  exposes operational detail or says Verified ZIP enabled, Working, Tested, or
  Healthy.

## D-0179: ZIP smoke authority is artifact-and-installation specific

- Status: approved and implemented
- Decision: authority is exactly ManagedInstance UUID plus accepted immutable
  AddonZipArtifact UUID. The server resolves successful verified installation
  evidence, normalized payload, exact ID/version/type, RuntimeRevision/Profile,
  current container, and fixed destination. There is no smoke-by-add-on-ID or
  caller invocation authority.
- Preconditions: the disposable suppression-profile instance must already be
  running, healthy, freshly ready, exactly recognized and enabled, with no
  blocking reconciliation. Immutable dependency declarations must cross-check
  Kodi details and pass complete live inventory evaluation. Exact installed-tree
  readback must match before dispatch; changed makes evidence stale, unknown
  makes it unknown, and neither is adopted or retried.
- Invocation: the existing fixed adapter performs only `Files.GetDirectory` for
  server-derived `plugin://<addon-id>/`, `media=video`, no properties, result
  range 0..1, and a ten-second timeout. No lifecycle, enablement, dependency, or
  retry mutation is automatic.

## D-0180: ZIP smoke results preserve execution and tree provenance separately

- Status: approved and implemented
- Decision: durable operation state, overall result, execution result, and
  post-execution provenance remain distinct. `passed` requires correlated
  bounded completion plus exact post-readback; zero items is valid. A correlated
  Kodi error with matched tree is `execution_failed` and preserves verified ZIP
  evidence. A changed tree is `installed_tree_changed` and makes evidence stale;
  an unavailable/incomplete verifier makes evidence unknown and cannot pass.
- Ambiguity: timeout or crash after dispatch is permanently
  reconciliation-required with timeout/ambiguous execution and unknown evidence.
  Ping/readiness or an immediate matching tree cannot restore authority. There
  is no automatic retry/restart/kill; explicit ordinary stop contains execution.

## D-0181: ZIP smoke has separate history, coordination, and private Web surface

- Status: approved and implemented
- Decision: `AddonZipSmokeTestOperation` stores only bounded semantic identity,
  artifact and installation references, nullable live plus immutable original
  instance, revision/container precondition, typed results, duration, item count,
  dependency classification, and timestamps. It stores no directory content,
  plugin URI, paths, raw JSON/errors, logs, or container identity in Web output.
- Coordination: per-instance `addon_zip_smoke_test` conflicts with lifecycle,
  reset, Snapshot create/restore, fixture/runtime reassignment, DevelopmentSource
  mutations, ZIP install/enable/smoke, and deletion. Snapshot deletion remains
  unrelated and no global-heavy lease is used. Retention keeps 20 ordinary
  terminal rows and never prunes active or reconciliation-required rows.
- Web: authenticated same-origin CSRF-protected strict-empty-body
  `POST /api/instances/{instance_id}/addon-zips/{artifact_id}/smoke-test` returns
  HTTP 202 plus bounded status. UI eligibility is advisory and warns: “Plugin
  execution may contact external services; the managed runtime network is not
  egress-isolated.”

## D-0182: V1 local development uses copy/sync; raw bind removed from V1

- Status: approved v1 scope change
- Decision: the supported v1 DevelopmentSource materialization workflow is a
  manual explicit copy/sync from an attached registered source. It transfers
  changed and new entries only, performs no deletion or automatic Kodi restart,
  and retains exact provenance and replacement protection. Existing bounded
  inventory, enablement, and smoke workflows remain separate optional steps.
- Security evidence: raw direct Docker bind of mutable DevelopmentSource host
  paths is rejected. The architecture gate demonstrated source-root path
  re-resolution to outside-root content, late hardlink exposure of outside-root
  files, late Unix-socket boundary crossing, VCS metadata exposure to Kodi and
  Snapshot capture, and loss of writable-layer state during container
  recreation. Read-only mounting prevents container writes but does not contain
  host-side namespace mutation.
- Scope: this explicitly changes the original bind-first v1 requirement; it
  does not reinterpret copy/sync as having satisfied that requirement all
  along. A policy-enforcing containment-safe live projection or equivalent
  mechanism remains optional post-v1 work. Raw direct bind is not an approved
  Kodi Lab mechanism.

## D-0183: Repository sources are global but use is explicitly attached

- Status: approved and implemented for the Phase 8 repository first slice
- Decision: `AddonRepositorySource` persists an opaque UUID, unique logical
  name, canonical index URL, closed `external_https_repository` origin, and
  creation time. `ManagedInstanceAddonRepositorySource` is the only
  per-instance authority. Registration never implies attachment or global use.

## D-0184: Repository URL authority is a closed HTTPS index endpoint

- Status: approved and implemented
- Decision: only DNS-host HTTPS on normalized default port 443 ending exactly
  `/addons.xml` or `/addons.xml.gz` is accepted. Hostnames are lowercase IDNA;
  paths have safe components and a 2 KiB bound. IP literals, userinfo,
  query/fragment, controls, traversal, encoded separators, non-default ports,
  and every other scheme are rejected. Package identity is server-derived.

## D-0185: DNS, connection, redirect, TLS, and proxy policy are pinned

- Status: approved and implemented
- Decision: an explicit global-unicast predicate rejects private, loopback,
  CGNAT, link-local, multicast, unspecified, reserved, documentation,
  benchmarking, Docker/internal, metadata, IPv6 ULA/site-local, and mapped
  private addresses. DNS has a three-second deadline, at most eight answers,
  and rejects any mixed set. Each connection uses one exact validated address
  without socket-time re-resolution while retaining the registered hostname for
  TLS SNI, certificate verification, and Host. Up to three HTTPS, same-host,
  port-443 redirects re-resolve and revalidate. Ambient proxy variables are
  never consulted. System CAs and normal hostname verification are mandatory;
  credentials, headers, cookies, custom CAs, client certificates, and insecure
  TLS are unrepresentable.

## D-0186: Explicit refresh publishes immutable bounded metadata

- Status: approved and implemented
- Decision: only direct XML or one explicit gzip layer is parsed. Transfer,
  expansion, ratio, headers, candidates, dependencies, XML depth, text, and
  bounded-string limits are fixed; DTD/entities, HTML, ambiguous content types,
  repository discovery, datadir/checksum/package orchestration, and background
  polling are rejected. Each success publishes an immutable snapshot plus
  normalized candidates; no raw XML or package URL enters normal metadata.
  Unsupported candidates are display-only. Installable candidates are exactly
  passive non-service Python video plugins supported by uploaded-ZIP validation.

## D-0187: Candidate UUID derives one same-origin immutable ZIP artifact

- Status: approved and implemented
- Decision: package location is always
  `<registered-index-directory>/<id>/<id>-<version>.zip`; metadata cannot
  override it. Fetch repeats the pinned HTTPS policy, bounded transfer, and the
  existing strict ZIP validator. Descriptor ID, version, type, and dependencies
  must exactly match. Publication is all-or-nothing as `repository_download`
  with exact repository/snapshot/candidate/fetch lineage and private package
  identity. Refresh and fetch are durable global-heavy typed operations.

## D-0188: Repository install reuses materialization, never Kodi resolution

- Status: approved and implemented
- Decision: repository installation has separate instance-plus-repository-
  artifact authority but reuses the validated ZIP materializer. Kodi repository
  registration, GUI, `InstallAddon`, `InstallFromZip`, native dependency
  resolution, and dependency mutation are forbidden. Missing or disabled
  dependencies permit disabled installation and remain unchanged.

## D-0189: Repository installation is absent-only and protects attachments

- Status: approved and implemented
- Decision: exact attached repository lineage, no reconciliation, complete live
  inventory absence, destination absence before stop and after stop, and no
  same-ID attached DevelopmentSource are mandatory. An attached same-ID source
  blocks before lifecycle mutation even when Kodi and the destination are
  absent; install conflicts with source and repository attachment changes.
  Copied, manual, inventory-only, destination-only, and bundled/system
  collisions block through the shared absent-only checks. Any retained prior
  verified ZIP or repository installation evidence also blocks even if live
  inventory and destination observations are absent.
  `protected_development_source_collision` is the safe typed result.

## D-0190: Repository provenance is exact historical lineage

- Status: approved and implemented
- Decision: verified evidence carries artifact origin plus exact repository,
  snapshot, and candidate lineage. Inventory may say `Prior verified repository
  install — <logical repository name>` only when instance, revision, container,
  ID/version, operation, artifact, and lineage all agree. This is historical
  provenance, not publisher trust, byte equality, enablement, or working state.
  Snapshot restore and reset stale evidence without removing sources,
  attachments, snapshots, artifacts, or history; restored bytes are not adopted.

## D-0191: Repository work has typed coordination and fail-closed recovery

- Status: approved and implemented
- Decision: refresh and fetch use repository-specific global-heavy leases;
  installation uses repository-specific global-heavy plus per-instance mutation
  leases. Installation conflicts with lifecycle, reset, Snapshot create/restore,
  runtime/fixture changes, DevelopmentSource mutations, ZIP mutations,
  repository installation, and deletion. Pre-application network/metadata/ZIP
  failures are ordinary. After application begins, existing ZIP fail-closed
  reconciliation leaves the exact instance stopped, evidence unknown, and
  performs no retry, adoption, delete, overwrite, or automatic restart.

## D-0192: Replacement and update approval is intentionally deferred

- Status: superseded by D-0193 through D-0201
- Decision: there is no force Boolean, override flag, reinstall, update,
  replacement, or ignore-collision path. A later gate must bind one explicit
  approval to exact incumbent and incoming state before any replacement work.

## D-0193: Repository replacement has exactly two eligible incumbent classes

- Status: approved and implemented
- Decision: the first slice accepts only a `repository_download` artifact for
  the same ID as a currently attached DevelopmentSource on a disposable
  instance. Class A is attached but unmaterialized: no deployment evidence,
  destination, or live inventory target. Class B is a verified copied source:
  its retained successful deployment, current full destination equality, and
  exact live ID/version/type/enabled state must all agree.
- Exclusions: detached source destinations, uploaded-ZIP or prior repository
  installs, manual/unknown, inventory-only, destination-only, bundled/system,
  stale, and unknown incumbents remain ineligible. Same/lower/higher version
  strings are not ordered; an exact incoming payload no-op is rejected.

## D-0194: One exact plan receives one immediate short-lived approval

- Status: approved and implemented
- Decision: `RepositoryReplacementPlan` persists an opaque approval UUID and
  binds immutable original/live instance, repository source/attachment/
  snapshot/candidate/artifact lineage, add-on and incumbent class, exact source
  attachment and copied-deployment evidence when applicable, incumbent state,
  RuntimeRevision/container, qualifying central Snapshot, incoming payload,
  timestamps, state, bound execution UUID, and terminal outcome.
- Lifetime: plans expire after a fixed 15 minutes and have closed pending,
  approved, expired, invalidated, consumed, and cancelled states. Approval and
  creation of its sole queued operation occur atomically. It is consumed
  immediately before application and cannot be reactivated or reused. There is
  no force Boolean or reusable override token.

## D-0195: Replacement retains the attachment and derives active override

- Status: approved and implemented
- Decision: success never detaches or modifies the DevelopmentSource. Current
  destination provenance becomes the verified repository replacement while the
  same-ID source remains attached. “Attached DevelopmentSource is currently
  overridden” is derived only from that attachment plus exact current verified
  repository replacement evidence; no generic override-state table exists.

## D-0196: Replacement is overwrite-and-add only

- Status: approved and implemented
- Decision: Class B is eligible only when every incumbent path exists in the
  normalized incoming manifest with the same file/directory class. Incoming
  paths may be added; incumbent-only paths or type transitions are rejected
  before application. Class A materializes into an absent destination.
- Authority: the dedicated broker request contains only instance, approval,
  operation, and expected-container identities. The broker derives payload,
  destination, ownership, modes, and manifests. It has no delete, arbitrary
  path, generic execution, Docker argument, user/group, or overwrite control.

## D-0197: Class B preserves enabled and declared dependency state

- Status: approved and implemented
- Decision: exact incumbent enabled Boolean and relevant declared dependency
  presence/enabled/version observations are captured before stop and must match
  after readiness. Class A retains disabled-recognition repository semantics.
  No dependency or add-on-data/profile mutation is authorized.

## D-0198: Class B requires a recent central Snapshot

- Status: approved and implemented
- Decision: planning and execution require an available same-instance,
  same-RuntimeRevision central Snapshot captured no more than 15 minutes before
  the plan and still available at execution. Class A requires none. There is no
  automatic rollback or subtree backup; restoring the bound Snapshot is the
  supported prior-state exit, and restored source bytes do not regain verified
  provenance automatically.
- Capture accepts Docker's unordered archive enumeration only after two bounded
  passes agree on the same approved inventory. Approved regular files are
  privately spooled, then emitted in canonical sorted order; unsafe objects,
  duplicate/drifted members, bounds violations, and insufficient two-copy disk
  reserve still fail closed.

## D-0199: Replacement uses explicit evidence transitions

- Status: approved and implemented
- Decision: at the durable application marker, copied-source evidence becomes
  unknown. Proven success publishes verified-at-last-operation repository
  evidence and makes old source evidence stale. Any post-marker ambiguity makes
  both evidence classes unknown and requires reconciliation. Snapshot restore
  makes repository evidence stale and leaves source evidence stale; it does not
  attribute restored bytes. Inventory shows only qualified current repository
  provenance plus the separate retained-attachment override notice.

## D-0200: Replacement has dedicated coordination and reconciliation

- Status: approved and implemented
- Decision: `repository_replacement` owns both per-instance and global-heavy
  leases and therefore conflicts with lifecycle, reset, Snapshot create/restore,
  attachment and deployment work, repository work, ZIP work, reassignment, and
  deletion. Deleting a bound Snapshot invalidates a pending plan.
- Boundary: pre-marker failure permanently invalidates the approval and, after
  a stop, deterministically restores the running state. Consumption, application
  marker, and source-evidence unknown transition are one transaction immediately
  before first modification. Any later uncertainty leaves the exact instance
  stopped, both evidence classes unknown, and a reconciliation-required record;
  there is no retry, adoption, overwrite continuation, restart, or auto-restore.

## D-0201: Replacement Web authority and presentation are private and closed

- Status: approved and implemented
- Decision: authenticated same-origin CSRF-protected empty-body plan preparation
  and immediate `{"confirmed": true}` approve-and-execute are the only Web
  mutations. All extra and caller-selected identity, path, version, Snapshot,
  container, force, overwrite, and delete fields are rejected.
- Presentation: review shows only logical repository/source names, add-on
  identity, incumbent/incoming versions, class, enabled state, Snapshot
  readiness, and bounded effect/rollback wording. Status and history expose no
  paths, manifests, digests, package URLs, container identities, raw errors, or
  internal Snapshot locations. Copy/sync returns `repository_override_active`
  before destination mutation. Supported exits are central Snapshot restore or
  disposable reset followed by fresh copy/sync; detach alone does not restore
  bytes.

## D-0202: Phase 9 V1 workspaces are plain copies of registered local sources

- Status: approved and implemented for Phase 9 Slice 1
- Decision: one workspace originates only from one registered
  `local_directory` DevelopmentSource and is a normalized Lab-owned safe copy.
  It is not a Git clone/worktree/repository, mount, bind, alias, uploaded ZIP,
  repository-download artifact, or arbitrary host directory. A source inside a
  Git checkout is still treated only as an ordinary tree. No Git command,
  configuration, hook, remote, submodule, LFS, branch, commit, tag, fetch,
  pull, or push authority exists. Existing safe-capture policy excludes
  `.git`, `.hg`, and `.svn` administration directories.

## D-0203: Workspaces have durable UUID identity and private application storage

- Status: approved and implemented for Phase 9 Slice 1
- Decision: `AgentWorkspace` retains workspace/source UUIDs, immutable source
  add-on ID and source name/version snapshots, closed lifecycle state,
  independent baseline artifact UUID and manifest identity, generation, current
  tree identity, and capture/create/update timestamps. No host path is stored.
- Lifecycle and storage: Slice 1 uses only `creating`, `ready`, `abandoned`, and
  `reconciliation_required`; generation starts at zero. Bytes live only below
  `KODI_LAB_DATA_ROOT/agent-workspaces/<workspace-uuid>/` as private baseline,
  mutable `tree/`, and creation staging. Paths are server-derived from UUIDs.
  Multiple independent workspaces per source are allowed. Ordinary non-root
  application code owns creation and reads; broker, Docker, sudo, root, Kodi,
  Git, and an implicit current-workspace pointer are absent.

## D-0204: Creation publishes one immutable baseline and its equal generation-0 tree

- Status: approved and implemented for Phase 9 Slice 1
- Decision: creation reuses the Phase 8 bounded descriptor-relative no-follow
  capture and its canonical constants: 25,000 files, 50,000 entries, 1 GiB
  total, 256 MiB per file, 512-byte paths, 255-byte components, depth 32, and a
  16 MiB manifest. It rejects links, hardlinks, special files, unsafe names and
  VCS administration directories, validates captured root `addon.xml` identity,
  and requires a matching second source pass.
- Publication: a private normalized capture becomes the retained baseline. The
  mutable generation-0 tree is copied only from that capture and both trees are
  recaptured and required to match its exact topology, bytes, and normalized
  modes before `ready`. A typed global-heavy creation operation records bounded
  phase/failure history. Proven pre-publication failure removes exact private
  partial state and abandons the catalog row; uncertainty after baseline
  publication retains bytes and becomes `reconciliation_required`.
- Read-only Slice 1 authority: directory inspection returns at most 2,000 safe
  name/type/optional-size entries. File reads return only strict UTF-8 text and
  are capped at 2 MiB. Semantic relative paths reject absolute, empty-file,
  dot, parent, backslash, NUL/control, component, path, and depth violations;
  traversal is descriptor-relative and no-follow. Host paths, ownership,
  inodes, timestamps, symlink targets, manifests, digests, and baseline bytes
  are not exposed.

## D-0205: Workspace mutation authority is bounded, exact, and generation-bound

- Status: approved and implemented for Phase 9 Slice 2
- Authority: the application accepts only AgentWorkspace UUID, expected current
  generation, normalized workspace-relative file path, and bounded UTF-8 text.
  Ready workspaces alone may mutate. A server-derived per-workspace advisory
  lock serializes edits across application processes; a conditional database
  update publishes exactly generation plus one. Independent workspaces do not
  share this filesystem lock.
- Operations: create requires an absent target beneath an existing safe
  directory. Replace requires an existing single-link regular UTF-8 file. Exact
  patch is a full-file expected-text preimage plus replacement text; mismatch
  fails with no fuzzy search, offset interpretation, merge, or partial apply.
  Each text field and resulting file is at most 2 MiB, and mutation JSON is
  capped at 16 MiB. Create mode is 0644; replace and patch preserve the current
  normalized regular-file mode. Callers control no ownership, mode, timestamp,
  ACL, xattr, temporary path, or metadata.
- Integrity and durability: before mutation, a full bounded no-follow recapture
  must equal durable current-tree identity and rejects VCS administration
  directories. Complete content is fsynced in a private same-directory file.
  Create uses atomic Linux `renameat2(RENAME_NOREPLACE)`; replace/patch use
  atomic rename. Afterward, a second full recapture supplies the new identity.
  A private exact preimage link permits deterministic one-target rollback if
  recapture or database publication fails. Unproven rollback or remaining tree
  disagreement changes the workspace to `reconciliation_required`; unknown
  bytes are never adopted.
- Diff and history: change listing freshly validates immutable baseline and
  durable current tree, reports added/modified files only, and provides bounded
  16 KiB UTF-8 before/after previews. Missing baseline entries, type changes,
  changed binary files, and external drift fail closed. Root `addon.xml` may
  retain a valid changed ID, but the immutable source ID remains unchanged and
  the workspace is classified `identity_changed` for later promotion blocking.
  Migration `0030_agent_workspace_mutations` stores only event UUID, workspace
  UUID, generation, create/replace/patch kind, relative path, and time in the
  same transaction as generation publication; it stores no content or digest.

## D-0206: Initial workspace mutation has no delete, rename, or type transition

- Status: approved and implemented for Phase 9 Slice 2
- Decision: there is no file/directory delete, rename, move, mkdir, cleanup,
  chmod, ownership, binary-write, type-transition, shell, Git, broker, Docker,
  Kodi, DevelopmentSource mutation, promotion, or MCP authority. Exact patch may
  produce an empty regular file but cannot remove it. File creation is limited
  to already existing safe directories. Recovery-only removal of an exactly
  proven just-created postimage and restoration of a private exact preimage are
  internal atomicity mechanisms, not caller operations.

## D-0207: Canonical source promotion is an exact reviewed three-way operation

- Status: approved and implemented for Phase 9 Slice 3
- Eligibility: only added and modified UTF-8 single-link regular files are
  promotable. Delete, rename, directory creation, binary mutation, object-type
  transition, mode-only transition, and whole-tree replacement are rejected.
- Decision: a private 15-minute plan compares immutable workspace baseline,
  fresh complete DevelopmentSource capture, and the exact current workspace
  generation/tree. A modification is clean only while source type, bytes, and
  normalized executable identity equal baseline. An addition is clean only
  while absent beneath existing safe directories. Unrelated safe source adds,
  modifications, and deletions do not conflict; any unsafe source object does.
  Root `addon.xml` may change valid metadata but the effective promoted add-on
  ID must equal immutable registered identity. There is no XML merge.
- Review and approval: at most 100 files and 4 MiB total complete before/after
  UTF-8 text are persisted privately and returned within an 8 MiB encoded
  envelope. Truncation can never authorize promotion. The strict administrator
  flow is empty-body plan creation followed by immediate
  `{"confirmed": true}` approval. Approval binds exactly one target and one
  source operation and is permanently single-use once application starts.

## D-0208: Promotion source writes use unprivileged exact filesystem authority

- Status: approved and implemented for Phase 9 Slice 3
- Permission: registration never changes ownership or mode. Planning classifies
  a readable source separately from one actually writable by the effective
  application account, including every affected existing parent. Missing
  provisioning fails closed. Promotion uses no broker/root helper, Git, shell,
  Docker, or Kodi operation.
- TCB clarification: any DevelopmentSource directory provisioned writable for
  promotion is part of the Kodi Lab application trusted-computing base.
  Registration alone is read trust; administrators separately provision the OS
  permissions, and Kodi Lab never chmods or chowns the source. Compromise of the
  application service account could modify every promotion-enabled source it
  can write, so ownership/ACLs must be least-privilege and intentionally scoped.
- Backup and apply: before first canonical mutation, a private operation/UUID
  backup records exact modified-file bytes and normalized mode or an addition's
  absence marker, then verifies publication. Fixed server-derived relative
  paths apply in UTF-8 lexical order through descriptor-relative no-follow
  traversal with no parent creation. Added files require Linux
  `renameat2(RENAME_NOREPLACE)`. Modified files require
  `renameat2(RENAME_EXCHANGE)`, followed by exact displaced-preimage proof;
  absence of the primitive fails closed.
- Failure: durable per-path journal states distinguish not attempted, applied,
  rolled back, and uncertain. Reverse rollback restores a modification only
  while its operation postimage is exact and removes an addition only while
  operation-bound rollback proves both prior absence and the exact created
  postimage. Exact restoration is an ordinary failure; ambiguity becomes
  reconciliation-required. Startup never adopts apparent success or retries an
  interrupted application.

## D-0209: Post-promotion Kodi verification is a separate bounded stage

- Status: approved and implemented for Phase 9 Slice 4
- Decision: successful source apply automatically starts one verification from
  its private selected-target handoff. Source terminal success, handoff
  publication, verification-child creation, exact reservation, and queued
  coordination commit in one database transaction before fallible verification
  preflight. Every promotion-service construction requires this publication
  collaborator; there is no optional/no-op construction mode. Later dispatch
  can execute or recover only the already durable child and cannot fabricate a
  missing child after source success. The sole verification UUID reserves the
  handoff without consuming it. The operation captures a fixed
  Kodi-log cursor, stops the exact container, performs handoff-bound
  changed/new-only sync, starts the same container, waits for normal readiness,
  proves ID,
  promoted version, supported type and enabled preservation from inventory,
  then reads a fixed ten-second, 1 MiB post-cursor log window.
- Results: sync, reload/readiness, inventory, and log outcomes remain separate.
  Verification succeeds only with exact sync/reload/inventory, complete log
  correlation, and no target error. Deterministic contradiction fails;
  unavailable non-contradictory observation is incomplete; post-mutation
  uncertainty requires reconciliation. No retry, enable repair, container
  recreate, source rollback, shell, arbitrary JSON-RPC, or generic file read is
  authorized. Ordinary history retains only classifications and counts;
  sanitized bounded target snippets live in a separate private evidence row.
  The complete attributable raw buffer, bounded by the existing 1 MiB scan
  ceiling, is decoded and deterministically redacted before control
  normalization, the 40 KiB retained-evidence cut, and snippet truncation.
  Bearer credentials, common token/key/
  password assignments, credential-like URL userinfo, and obvious token prefixes
  are replaced with `[REDACTED]`. This is not general DLP; raw log bytes are
  transient inside the private broker boundary and never enter normal DTO,
  history, result, or sanitized evidence storage.
- Consumption: the bound handoff remains reserved and unconsumed through exact
  preflight, cursor, stop, and sync. Exact sync readback, fresh ordinary
  deployment evidence, `consumed_at`, and the operation consumption marker
  commit atomically. A deterministic earlier failure remains reserved and
  unconsumed; ambiguity is reconciliation-required and the durable reservation
  prevents reuse.

## D-0210: Promotion state and evidence cross the source boundary explicitly

- Status: approved and implemented for Phase 9 Slices 3–5
- Decision: successful source application makes the workspace durably
  `promoted` and immutable, retains its history, preserves DevelopmentSource
  UUID/add-on ID, and makes every prior deployment-confidence row for that
  source stale. Enablement, smoke, and deployment operation history remain
  immutable history but do not prove the promoted tree.
- Handoff and coordination: immediately before staling evidence, Slice 3 writes
  one private promotion/target/source/runtime/container/prior-deployment-bound
  handoff for Slice 4. It is not generic current deployment confidence and has
  no public copy/sync authority. Per-workspace plan/operation exclusion,
  per-source promotion ownership, selected-target mutation ownership, and a
  global-heavy lease serialize capture/backup/apply against relevant mutation,
  deployment, attachment, lifecycle, reassignment, and deletion work. Complete
  rollback leaves the workspace ready; unresolved source state makes it
  reconciliation-required.
- Verification subset: one handoff owns one target, is reserved to one
  verification operation, and is consumed exactly once only with exact sync
  evidence publication. Reserved failures and ambiguities cannot be reused.
  Target and global-heavy leases exclude lifecycle, reset, Snapshot, reassignment,
  deletion, attachment, ordinary deployment, enable/smoke, ZIP/repository and
  replacement work. Exact sync restores ordinary verified deployment evidence;
  verification history never changes the succeeded source operation or
  promoted immutable workspace.
- Final lifecycle: `creating` may become `ready`, cleanly failed `abandoned`, or
  `reconciliation_required`; `ready` may become `promoted`, explicitly
  `abandoned`, or `reconciliation_required`. `promoted` and `abandoned` are
  immutable lifecycle outcomes. Ordinary cleanup never changes either outcome.
  `reconciliation_required` cannot be abandoned or cleaned by this workflow.
- Retention: byte presence is independent durable state, `present` or `cleaned`,
  with `cleaned_at`. A ready workspace must first be explicitly abandoned;
  promoted cleanup requires terminal source promotion and terminal Kodi
  verification. Active preparation/review/approval, source promotion,
  verification, creation, or reconciliation blocks cleanup. Lease expiry alone
  never makes work eligible.
- Cleanup authority: authenticated administrators submit one workspace UUID and
  `{"confirmed": true}`. A typed global-heavy cleanup operation records
  validation, a durable deletion marker with the exact root device/inode,
  descriptor-relative deletion, verification, and terminal outcome. The server
  derives `agent-workspaces/<uuid>` and removes the complete baseline, tree, and
  workspace-private staging/lock contents. It never accepts a path, follows a
  link, crosses a mount boundary, calls the broker, Docker, Kodi, Git, shell, or
  root, or modifies the DevelopmentSource.
- Crash rule: failure before the deletion marker is ordinary and leaves bytes.
  After the marker, only the same operation may resume removal of the same
  proven UUID subtree; identity contradiction or unprovable partial state is
  reconciliation-required. Directory-descriptor traversal removes injected
  links and special entries themselves without following them. Directory
  absence is success evidence only after the durable destructive marker (or a
  proven pre-publication creation failure).
- Durable history: metadata, final generation, provenance, mutation/lifecycle
  activity, plans, source operations and journals, verification operations,
  handoffs, private promotion backups, and bounded log evidence survive byte
  cleanup. List/detail remain path-free. Read, inspect, edit, diff, and promotion
  return `workspace_bytes_cleaned`; cleanup is idempotent and never recreates
  storage.
- ManagedInstance retention: promotion plan, source operation, exact handoff,
  and verification each retain an immutable original target UUID while their
  live ManagedInstance FK is nullable. The plan retains the bounded original
  target-name snapshot used by safe history projection. Centralized instance
  deletion detaches only terminal Phase 9 history; active or
  reconciliation-required Phase 9 work continues to block deletion. No
  workspace, source, promotion, handoff, verification, log, or backup history is
  deleted with the live target.
- Conservative V1 policy: cleanup is one-workspace-at-a-time and manual only.
  There is no TTL, timer, disk-pressure pruning, bulk cleanup, automatic cleanup
  after promotion/abandonment, or backup/log-evidence pruning. Later artifact
  retention work must approve any such deletion separately.

## D-0211: Phase 10 visual access begins as Web-administrator-only authority

- Status: approved and implemented for Phase 10 Slice 1 screenshot capture only
- Decision: authenticated local Web administrators may inspect screenshot
  capability, request one manual capture, and retrieve the latest private PNG
  for a ManagedInstance UUID. Capture POST requires the existing same-origin
  and CSRF controls; image retrieval requires the authenticated session and is
  never a static/public artifact URL.
- Boundary: Slice 1 adds no MCP, agent, token/client identity, browser remote,
  JSON-RPC input, keyboard/text input, interactive display, noVNC/VNC,
  WebSocket, lifecycle, log, workspace, or source-promotion authority.

## D-0212: Screenshot capture is one fixed semantic broker operation

- Status: approved and implemented for the complete four-runtime screenshot set
- Decision: `capture_managed_kodi_screenshot` accepts only canonical instance,
  expected immutable container, expected RuntimeRevision, and server-generated
  capture UUID identities. Trusted persistence admits only the exact current
  curated revisions for 19.5, 20.5, 21.3 and 22 Beta 2 together with their
  matching immutable profiles. Because the images have no `xwd` executable, the
  broker invokes one fixed isolated Python X11 `GetImage` program against the
  server-owned `:99` root with a five-second deadline. There is no
  caller-selected command, executable, argv, path, display, format, converter,
  timeout, Docker object, address, or port parameter.
- Artifact handoff: framebuffer bytes never enter framed broker JSON. The fixed
  program records server-observed depth, bits-per-pixel, byte order, scanline
  padding, row stride, visual class, bits-per-RGB and channel masks in a private
  versioned frame envelope. The broker writes it to a bounded mode-0600
  capture-UUID-derived staging object, returns only semantic identities/backend/
  byte count, and removes partial output on failure. The unprivileged application
  independently admits only the closed TrueColor RGB888 layout and performs PNG
  conversion and validation.

## D-0213: Managed screenshots use a private durable latest-only cache

- Status: approved and implemented for the complete screenshot runtime matrix
- Decision: SQLite table `managed_screenshots` contains at most one current
  binding per live ManagedInstance plus the last refresh outcome. PNG bytes live
  under the application data root and paths never enter DTOs. Publication is
  private new capture, complete RGB conversion and PNG validation, identity
  revalidation, atomic file publication, metadata replacement, then safe
  superseded-file removal.
- Failure/deletion: a failed refresh changes only bounded last-attempt error
  state and preserves the prior capture UUID, timestamp, metadata, and bytes.
  Missing cache bytes project safe unavailability and are not recreated.
  Central instance deletion removes screenshot bytes before its metadata
  transaction and explicitly deletes the row; the FK also cascades. Pixels are
  never retained as lifecycle history. A populated downgrade is refused because
  dropping metadata could orphan secret-bearing bytes.

## D-0214: Browser remote has one closed semantic vocabulary

- Status: approved and implemented across all four exact curated Kodi revisions
- Decision: Web accepts only a ManagedInstance UUID and `up`, `down`, `left`,
  `right`, `select`, `back`, `home`, `context_menu`, `info`, `play_pause`,
  `stop`, `previous`, `next`, `volume_up`, `volume_down`, or `mute_toggle`.
  Unknown actions and extra fields are rejected. No JSON-RPC method/params,
  player ID, endpoint, port, container/revision, timeout, path, command, shell,
  keycode, or text crosses the public boundary.
- Mapping: navigation uses the corresponding fixed `Input.*` method. Volume
  uses `Application.SetVolume` with server-fixed `increment`/`decrement`; mute
  uses `Application.SetMute` with `toggle`. Playback uses `Player.PlayPause`,
  `Player.Stop`, or `Player.GoTo` with fixed `previous`/`next` after one bounded
  `Player.GetActivePlayers`. These names and params are internal only.
- Player selection: video precedes audio, then picture; lowest player ID breaks
  a same-type tie. No active player is `remote_no_active_player`; malformed
  state is rejected rather than guessed.

## D-0215: Browser remote is coordinated and dispatched at most once

- Status: approved and implemented across all four exact curated Kodi revisions
- Coordination: one request-scoped process-local per-instance `browser_remote`
  guard conflicts with existing durable lifecycle, reset/restore, deletion,
  reassignment, deployment, install/replacement, and target promotion
  verification reservations in both acquisition directions. It is not
  global-heavy, has no durable operation row, and needs no schema migration.
- Dispatch: running/healthy/usable state, no reconciliation blocker, exact
  revision/profile, and unchanged container identity are proved before one
  fixed request. There is no automatic retry. Uncertain acknowledgement is
  `remote_action_ambiguous` and never claims failure.
- Refresh order: remote lease, identity proof, one dispatch, existing screenshot
  service for navigation, then lease release. Refresh failure is separate,
  preserves the prior cache, and never changes a successful remote result.

## D-0216: Visual capability is technical state, not added authorization

- Status: approved and implemented for the complete screenshot runtime matrix
- Decision: the path-free projection separates `screenshot_supported` from
  `screenshot_available_now` and current-cache presence. Support matches the
  exact four current curated RuntimeRevision UUIDs and their immutable profile
  identities; broad Kodi-version matching and nearby revisions fail closed.
  Availability additionally requires the exact running, healthy, usable managed
  runtime and no blocking reconciliation state. A stopped or absent runtime is
  never started or created for capture.
- Projection: current capture UUID, UTC timestamp, server-derived age,
  dimensions, fixed MIME, byte count, and bounded last-attempt classification
  are safe. Container identity, X display, command, network, host/storage path,
  Docker data, and raw failure are private.

## D-0217: Interactive display is one bounded view-only screenshot stream

- Status: approved and implemented for exact curated Kodi 21.3; Phase 10
  implementation is complete pending formal review
- Decision: authenticated local Web administrators may explicitly open one
  same-origin WebSocket for a ManagedInstance UUID. The server sends only
  strictly validated binary PNG frames at a hard maximum of one frame per
  second. Support is limited to RuntimeRevision
  `3a000000-0000-4000-8000-000000000006` and its exact immutable Kodi 21.3
  profile. Every other runtime is `display_unsupported`; screenshot support on
  the four-runtime matrix does not imply display support.
- Backend: the application reuses `capture_managed_kodi_screenshot` and the
  existing raw-frame decoder, PNG encoder, and strict PNG validator. Stream
  capture deletes its private raw staging artifact after every attempt and
  returns validated PNG bytes in memory. It performs no screenshot metadata
  write, durable PNG publication, latest-cache replacement, frame history, or
  per-frame audit. There is no new broker operation or schema.
- Authority: handshake requires the existing opaque local-admin session,
  trusted Host, exact Origin, no query string, and only a UUID route value.
  Authentication is re-resolved at least once per frame interval. There is no
  bearer URL, stream token, caller-selected rate, format, backend, timeout,
  path, endpoint, X display, or container identity. Browser data messages are
  protocol violations; keyboard, pointer, text, JSON, and control commands are
  never interpreted. All input remains on D-0214's semantic HTTP remote.
- Bounds: capture, validation, one bounded two-second send, then monotonic wait
  is strictly sequential, so queued-frame capacity is zero. Slow capture or
  send lowers delivered rate and never creates catch-up work. One session per
  instance, two globally, and a 15-minute hard lifetime are process-local
  limits. Capacity acquisition, WebSocket acceptance, streaming, and teardown
  share one exception-safe cleanup scope, so an acceptance or later failure
  releases the acquired instance/global slot exactly once. A successful-start
  audit event is emitted only after WebSocket acceptance. A server-generated
  display-session UUID is correlation metadata, not a credential.
- Lifecycle and identity: the session binds instance, exact RuntimeRevision,
  exact profile, and managed container identity. Running/healthy/usable state,
  reconciliation, session validity, support, and identity are revalidated for
  every frame. Durable mutation reservation signals process-local display
  cancellation and proceeds without waiting; an in-flight capture can produce
  no frame after failed post-capture revalidation. A replacement runtime is
  never adopted automatically.
- Presentation: the latest durable screenshot remains the default. Live display
  starts only from an explicit button, replaces the image visually, is marked
  `Live · up to 1 fps`, and has an explicit Stop. Close or error restores the
  latest durable screenshot when present. The frame is non-interactive and the
  semantic remote remains visible and independent. There is no VNC/noVNC,
  keyboard, pointer, full-text, video, image rebuild, or additional-runtime
  parity in this decision.

## D-0218: Visual privacy, errors, audit, and bounds are closed semantics

- Status: approved and implemented for screenshots and browser remote across the
  four exact curated Kodi revisions and live display on exact Kodi 21.3
- Privacy/validation: retrieval is authenticated with `image/png`, `no-store`,
  `nosniff`, same-origin CSP, and inline disposition. Strict validation requires
  the PNG signature, exact chunks/checksums, complete decompression, no trailing
  bytes, positive bounded dimensions, exact validated 1280x720 curated geometry,
  RGB8, and at most 8 MiB. Runtime validation additionally proves non-uniform
  initialized pixels without OCR or semantic screen recognition. There is no
  public URL, generic log, or pixel audit.
- Coordination/errors: process-local coordination permits one capture per
  instance and at most four globally. Stable Web errors include
  `managed_instance_not_found`, `visual_instance_not_running`,
  `visual_kodi_not_ready`, `screenshot_unsupported`,
  `screenshot_capture_in_progress`, `screenshot_capture_unavailable`,
  `screenshot_invalid_result`, and `visual_rate_limited`. No raw
  broker/Docker/X11/process detail escapes.
- Audit: bounded structured events contain capture/instance UUID, Web/local-admin
  markers, request/completion timestamps, result classification, and successful
  dimensions/byte count. They contain no pixels, path, command, display,
  container identity, or raw error. Slice 1 adds no durable operation row and no
  auto-refresh/history.
- Remote subset: bounded structured audit adds invocation UUID, closed action,
  request/completion timestamps, safe remote result, and screenshot-refresh
  classification. It excludes Kodi payloads, player metadata, endpoints,
  network/container identity, paths, pixels, and credentials. Remote errors are
  `managed_instance_not_found`, `visual_instance_not_running`,
  `visual_kodi_not_ready`, `remote_action_unsupported`,
  `remote_action_conflict`, `remote_no_active_player`, `remote_kodi_rejected`,
  and `remote_action_ambiguous`; screenshot errors remain separate.
- Rate bound: an in-process token bucket keyed by Web session plus instance
  allows 10 actions/second with burst 20 and returns `visual_rate_limited`
  before dispatch. Monotonic last activity drives opportunistic 15-minute idle
  expiry, explicit logout retires the session's buckets, and a hard ceiling of
  4,096 buckets bounds cardinality. At capacity, expired buckets are removed;
  if no safe idle eviction is available, a new key is rejected before dispatch
  rather than evicting a hot bucket. Buckets are not persisted.
- Display subset: the path-free projection adds `display_supported`,
  `display_available_now`, and `display_backend=live_screenshot_stream` only for
  the exact Kodi 21.3 profile. Stable display classifications are
  `managed_instance_not_found`, `display_unsupported`, `display_unavailable`,
  `display_instance_not_running`, `display_kodi_not_ready`, `display_conflict`,
  `display_rate_limited`, `display_session_expired`, and
  `display_backend_failed`. Raw X11, Docker, broker, socket, decoder, filesystem,
  network, endpoint, path, and container detail never escapes.
- Display audit is session-level only: bounded start/end events contain the
  display-session and instance UUIDs, Web/local-admin markers, timestamps, safe
  result/end reason, frame count, and aggregate bytes. Frames, pixels, per-frame
  hashes, endpoints, paths, container identities, credentials, and raw errors
  are excluded. No display-session table, frame table, or migration exists.

## Phase 10 formal completion note

The final independent closeout audit declared **Phase 10 — Browser
remote/display: COMPLETE** against the current canonical V1 specification. The
implementation checkpoint before documentation closeout is
`9821463b22c1d03a619571455f24e22853c0134e`; the schema remains at the single
Alembic head `0035_managed_screenshots`; and collection is 1884 tests. The
earlier `pending formal review` status wording above records the implementation
checkpoint at which D-0217 was approved; this note records the later formal
review result without rewriting D-0211 through D-0218.

Closeout revalidated four-runtime screenshot and closed browser-remote parity,
exact Kodi 21.3 view-only live display, the bounded 10-actions/second burst-20
remote limiter with 15-minute idle expiry and a 4,096-bucket ceiling, and
exception-safe display capacity across WebSocket acceptance failure. Optional
auto-refresh, keyboard/full-text, additional-runtime display parity, VNC/noVNC,
and higher-frame-rate video remain future work. Phase 10 adds no MCP or agent
authority.

## D-0219: Kodi Lab MCP uses the modern stateless protocol as its primary wire

- Status: approved; Phase 11 Slice 1 foundation implemented
- Decision: the primary wire protocol is exactly `2026-07-28` over POST
  `/mcp` in the existing FastAPI process on `127.0.0.1:8100`. It uses
  `server/discover` and per-request protocol metadata/headers, with no
  initialize/initialized exchange, `Mcp-Session-Id`, GET stream, application
  transport session, or selected/default target. Every target-scoped call
  carries an explicit `target_id`.
- Dependency: official Python MCP SDK `mcp==2.1.1` (and lock-resolved matching
  `mcp-types==2.1.1`) is exact-pinned because this is the reviewed generation
  implementing the new wire. The SDK can auto-fallback to handshake-era
  protocols, so Kodi Lab's outer gate requires `MCP-Protocol-Version` to equal
  `2026-07-28`; legacy `2025-11-25` compatibility is not implemented in this
  slice. Canonical stdio remains a later thin authenticated adapter.

## D-0220: MCP identity is durable and separate from Web identity

- Status: approved; Phase 11 Slice 1 implemented
- Decision: a durable enabled/disabled MCP client receives one assignable
  preset and owns separate credentials. A credential is 32 random bytes
  encoded base64url, revealed once, and persisted only as SHA-256 of the
  decoded high-entropy token. Verification uses constant-time comparison.
  No protected stable pepper facility exists, so Slice 1 does not introduce a
  poorly managed HMAC secret or a password KDF for machine tokens.
- Lifecycle: default expiry is 24 hours; Web administration may select a
  bounded shorter duration or explicit non-expiry. Issue, revoke, and atomic
  old-revoke/new-create rotation are supported. Disabled clients invalidate
  all credentials on the next request. Web cookies never authenticate MCP and
  MCP bearer credentials never authenticate Web routes.

## D-0221: MCP authority is an intersection recomputed on every call

- Status: approved; Phase 11 Slice 1 implemented
- Decision: the common boundary evaluates current credential validity, enabled
  client, client preset permission, exact client/target grant, target preset
  policy, tool permission, allowed target class/policy, technical capability,
  and required current state before an adapter executes. Unknown and
  unauthorized UUIDs share `target_not_found_or_not_authorized`. Discovery and
  an earlier successful request confer no cached authority.

## D-0222: One static transport-neutral registry owns MCP tool policy

- Status: approved; Phase 11 Slice 1 implemented
- Decision: server-owned definitions declare stable name, schema revision,
  permission set, read/mutate effect, allowed target classes, capability and
  state requirements, semantic rate class, adapter identity,
  production-eligibility, and whether target scope is required. Unknown
  permissions or incomplete metadata fail registration. Handlers remain thin
  and call a shared authorize-then-invoke boundary before Kodi Lab application
  services. User-editable arbitrary tool definitions are not supported.

## D-0223: Permissions are open-ended names but operationally fail closed

- Status: approved; Phase 11 Slice 1 implemented
- Decision: preset permissions are bounded normalized strings rather than a
  closed database enum, allowing later semantic permissions without replacing
  clients, credentials, presets, or grants. The production registry recognizes
  only approved permissions; Slice 1 recognizes only `target.inspect` and
  exposes no speculative capability.

## D-0224: Presets, exact grants, and target policy are distinct authority

- Status: approved; Phase 11 Slice 1 implemented
- Decision: deterministic immutable built-ins are Add-on Development,
  Read-only Inspection, Full Lab Control, and Production Remote Kodi. The first
  three currently contain only `target.inspect`; the production preset is
  present but unassignable and grants nothing until Phases 13/14. Full Lab
  Control remains target-use authority, never Web/infrastructure authority.
- Scope: every grant is exactly `(client_uuid, managed_target_uuid)`; there is
  no wildcard. A target policy assignment is separately required. Target
  deletion cascades both, and a newly created target UUID inherits neither.
  Custom-preset UI and granular RBAC editing are deferred.

## D-0225: Slice 1 exposes only bounded target inspection

- Status: approved; Phase 11 Slice 1 implemented
- Decision: the complete production inventory is lexical `target_list` and
  `target_status`. List pagination defaults to 50 and caps at 100. Both project
  only bounded semantic identity, runtime/profile classification, safe state,
  Kodi version, and safe capability. `ManagedRuntimeStatusService` remains the
  authoritative target-state path. Docker/container/network/port/path/X11,
  image/digest, broker internals, and raw Kodi JSON never leave the adapter.

## D-0228: MCP admission is bounded before tool execution

- Status: approved; Phase 11 Slice 1 implemented
- Decision: failed authentication uses actual connection peer identity, never
  attacker token text, at 5 requests/second with burst 20, at most 1,024
  buckets, and 15-minute idle retirement. Authenticated admission is 20
  requests/second per client with burst 40, at most 1,024 buckets, eight
  concurrent requests per client, and 32 globally. New cardinality is rejected
  rather than evicting hot authority. Monotonic clocks and exception-safe slot
  release are testable; later registry rate classes may add narrower bounds.

## D-0229: Phase 11 identity survives future target and tool expansion

- Status: approved; Phase 11 Slice 1 foundation implemented
- Decision: current grant and target-policy foreign keys intentionally bind to
  `managed_instances` so Slice 1 cannot authorize a non-managed target. Static
  metadata already names `internal_lab`, `external_development`, and
  `external_production`, but production tools admit only `internal_lab` and an
  actual ManagedInstance row is mandatory.
- Migration path: Phase 13 may rebuild the two small association tables to
  reference the already-existing generic `targets.id`, add external subtype
  persistence/adapters, and migrate existing UUID rows unchanged. Phase 14 may
  then add explicit production-safe policies. Client, credential, preset,
  permission, transport, registry, and authorization identities remain intact;
  target class alone is never permission.

## D-0230: Canonical MCP stdio is an authenticated loopback HTTP proxy

- Status: approved; Phase 11 Slice 2 implemented
- Decision: installed command `kodi-lab-mcp-stdio` accepts modern
  `2026-07-28` newline-delimited MCP on stdin/stdout and proxies each request to
  the existing loopback `/mcp` authority. It does not instantiate the Kodi Lab
  database, repositories, services, tool registry, authentication, target
  resolution, authorization, or error adapters. The HTTP boundary remains the
  sole authority and receives `Authorization: Bearer` on every request.
- Credential and endpoint: the bearer is accepted only from
  `KODI_LAB_MCP_TOKEN`. It is never accepted on argv, in the endpoint, in an MCP
  payload, or from a Kodi Lab-created config file. The default endpoint is
  `http://127.0.0.1:8100/mcp`; optional `KODI_LAB_MCP_URL` is restricted to
  plain HTTP, exact loopback host `127.0.0.1` or `localhost`, exact `/mcp`, and
  no userinfo, query, or fragment. Redirects are disabled.
- State and bounds: the process creates no session/default target, credential
  verifier, grant cache, or durable state. It forwards no `Mcp-Session-Id` and
  rejects any such response. Input frames retain the HTTP 64 KiB bound. Four
  workers and an eight-entry backpressured queue bound process-local memory;
  the HTTP admission boundary remains authoritative. EOF cancels the task group
  and outstanding HTTP work; the adapter never daemonizes.
- Protocol and privacy: no initialize/initialized or legacy fallback is
  introduced. Stable bounded errors omit token, traceback, endpoint, and HTTP
  library detail. Environment variables remain visible to sufficiently
  privileged same-user/root processes; that caveat does not justify plaintext
  credential files.
- Audit: no caller-supplied transport label is sent. Server audit therefore
  records the authenticated HTTP authority path without claiming a safely
  distinguished stdio origin. Transport never changes permission or target
  trust class.

## D-0231: Slice 2 hardens credential comparison and registry scope enforcement

- Status: approved; Phase 11 Slice 2 remediation implemented
- Decision: structurally valid unknown bearer credentials execute the same
  constant-time SHA-256 digest comparison primitive against a fixed dummy
  verifier before generic rejection. Registry `target_scope_required` metadata
  is enforced by both common authorization entrypoints, preventing a scoped
  tool from using the global path or a global tool from using the target path.
  These changes add no permission, tool, target class, or migration.

## D-0232: Slice 3 adds two service-backed read-only observation tools

- Status: approved and implemented for Phase 11 Slice 3
- Decision: the complete production catalog becomes lexical `addon_list`,
  `screenshot_capture`, `target_list`, and `target_status`. `addon_list` requires
  `target.inspect`, uses `ManagedInstanceAddonInventoryService`, defaults to 50
  records, caps at 100, orders by stable semantic add-on ID and exposes no path,
  URL, raw Kodi result or internal identity. `screenshot_capture` requires
  `target.visual` and uses `ManagedScreenshotService` plus the existing fixed
  broker primitive. Neither adapter creates a new Kodi, broker, filesystem,
  authentication or authorization path.
- Authority: both definitions retain exact target scope, internal-lab class,
  server-owned capability/state metadata, read-only effect and semantic rate
  class. The common authority intersection remains mandatory. Unknown and
  unauthorized targets stay indistinguishable. Screenshot capability is
  projected from the exact curated Phase 10 runtime profiles, not from Kodi
  version text or the Kodi 21.3 live-display subset.

## D-0233: Native screenshot results retain the durable capture and bounded stdio

- Status: approved and implemented for Phase 11 Slice 3
- Decision: one screenshot tool call makes at most one existing durable capture
  attempt. The service returns the exact validated PNG object used for atomic
  latest-cache publication together with the durable result, avoiding a racy
  reread of a later `latest` capture. MCP uses official SDK `ImageContent` with
  exact `image/png` plus compact structured metadata. Failed capture preserves
  the previous cache, and no MCP-specific image copy, URL or temp-file transport
  is created.
- Bounds: the existing PNG ceiling stays 8 MiB. The 64 KiB request limit is not
  reused as a response limit. Stdio reads at most the maximum base64 expansion
  plus 64 KiB JSON/metadata overhead, serializes awaited stdout writes, and
  retains four workers plus the eight-item backpressured inbound queue. Image,
  base64, hash and path data are absent from MCP audit records.
- Presets/migration: `0037_mcp_observation_permissions` adds `target.visual` to
  Add-on Development, Read-only Inspection and Full Lab Control. All retain
  `target.inspect`; Production Remote Kodi remains empty and unassignable.
  Downgrade refuses when assigned authority would be lost. `target.control`
  remains reserved for the remote-action slice.

## D-0234: MCP semantic remote control uses durable client-scoped replay receipts

- Status: approved and implemented for Phase 11 Slice 4
- Tool and authority: production adds only lexical `remote_action`, classified
  mutating/control, internal-lab-only and not production-eligible. Its strict
  input is client UUID `request_id`, explicit target UUID, and D-0214's existing
  16-value `BrowserRemoteAction`. It requires `target.control`; migration
  `0038_mcp_remote_control` seeds that permission only for Add-on Development
  and Full Lab Control. Read-only Inspection gains nothing and Production Remote
  Kodi remains empty/unassignable.
- Service ownership: MCP wraps the existing D-0215 `BrowserRemoteService` rather
  than copying mappings, active-player selection, mutation coordination,
  screenshot refresh or no-retry transport behavior. A server-created actor
  context supplies either Web local administrator or MCP client UUID; request
  data cannot name an actor.
- Replay identity and transaction boundary: `(client_id, request_id)` is unique.
  Credential UUID is recorded but credential rotation does not create a new
  namespace. Current authentication and target authority are recomputed before
  every new or duplicate attempt. A short transaction commits `pending` before
  rate admission or Kodi dispatch; the network call occurs with no database
  transaction; a separate short transaction publishes terminal state.
- State machine: an exact completed duplicate returns the stored semantic result
  without rate consumption or dispatch. Pending/uncertain replay returns
  `remote_action_ambiguous`; changed target/action returns
  `request_id_conflict`. Dispatch uncertainty is durable `ambiguous`. A crash or
  result-write failure leaves `pending`, which is also never redispatched.
  Database uniqueness decides concurrent claims, so process-local locks are not
  the replay boundary.
- Rate/order: only the winner for a new receipt enters the existing shared 10/s,
  burst-20 limiter, keyed by server-derived MCP client plus target. A stable rate
  rejection completes the receipt as `rate_limited`; its duplicate is replayed
  without consuming another token.
- History/privacy: receipts have no cascading target/client/credential FK and
  therefore survive target deletion. They retain only UUIDs, the closed action,
  timestamps, state, bounded result, and bounded refresh classification/capture
  metadata. Tokens, arbitrary text, raw JSON-RPC, screenshot bytes/paths,
  endpoints and runtime/container/network data are excluded. A new target UUID
  inherits no authority; reuse of the same client request ID still conflicts by
  design.
- Bounds: claim-time cleanup retains coherent completed receipts for 30 days,
  after which those request IDs may be reused. Pending or ambiguous identity is
  protected because age cannot prove no dispatch. Hard limits are 10,000
  receipts/client and 100,000 globally; saturation refuses new protected work.
  There is no daemon, MCP/Web delete surface, manual replay action, lifecycle
  authority, external target, or production-control policy in this decision.

## D-0235: V1 custom presets are immutable built-in snapshots

- Status: approved and implemented for Phase 11 Slice 5
- Decision: a Web administrator creates a custom preset from one active
  immutable built-in, a trimmed bounded unique name, and a new random UUID. The
  transaction copies only the built-in's current server-known permission rows.
  The inactive Production Remote Kodi preset is not a template. Authorization
  identity remains the UUID, never the display name.
- Boundary: V1 has no arbitrary permission editor, custom rename, custom delete,
  built-in mutation/conversion, or custom-from-custom chain. Future permissions
  do not automatically broaden existing clones. Existing client and internal
  target-policy assignments accept the clone and retain every other authority
  intersection layer.
- Audit/schema: creation and reassignment emit content-free UUID events;
  reassignment records the displaced preset as unassigned. Existing schema is
  sufficient and Alembic remains `0038_mcp_remote_control`.

## D-0236: Exact 2025-11-25 compatibility is stateless HTTP only

- Status: approved and implemented for Phase 11 Slice 5
- Decision: primary protocol remains exact `2026-07-28`. The same POST `/mcp`
  authority also accepts exact `2025-11-25` with the pinned SDK in stateless
  mode. Only an exact headerless `initialize` offer may select legacy; later
  requests carry exact `2025-11-25`. Unsupported versions and cross-era methods
  are rejected without fallback.
- State/authority: no `Mcp-Session-Id`, GET stream, selected target, protocol
  identity, credential, permission, or authorization state is persisted. Both
  eras share bearer verification, admission, principal derivation, registry,
  authorization, service adapters, replay receipts, errors, and privacy.
- Stdio/deprecation: canonical stdio remains modern-only because legacy stdio
  would require negotiation state in the proxy. There is no second server.
  Future removal of HTTP legacy support requires an explicit decision,
  compatibility evidence, and protocol/schema policy review.

## D-0237: Phase 11 closes with a tested five-tool client matrix

- Status: approved and implemented for Phase 11 Slice 5
- Decision: official Python SDK `2.1.1` is the reference for modern pinned,
  modern auto, stdio, and legacy HTTP. Official conformance covers all applicable
  implemented surfaces; runner-only fixtures and absent optional prompt,
  resource, subscription, sampling, elicitation, OAuth, task, and MRTR surfaces
  are classified exclusions rather than production tools.
- Third parties: OpenCode `1.14.39` is compatible over legacy HTTP and not over
  modern-only stdio. Codex CLI `0.154.0` is tested-incompatible because it offers
  `2025-06-18`; Kodi Lab does not broaden its exact compatibility boundary.
  Untested clients receive no claim.
- Boundary: production remains exactly `addon_list`, `remote_action`,
  `screenshot_capture`, `target_list`, and `target_status`. Phase 11 is
  implementation-complete pending independent formal review, not formally
  complete.

## Phase 11 formal completion record

- Status: **Phase 11 — MCP core: COMPLETE**
- Review basis: independent formal review of implementation checkpoint
  `3faeeb808ca1a86ab5b1a8a0038827fe9b76b57a`, Alembic head
  `0038_mcp_remote_control`, and 1,928 collected tests. D-0219 through D-0237
  remain the governing protocol, identity, authority, tool, compatibility and
  client-boundary decisions; the pending status in D-0237 records its earlier
  implementation checkpoint.
- Accepted core: primary stateless `2026-07-28` HTTP, canonical modern-only
  stdio, bounded stateless `2025-11-25` HTTP compatibility, durable
  verifier-backed clients, built-in and clone-only custom presets, explicit
  grants and target-policy intersection, the exact five-tool registry, native
  bounded screenshots, and durable replay-safe semantic remote control.
- Compatibility boundary: Codex CLI `0.154.0` is not a V1 acceptance target and
  its excluded `2025-06-18` offer does not expand the approved two-revision
  boundary. OpenCode `1.14.39` retains only its proven legacy-HTTP connection
  and authorized discovery claim. Untested clients have no compatibility claim.
- Later phases: AgentWorkspace and DevelopmentSource mutation, deployment and
  promotion, external targets, production approval policy, broad diagnostics,
  and later Kodi tools remain outside Phase 11.

## D-0238: Phase 12 begins with authorization and read-only inspection
- Status: accepted for Phase 12 Slice 1.
- Decision: preserve Phase 11 and add only `workspace.inspect`, exact source
  grants, exact workspace ownership, and four bounded read-only tools. No
  development mutation is implied.

## D-0239: Source access is an exact Web-admin-managed grant
- Status: accepted for Phase 12 Slice 1.
- Decision: persist only `(mcp_client_uuid, development_source_uuid)`. There is
  no wildcard. Registration and locator authority stay Web-only, grant changes
  apply on the next request, and ungranted UUIDs are not discoverable.

## D-0240: MCP workspace ownership is one exact durable binding
- Status: accepted for Phase 12 Slice 1.
- Decision: one binding owns a workspace for one MCP client and exact source. A
  composite foreign key enforces source consistency. Origin is closed to
  `web_admin|mcp_created`; only `web_admin` is created in this slice.

## D-0241: Grant and binding authority remain independent
- Status: accepted for Phase 12 Slice 1.
- Decision: access requires enabled client, `workspace.inspect`, current exact
  grant, exact binding, matching source, and safe operation state. Revocation
  retains binding/bytes; restoration may restore access. Credential lifecycle
  never changes client ownership.

## D-0242: Development reads reuse Phase 9 services and paths
- Status: accepted for Phase 12 Slice 1.
- Decision: MCP never derives storage. Status uses durable state; tree/read use
  the existing normalized relative-path, descriptor-relative no-follow service.
  Reconciliation-required status remains visible while tree/read fail closed.
  Linux case and NFC/NFD distinctions are not normalized.

## D-0243: Development inspection is compact and bounded
- Status: accepted for Phase 12 Slice 1.
- Decision: source list is path-free metadata; status is owned-and-granted
  state; tree is one directory and at most 200 entries; read is one strict UTF-8
  segment over a 2 MiB file maximum. Structured development results are below
  48 KiB.

## D-0244: Development inspection privacy is semantic
- Status: accepted for Phase 12 Slice 1.
- Decision: absence, another owner, missing/revoked grant, and source mismatch
  share `workspace_not_found_or_not_authorized`. Audit retains semantic UUIDs,
  tool, outcome, and times, but no content, hash, locator, path, tree dump,
  arbitrary argument, or raw error.

## D-0245: Web administration alone grants and binds Slice 1 authority
- Status: accepted for Phase 12 Slice 1.
- Decision: Web may grant/revoke, list eligible workspaces, bind one existing
  workspace, and inspect bindings under existing authentication, Host, Origin,
  and CSRF controls. MCP cannot self-grant, self-bind, transfer, share, register,
  create, or unbind.

## D-0246: Development reads share bounded admission and transport behavior
- Status: accepted for Phase 12 Slice 1.
- Decision: four tools share one client-keyed 10/second burst-20 bounded rate
  class inside existing admission and behave identically through modern HTTP,
  modern stdio, and legacy HTTP. The catalog is exactly nine lexical tools.
  Phase 12 remains partial.

## D-0247: Workspace mutation is one new permission and two compact tools
- Status: accepted for Phase 12 Slice 2.
- Decision: add `workspace.edit` only to Add-on Development and Full Lab
  Control. Existing custom presets remain immutable snapshots. Read-only
  Inspection and Production Remote Kodi do not gain it. The only new tools are
  replay-safe `workspace_create` and discriminator-based `workspace_edit`; the
  latter closes operations to `create|replace|exact_patch`.

## D-0248: MCP creation ownership and replay identity commit atomically
- Status: accepted for Phase 12 Slice 2.
- Decision: reuse the Phase 9 `AgentWorkspaceService.queue_creation` allocation
  and operation. In one immediate SQLite transaction, publish the workspace,
  creation operation, existing global-heavy reservation, `mcp_created` owner
  binding, and `mcp_development_requests` mapping before executor submission.
  The mapping is unique by client UUID, request UUID, and operation kind and
  binds a normalized source fingerprint plus exact resource/operation UUIDs.
  Nothing can observe or claim an unbound MCP-created workspace.
- Recovery: exact replay returns the same UUIDs and current semantic states;
  changed source is `request_id_conflict`. A committed queued operation is
  recovered by normal executor startup after a crash. Unknown outcome resolves
  through the mapping or returns `workspace_creation_ambiguous`; it never
  allocates a second speculative workspace.

## D-0249: Creation quotas count every still-present MCP-owned workspace
- Status: accepted for Phase 12 Slice 2.
- Decision: count both binding origins while workspace bytes remain `present`,
  including creating, ready, promoted, abandoned, and reconciliation-required
  lifecycle states. Enforce four per client, 32 globally, and one creating per
  client through an immediate transaction and durable queries. There is no
  eviction. Saturation is `workspace_quota_exceeded`.
- Replay-key bounds: mappings retain 30 days, opportunistically delete only
  expired request identities, and cap at 10,000 per client and 100,000 global.
  This is not workspace cleanup or retention automation; that remains Slice 3.

## D-0250: Workspace edit is exact Phase 9 generation CAS
- Status: accepted for Phase 12 Slice 2.
- Decision: every edit requires exact owner binding, current source grant,
  `workspace.edit`, byte-present ready state, and exact expected generation.
  Create requires absence and an existing parent; replace requires an existing
  UTF-8 regular file; exact patch is a complete exact-text preimage. No force,
  overwrite option, mkdir, delete, rename, mode, fuzzy patch, regex, diff
  parser, merge, rebase, or normal edit request ID exists.
- Integrity: call only the existing Phase 9 service. Its workspace lock, full
  pre/post recapture, atomic write, rollback/reconciliation behavior, and
  conditional generation publication remain authoritative. A retry with the
  old generation conflicts rather than silently repeating a mutation.

## D-0251: MCP edit payload and result are smaller than Phase 9 internals
- Status: accepted for Phase 12 Slice 2.
- Decision: aggregate user text is at most 48 KiB and conservatively encoded
  arguments remain below the 64 KiB transport request limit. Underlying files
  retain the Phase 9 2 MiB bound. Success returns UUIDs, relative path,
  operation, prior/new generation, semantic success, and authoritative byte
  size, never automatic file content.

## D-0252: Mutation privacy and admission remain semantic
- Status: accepted for Phase 12 Slice 2.
- Decision: mutation audit retains client/source/workspace UUIDs, tool, outcome,
  and times but no path, content, expected/replacement text, patch, locator, raw
  error, or host detail. Development edits use a client-keyed two/second,
  burst-four bounded rate class and one process-active edit per workspace;
  Phase 9 supplies cross-process exclusion. Creation uses durable quota,
  replay, and global-heavy coordination.

## D-0253: Slice 2 preserves transport parity and stops before validation
- Status: accepted for Phase 12 Slice 2.
- Decision: modern `2026-07-28` HTTP, modern stdio, and bounded `2025-11-25`
  HTTP share one client-scoped creation replay namespace and one edit/CAS
  implementation. The production catalog is exactly eleven lexical tools.
  There is no legacy stdio, validation, deployment, execution, promotion
  request, workspace cleanup automation, external/production target, shell,
  Git, Docker, raw filesystem, or arbitrary command authority. Phase 12 remains
  partial.

## D-0254: Structural validation is one closed observational tool
- Status: accepted for Phase 12 Slice 3.
- Decision: add only `workspace_validate`, requiring `workspace.inspect`, exact
  owner/source/grant agreement, ready present state and exact generation. Inputs
  are only workspace UUID and generation. No new permission, target grant,
  command, test selector, script, environment, package manager or timeout is
  accepted, and no validation history table is created.

## D-0255: Phase 9 recapture and add-on parsing remain authoritative
- Status: accepted for Phase 12 Slice 3.
- Decision: validation reuses the Phase 9 coordinated complete baseline/current
  recapture, UTF-8 change checks and authoritative `addon.xml` parser. It
  classifies ID/version/extension changes and promotability, rejects deletion,
  binary mutation, type/mode transition and unsafe objects, never repairs or
  adopts drift, and returns no content, digest, locator or host path.

## D-0256: Validation output and concurrency are bounded
- Status: accepted for Phase 12 Slice 3.
- Decision: results remain below 48 KiB and expose at most 64 deterministic
  relative changed paths plus complete counts and truncation. Admission is one
  validation/client, one/workspace and two globally. Exact-generation conflict
  is final for that call; there is no retry against newer state.

## D-0257: Terminal MCP workspace bytes retain seven days
- Status: accepted for Phase 12 Slice 3.
- Decision: MCP-owned abandoned and safely terminal promoted workspace bytes
  become eligible after seven days through the existing Phase 9 cleanup service.
  Reconciliation, active/ambiguous promotion or verification, failed cleanup and
  other uncertainty retain bytes for Web-admin attention. Cleanup never deletes
  binding or history, and cleaned bytes no longer consume present-byte quota.

## D-0258: Disabled-client ready workspaces quarantine for thirty days
- Status: accepted for Phase 12 Slice 3.
- Decision: persist `mcp_clients.disabled_at`. Disable revokes MCP access
  immediately but preserves bytes. After 30 days a still-safe ready workspace
  uses existing abandonment, then the ordinary seven-day cleanup grace.
  Re-enable clears `disabled_at` and cancels quarantine eligibility. Enabled
  ready workspaces never age-delete.

## D-0259: Retention is bounded system policy under workspace coordination
- Status: accepted for Phase 12 Slice 3.
- Decision: an opportunistic activity/startup sweep inspects at most 32
  candidates, makes at most two lifecycle transitions and queues at most two
  cleanups in deterministic cursor order. It rechecks state, shares Phase 9
  workspace coordination with edit/validation, submits to the existing cleanup
  executor and records content-free audit classifications. MCP gains no abandon,
  cleanup or delete tool; Web retains only its existing guarded controls.

## D-0260: Slice 3 preserves protocol and deployment boundaries
- Status: accepted for Phase 12 Slice 3.
- Decision: modern `2026-07-28` HTTP, modern stdio and bounded `2025-11-25`
  HTTP share one twelve-tool lexical catalog and validation implementation.
  There is no legacy stdio, deployment, add-on execution, promotion request,
  external/production target, broker, Docker, shell, Git or arbitrary command
  authority. Phase 12 remains partial.

## D-0261: Slice 4 adds exactly two permissions and two tools
- Status: accepted for Phase 12 Slice 4.
- Decision: add `workspace.deploy` and `development.execute` only to the
  Add-on Development and Full Lab Control built-ins. Existing custom presets
  remain immutable snapshots; Read-only Inspection and Production Remote Kodi
  remain unchanged. Add only `workspace_deploy` and `workspace_execute`, making
  the exact lexical production catalog fourteen tools. Promotion authority is
  not added.

## D-0262: Successful exact-generation validation is a deployment precondition
- Status: accepted for Phase 12 Slice 4.
- Decision: retain one latest successful structural-validation evidence row per
  workspace: workspace/source UUIDs, generation, parsed add-on ID/version,
  bounded extension classification and validation time. Deployment requires
  this evidence to match the current exact generation and approved source/add-on
  identity. Editing advances generation and therefore invalidates eligibility
  without deleting history or inventing a validation-history subsystem.

## D-0263: Workspace deployment is one transport-neutral closed service
- Status: accepted for Phase 12 Slice 4.
- Decision: the MCP handler supplies only request, workspace, generation and
  target UUIDs. A new application service owns exact workspace/source checks,
  matching DevelopmentSource attachment, internal-disposable target policy,
  canonical baseline or safe prior-workspace evidence, lifecycle coordination,
  stopped-only overwrite/add copy, readiness, inventory verification and
  semantic publication. Paths, container identity, sync flags, package format,
  restart choice and commands remain server-derived and are never MCP input.

## D-0264: Deployment ambiguity is durable and never redispatched
- Status: accepted for Phase 12 Slice 4.
- Decision: persist the request and operation before the first target mutation,
  then mark stop/sync/start/readiness/inventory phases. Completed duplicates
  return stored evidence; changed parameters conflict. An unresolved request
  after the mutation marker returns `deployment_ambiguous`, is restart-swept to
  reconciliation-required, is never automatically retried, and attempts to
  leave the target stopped. Sync uncertainty also makes canonical source
  deployment confidence unknown. Web administrator reconciliation remains the
  recovery plane.

## D-0265: Deployment replay and concurrency reuse existing coordination
- Status: accepted for Phase 12 Slice 4.
- Decision: extend `mcp_development_requests`; identity remains durable client
  UUID plus request UUID plus operation kind. Enforce one active deployment per
  client, workspace and target, the managed-target lease, workspace lock and
  global-heavy lease. The deployment admission interval is one client/target
  pair per five minutes with bounded idle-retired keys. There is no MCP Task or
  second replay mechanism.

## D-0266: Execution is only the fixed copied Python video-plugin smoke
- Status: accepted for Phase 12 Slice 4.
- Decision: `workspace_execute` has the same four UUID/generation inputs and
  requires an exact successful deployment for the current workspace,
  generation and target. The server derives add-on ID/version, requires enabled
  exact inventory and destination provenance, admits only the sole copied
  `xbmc.python.pluginsource` video entry point under suppress-bytecode policy,
  and invokes the fixed bounded `Files.GetDirectory` smoke adapter. Services,
  scripts, repositories, binary add-ons, skins and other types return
  `execution_unsupported`; no arbitrary add-on, URL, parameters or JSON-RPC is
  accepted.

## D-0267: Execution is conservative at-most-once across restart and protocol
- Status: accepted for Phase 12 Slice 4.
- Decision: persist the claim before dispatch and the invocation marker before
  calling Kodi, without holding a database transaction across Kodi. Completed
  duplicates return the stored bounded classification/count; mismatch
  conflicts. Timeout, lost response, crash after the marker or unverifiable
  post-provenance is `execution_ambiguous` and never redispatched. One active
  execution per target and one client admission per minute are enforced with
  bounded fake-clock-testable state.

## D-0268: Evidence, retention and protocols remain bounded
- Status: accepted for Phase 12 Slice 4.
- Decision: deployment/execution rows retain semantic UUIDs, generation,
  add-on identity/version, phases, bounded classifications/counts and times,
  never paths, container IDs, raw Kodi, directory entries, logs or content.
  Current deployment and unresolved operation evidence blocks automatic byte
  cleanup. Workspace status and existing Web administration project only the
  latest bounded validation/deployment/execution state. Modern HTTP, modern
  stdio and bounded legacy HTTP share the same service and durable replay
  namespace. Promotion request, human approval integration and formal Phase 12
  closeout remain later work; Phase 12 remains partial.

## D-0269: Slice 5 adds one permission and one preparation tool
- Status: accepted for Phase 12 Slice 5.
- Decision: add only `development.request_promotion` to Add-on Development and
  Full Lab Control and add only `promotion_request`. Existing custom snapshots,
  Read-only Inspection and Production Remote Kodi remain unchanged. Inputs are
  exactly request, workspace, generation and target UUIDs. Approval, apply,
  force, summary, source path and changed-file inputs are unrepresentable.

## D-0270: Exact execution evidence is a promotion-request prerequisite
- Status: accepted for Phase 12 Slice 5.
- Decision: MCP preparation requires the current ready byte-present owned
  workspace, current source and target grants, exact internal-disposable target
  policy, same-generation successful validation, deployment and fixed execution
  evidence, stable source/workspace/add-on identity and promotion-safe Phase 9
  state. The application derives source, version and complete change summary.

## D-0271: MCP reuses the Phase 9 promotion plan and Web review
- Status: accepted for Phase 12 Slice 5.
- Decision: the adapter invokes the existing Phase 9 preparation boundary and
  stores bounded MCP origin and exact deploy/execute evidence on that durable
  plan. Phase 9 remains sole owner of change derivation, source preimage,
  conflict detection, backup, atomic apply, rollback, handoff and verification.
  The existing exact-diff Web review is reused; MCP receives no diff or content.

## D-0272: Promotion request replay commits with the plan
- Status: accepted for Phase 12 Slice 5.
- Decision: extend `mcp_development_requests` with `promotion_request`; publish
  the request mapping and Phase 9 preparing plan in one transaction. Exact
  replay returns the same plan and current state, changed parameters conflict,
  and a committed but still-preparing marker is
  `promotion_request_ambiguous`, never automatic re-preparation. One active plan
  per workspace and existing expiry rules prevent plan spam. Active-plan maps
  are not pruned by ordinary 30-day request retention.

## D-0273: Promotion approval and rejection are Web-only human authority
- Status: accepted for Phase 12 Slice 5.
- Decision: MCP can neither approve nor reject. Review, literal confirmation,
  approval and rejection use the existing administrator session, Trusted Host,
  same-origin and CSRF boundary. A valid MCP bearer is not Web authentication.
  Approval rechecks the exact plan, source conflict, target anchor and immutable
  generation/evidence before entering the canonical Phase 9 apply path.

## D-0274: Revocation blocks MCP but does not silently decide human review
- Status: accepted for Phase 12 Slice 5.
- Decision: disabling the originating client or revoking its source/target
  grant immediately blocks further MCP calls and status access but preserves
  plan/history. It does not itself approve, reject or invalidate the plan. The
  Web administrator may inspect/reject and may approve only if the independent
  canonical plan/source/workspace/target/evidence recheck remains valid. Current
  MCP authority is deliberately not delegated into the human approval decision.

## D-0275: Source success still requires the Phase 9 verification handoff
- Status: accepted for Phase 12 Slice 5.
- Decision: Web approval uses the existing backup, atomic source apply, target
  sync/reload, inventory/readiness/log verification and durable verification
  child. MCP preparation never claims source success. Once source apply has
  durably succeeded, a failed or incomplete verification child remains a
  distinct `source_promoted_verification_failed|incomplete` classification and
  never rewrites immutable source-apply success.

## D-0276: Slice 5 remains bounded and closes implementation only
- Status: accepted for Phase 12 Slice 5.
- Decision: admit at most two new preparations per client per ten minutes with
  bounded keys and existing workspace/promotion coordination. Audit only
  semantic UUIDs, generation, tool, outcome and times. Active promotion and
  required verification continue to block byte cleanup. `workspace_status`
  projects bounded latest plan, operation, verification and final
  classification. Modern HTTP, modern stdio and bounded legacy HTTP share one
  fifteen-tool lexical catalog and replay namespace. There is no approval tool,
  Git, repository publication, shell, arbitrary execution, external or
  production target. Successful Slice 5 is implementation-complete pending a
  separate formal Phase 12 review, not formal completion.

## D-0277: Shared development-request pruning is lifecycle-aware and fail-closed
- Status: accepted Phase 12 remediation.
- Decision: all `mcp_development_requests` retention uses one bounded, DB-only
  pruner across creation, deployment, execution and promotion. Ordinary 30-day
  expiry applies only after the exact associated workflow is provably terminal
  and replay-safe to forget. Creation queued/running/reconciliation states,
  deployment mutation-marked or unresolved states, execution dispatch-marked
  or unresolved states, active review/apply, required verification, and every
  missing/unknown/inconsistent relationship retain the mapping. Successful
  promotion remains protected through terminal mandatory verification.
  Protected rows continue to count toward 10,000/client and 100,000/global
  capacity; saturation fails closed. Candidate and unreferenced terminal
  operation cleanup are bounded and deterministic. This changes no transport,
  tool, permission, schema, retention horizon, or Phase 12 completion status.

## D-0278: Replay pruning requires coherent evidence and bounded references
- Status: accepted Phase 12 remediation.
- Decision: D-0277 terminality is a positive proof over the complete durable
  evidence shape, not an inference from terminal state and completion time.
  Creation lifecycle, deployment mutation/progress, execution dispatch/result,
  and promotion plan/apply/handoff/verification fields must be internally
  consistent; every malformed, contradictory, incomplete, missing, or unknown
  combination is retained. In particular, a pre-mutation deployment failure has
  no stop, sync, restart, inventory, result, or digest evidence, and a
  pre-dispatch execution failure has no dispatch, post-dispatch classification,
  result, or count evidence. Referencing promotion workflows are tested by one
  scalar SQL existence predicate per inspected deployment/execution rather than
  materializing historical plans. The outer batch remains 256; per-candidate
  queries and instantiated application rows are therefore finite regardless of
  promotion fanout. Terminal operation cleanup applies the same consistency
  predicates and bounded existence checks. No tool, protocol, permission,
  migration, retention horizon, or Phase 12 status changes.

## D-0279: Promotion journal evidence is authoritative for replay retention
- Status: accepted Phase 12 remediation.
- Decision: promotion-request pruning treats the content-free Phase 9 source
  application journal as authoritative mutation evidence. Definitive
  pre-application failure requires an exact one-to-one plan-path journal whose
  rows are all `not_attempted`, no backup, handoff or verification progress, and
  coherent failed operation markers. Successful terminal promotion requires the
  same exact journal shape with all rows `applied` plus the existing coherent
  handoff and terminal verification proof. Applied, rolled-back, uncertain,
  missing, extra or mismatched journal evidence overrides contradictory
  high-level markers and retains replay identity. Correlated scalar existence
  predicates keep the decision bounded without loading journal collections.
  This changes no tool, protocol, permission, migration, horizon, retention
  authority, or Phase 12 completion status.

## Phase 12 formal completion record

- Status: **Phase 12 — MCP development workflows: COMPLETE**
- Review basis: independent formal review of implementation checkpoint
  `dc6f7857b222aff31467222e8d9eff8b96df6328`, Alembic head
  `0043_mcp_promotion_request`, and 2,020 collected tests. D-0238 through D-0279
  remain governing decisions; their PARTIAL, implementation-complete and
  remediation-status wording records historical checkpoints.
- Accepted workflow: exact Web-managed DevelopmentSource grants, one durable
  workspace owner, bounded inspection, replay-safe creation, generation-CAS
  editing, closed validation, conservative retention, exact internal deployment,
  fixed execution, MCP promotion request, Web-only approval, source-conflict
  protection, and mandatory verification.
- Replay closeout: D-0277 centralizes lifecycle-aware request retention; D-0278
  requires coherent terminal evidence and bounded scalar promotion references;
  D-0279 makes exact journal evidence authoritative. Unknown, malformed,
  ambiguous or contradictory evidence retains identity and consumes capacity.
- Registry and boundary: the production catalog is exactly fifteen tools and the
  Phase 12 permissions are exactly `workspace.inspect`, `workspace.edit`,
  `workspace.deploy`, `development.execute`, and
  `development.request_promotion`. Phase 11 identity and transports remain
  frozen. Shell, Git/GitHub, repository/ZIP publication, arbitrary filesystem,
  command/test or add-on execution, generic JSON-RPC, Docker/raw broker,
  external targets and production mutation remain absent.
- Next boundary: Phase 13 may begin with external-development-target architecture
  review. LAN development and production Kodi testing were not part of this
  closeout; production remains deferred until production-policy safeguards are
  explicitly in scope.

## D-0280: Phase 13 begins with external-development registration only
- Status: accepted Phase 13 Slice 1.
- Decision: Slice 1 adds Web-managed `external_development` targets and native
  read-only verification only. `external_production` is not representable in
  the closed target type, and no MCP, bridge, screenshot, deployment,
  execution, promotion, lifecycle or production authority is added. Phase 13
  overall remains PARTIAL.

## D-0281: External Kodi is a one-to-one target subtype
- Status: accepted Phase 13 Slice 1.
- Decision: the existing `targets` row remains the stable UUID identity.
  `external_kodi_targets` stores only endpoint configuration, opaque secret
  reference, closed capabilities and separate administrative, verification and
  observation state. Durable observations and content-free events preserve
  evidence without creating a parallel identity system.

## D-0282: Registration and trust are Web administrator decisions
- Status: accepted Phase 13 Slice 1.
- Decision: authenticated, same-origin, CSRF-protected Web administration alone
  may register, probe, confirm, enable, disable, edit, rotate credentials or
  deactivate an external target. Registration starts disabled and unverified;
  successful probe records evidence but explicit confirmation is required
  before enable. MCP bearer credentials have no equivalent route or tool.

## D-0283: Slice 1 endpoints are structured private-LAN IP literals
- Status: accepted Phase 13 Slice 1.
- Decision: configuration is protocol enum, canonical IP literal and a port
  from the server allowlist. The server owns `POST /jsonrpc`, disables redirects
  and environment proxies, and accepts only RFC1918 IPv4 or explicitly
  configured IPv6 ULA. Loopback is test-only; public, metadata, link-local,
  multicast, unspecified, reserved, documentation, benchmark, CGNAT, mapped
  IPv6 and managed-lab addresses fail closed. Hostname/DNS support is deferred,
  so no DNS rebinding path exists in Slice 1.

## D-0284: Native verification proves endpoint observations, not machine identity
- Status: accepted Phase 13 Slice 1.
- Decision: the verifier calls only fixed read-only `JSONRPC.Ping`,
  `JSONRPC.Version` and `Application.GetProperties` requests with bounded time,
  body and JSON shape. Evidence is the exact endpoint address, Kodi name and
  supported version, JSON-RPC compatibility and observation time. Endpoint,
  credential or important identity change requires re-verification; native
  evidence is not a strong machine identity and bridge identity is future work.

## D-0285: Recoverable credentials live in protected AEAD files
- Status: accepted Phase 13 Slice 1.
- Decision: SQLite contains only a random UUID reference. A deployment-provided
  256-bit base64url key encrypts bounded credentials with AES-256-GCM and bound
  target/reference/version associated data in UUID-named no-follow files under
  a `0700` directory with `0600` files and atomic publication. Missing or wrong
  keys fail closed. Rotation publishes a new reference before retiring the old;
  deactivation retires the active secret. Secrets never enter status or audit.

## D-0286: Configured capability is a closed maximum, not authority
- Status: accepted Phase 13 Slice 1.
- Decision: the only stored maximum-capability vocabulary is
  `target_inspect`, `addon_inventory`, `semantic_remote`, `screenshot`,
  `workspace_deployment` and `fixed_execution`. Native Slice 1 verification can
  establish only `target_inspect`; configuration or observation grants no MCP
  authority. Effective use requires future class eligibility and authorization
  in addition to configured and verified capability intersection.

## D-0287: Existing semantic tools use one target-adapter boundary
- Status: accepted Phase 13 Slice 2.
- Decision: `target_list`, `target_status`, `addon_list` and `remote_action`
  admit verified `external_development` targets through one application-owned
  `KodiTargetAdapterService`. It selects the existing managed adapter or the
  fixed external native adapter below MCP registry and authorization. Tool
  handlers contain no caller-selectable protocol or external special case.
  Screenshot, workspace deployment/execution, promotion, bridge, lifecycle,
  raw JSON-RPC and production remain internal-only or absent. The catalog and
  every tool schema remain exactly the existing fifteen.

## D-0288: External capability is configured maximum intersected with proof
- Status: accepted Phase 13 Slice 2.
- Decision: effective external authority is valid credential, enabled client,
  tool permission, exact target grant, target preset policy, external-
  development class, enabled and verified state, current identity, configured
  maximum and verified technical capability. No dimension substitutes for
  another. `addon_inventory` is proved by one bounded read-only
  `Addons.GetAddons` page. `semantic_remote` is proved without mutation by
  fixed per-method `JSONRPC.Introspect` requests covering the complete closed
  semantic mapping; Kodi version alone is never proof. MCP cannot establish or
  confirm capability. Listing uses stored confirmed evidence, while status and
  inventory recheck identity on demand and remote control always rechecks
  identity immediately before dispatch.

## D-0289: External remote control retains the Phase 11 replay barrier
- Status: accepted Phase 13 Slice 2.
- Decision: external control uses the unchanged client/request UUID receipt as
  a durable pre-dispatch marker and the unchanged closed action vocabulary.
  Exact terminal duplicates replay; changed parameters conflict; pending,
  post-send transport loss and malformed/lost acknowledgements are ambiguous
  and never redispatched. Current client permission, grant, target policy,
  class/state and capability are re-authorized after the fresh identity check
  and immediately before mutation. A connect failure that proves no connection
  was made is a definite pre-send failure, while any failure after bytes may
  have reached Kodi is conservative ambiguity. One operation per external
  target and a bounded global native-operation pool prevent slow targets from
  consuming unbounded workers.

## D-0290: The bridge is an optional narrow semantic extension
- Status: accepted Phase 13 Slice 3.
- Decision: a verified external-development target may add one separately
  configured and verified bridge without changing native target trust. Protocol
  version 1 exposes exactly `bridge_identity`, `bridge_capabilities` and
  `capture_screenshot`, plus the possession-proof pairing exchange. It has no
  filesystem, log, generic JSON-RPC, add-on administration, deployment,
  execution, shell, restart, profile or lifecycle operation. Native-only Slice
  2 tools neither require nor reveal bridge pairing. Production remains absent.

## D-0291: Bridge pairing is Web-only and pins persistent instance identity
- Status: accepted Phase 13 Slice 3.
- Decision: each bridge generates one persistent random UUID locally. An
  authenticated Web administrator creates a random 256-bit, target-bound,
  purpose-bound token with a ten-minute lifetime and sees it once; the token is
  entered locally in Kodi. The bootstrap request first proves Lab knows the
  token, then the bridge proves possession; only then do both sides derive a
  separate long-lived request secret and Kodi clears the visible token. This
  prevents unauthenticated remote enrollment or token consumption. Kodi Lab
  observes UUID, protocol, add-on ID/version, Kodi version, closed
  capability and build fingerprint, then requires explicit Web confirmation
  before pinning and activation. Rotation or revocation retires every prior Lab
  secret. MCP cannot begin, confirm, rotate, inspect a token or revoke pairing.

## D-0292: Bridge requests and responses are mutually authenticated and replay bounded
- Status: accepted Phase 13 Slice 3; capacity behavior refined by D-0302.
- Decision: HMAC-SHA-256 subkeys with versioned request/response labels cover
  canonical target UUID, bridge UUID, protocol, operation, request UUID,
  timestamp, nonce and payload digest. Responses additionally cover status and
  returned payload digest. The clock window is sixty seconds but never replaces
  nonce/request uniqueness. The add-on durably retains at most 256 request
  identities for 24 hours and caches four exact responses for 120 seconds;
  conflicting or repeated identity cannot re-execute. Lab receipts are capped
  at 256/target and 4,096 globally for 24 hours, prune only expired terminal
  rows, and retain pending/ambiguous identity. Pairing is at most one/target and
  two globally; screenshots are one/target and four globally.

## D-0293: External screenshots are authenticated bounded bytes, never remote paths
- Status: superseded by D-0299 during Phase 13 Slice 5.
- Decision: the unchanged `screenshot_capture` schema can admit an external-
  development target only through configured screenshot maximum, native trust,
  exact MCP authority and a paired, reachable, identity-pinned bridge with
  verified `screenshot` capability. The add-on uses Kodi RenderCapture at the
  server-owned 1280x720 geometry, converts BGRA to one non-interlaced RGB PNG in
  memory and returns bytes directly. Kodi Lab accepts only the existing strict
  PNG shape and 8 MiB maximum and persists no image. Request loss retries the
  exact request UUID/nonce once so the bridge returns the cached result or a
  bounded ambiguity; it never silently creates a second capture. HTTP HMAC
  authenticates integrity but screenshots remain cleartext on the LAN; HTTPS
  retains normal certificate validation and has no `verify=false` mode.

## D-0294: Bridge deployment is pathless, content-addressed and development-only
- Status: accepted Phase 13 Slice 4.
- Decision: protocol 1 additively advertises `workspace_deployment` and exposes
  only exact add-on inspection, deployment begin, referenced chunk transfer,
  commit and receipt lookup. Lab supplies a canonical bounded manifest and
  hashes but no destination. The bridge derives `special://home/addons/<id>`
  from the verified descriptor ID, stages regular files only, excludes VCS and
  bytecode caches, verifies the complete tree, and admits only one copied
  Python video plugin on an approved `external_development` target. There is no
  generic upload, filesystem, install, URL, shell, restart or lifecycle API.

## D-0295: External deployment reuses Phase 12 replay and evidence
- Status: accepted Phase 13 Slice 4.
- Decision: `workspace_deploy` keeps its four existing arguments and its Phase
  12 client/request map, workspace ownership, validation, rate, heavy lease and
  target grant. The generalized deployment row records either exact managed
  runtime identity or exact bridge identity/receipt, never both. The bridge
  independently binds deployment/request UUID, manifest and chunks; exact
  replay returns one receipt and conflicting reuse fails. Staging transfer may
  resume idempotently, but commit is never redispatched after uncertainty.

## D-0296: External baseline provenance is Web-established and drift revokes authority
- Status: accepted Phase 13 Slice 4.
- Decision: before first deployment, Web compares an approved
  DevelopmentSource capture with the bridge's current add-on ID, version,
  manifest digest, file count and bytes. MCP cannot establish or re-baseline
  this association. Every later deployment/execution proves the expected last
  tree; external change marks the association stale and blocks mutation rather
  than adopting it. Disablement, deactivation, bridge revocation or capability
  drift blocks new work while durable evidence remains.

## D-0297: Fixed external execution remains native and closed
- Status: accepted Phase 13 Slice 4.
- Decision: the bridge has no execution capability or JSON-RPC proxy. Native
  verification proves the configured `fixed_execution` maximum by exact
  `Files.GetDirectory` introspection; after exact bridge deployment, the target
  adapter invokes only the existing Phase 12 video-plugin directory smoke.
  Pre/post inventory and bridge-tree evidence bind the same add-on, version,
  target, workspace and generation. The existing pre-dispatch marker and
  no-post-dispatch-retry ambiguity rule are unchanged.

## D-0298: External ambiguity is Web-reconciled from durable bridge receipts
- Status: accepted Phase 13 Slice 4.
- Decision: commit-response loss makes the Lab operation reconciliation-
  required and never causes replacement redispatch. A CSRF-protected Web-only
  action queries the pinned bridge by deployment UUID and accepts only a
  terminal receipt matching bridge, target, semantic request, manifest and
  counts. Missing, staging, contradictory or unprovable evidence stays
  ambiguous. Bridge operation metadata is bounded to 64 entries; abandoned
  staging bytes expire after 24 hours into retained failed receipts, terminal
  and unresolved receipt identity is not evicted to recover capacity.

## D-0299: External screenshots capture the Kodi GUI through one transient bridge-owned file
- Status: accepted Phase 13 Slice 5 remediation.
- Decision: Kodi `RenderCapture` exposes the active video renderer and therefore
  cannot implement the intended Home/settings/library/add-on GUI screenshot.
  The bridge instead invokes `TakeScreenshot(<bridge-owned-path>,sync)` with one
  unpredictable filename beneath its fixed `special://temp/kodi-lab-bridge`
  namespace. Neither MCP nor Lab supplies any path component. The bridge bounds
  the synchronous existence wait, rejects symlinks, reparse points, hardlinks
  and non-regular files, checks encoded size before bounded reading, validates
  and normalizes a bounded non-interlaced RGB/RGBA PNG, and deletes the exact
  transient file in `finally` before constructing a response. Cleanup failure
  returns an
  authenticated semantic failure and short-TTL startup/pre-capture cleanup
  scans at most 64 direct children and deletes at most 16 strict bridge capture
  names; it never recurses or touches unknown names. This fixed local lifecycle
  is not generic filesystem authority. Lab persists no screenshot bytes or
  native path. Exact request replay still returns the four-entry/120-second
  authenticated response cache and cannot invoke a second screenshot. Kodi
  19.5 through 21.3 honor `sync`; Kodi 22 ignores the removed option, so the
  same operation uses its strict deadline to retry only the exact candidate
  until a complete valid PNG is readable.

## D-0300: External installed-tree provenance excludes only bounded interpreter caches
- Status: accepted Phase 13 Slice 5 real-network remediation.
- Decision: Kodi 20.5 proved that native fixed execution can create
  `__pycache__` after an exact bridge deployment, making the former bridge
  readback reject its own expected runtime byproduct after a successful
  invocation. Incoming manifests and staged trees still reject every bytecode
  path. Installed-tree inspection alone skips an exact real `__pycache__`
  directory only when every direct child is a bounded single-link regular
  `.pyc` file. It never recurses into that directory and rejects symlinks,
  reparse-like entries where observable, nested directories, unknown files,
  direct `.pyc` paths and aggregate count/size excess. The bridge neither
  reads nor deletes excluded bytecode. All source files, descriptors and other
  destination additions remain in the exact digest, so this implements the
  D-0294 canonical exclusion without adding filesystem authority or silently
  adopting source drift.

## D-0301: Explicit external re-baseline supersedes older deployment lineage
- Status: accepted Phase 13 Slice 5 real-network remediation.
- Decision: drift makes the external DevelopmentSource baseline stale and
  blocks deployment, execution and promotion. Recovery requires the operator
  to restore the disposable target to the exact registered source and perform
  the existing authenticated Web baseline comparison. A successful Web
  baseline whose establishment time is newer than prior successful deployment
  completion becomes the new trust anchor; only a later successful deployment
  advances expected target provenance again. MCP cannot select, create or
  alter this ordering, and neither stale nor mismatched bytes are adopted.

## D-0302: Non-idempotent bridge replay identity survives bounded traffic
- Status: accepted Phase 13 formal-review remediation.
- Decision: the 256-entry durable bridge replay ledger must never evict an
  unexpired `capture_screenshot` request identity. If every entry is protected,
  new authenticated work fails closed before dispatch. Read-only identity,
  capability and inspection calls plus content-addressed deployment begin,
  chunk, commit and receipt calls may be displaced because their handlers
  independently make exact replay idempotent and reject conflicting payloads;
  this preserves large bounded transfers without permitting a second capture.
  Legacy three-field entries have no proven operation class and therefore stay
  protected until expiry. The screenshot reader also rejects multi-link files,
  and native semantic mutation/fixed execution now enforce their existing
  aggregate deadline; deadline expiry after possible dispatch is ambiguous and
  is never retried. Bridge version 1.1.7 and its build fingerprint identify the
  remediation without changing protocol 1, capabilities, MCP schema, tool
  count or database schema.

## Phase 13 formal completion record

- Status: **Phase 13 — Remote targets: COMPLETE**
- Review basis: independent formal review of implementation checkpoint
  `28d84c607598d28dd237da12056ac37485768c1d`, Alembic head
  `0046_external_bridge_development`, the exact fifteen-tool production
  catalog, retained Kodi 20.5 evidence and the D-0302 remediation.
- Accepted boundary: authenticated-Web registration, native trust lifecycle,
  exact MCP grant/preset/policy intersection, bounded native inspection and
  semantic control, optional authenticated GUI screenshot bridge, pathless
  external workspace deployment, fixed native execution, exact provenance and
  Web-reviewed promotion evidence for `external_development` only.
- Evidence boundary: live LAN proof is Kodi 20.5 only. Kodi 19.5, 21.3 and 22
  compatibility remains deterministic automated evidence. Direct SSH was
  out-of-band maintainer setup/recovery authority and is not a product
  capability.
- Deferred boundary: `external_production` is not representable in the database;
  production policy, approval, activation, deployment and lifecycle authority
  remain Phase 14 work. Phase 13 adds no shell, generic filesystem, arbitrary
  JSON-RPC, Docker/raw-broker, Git or host-lifecycle authority.

## D-0303: External production shares transport state but has a dedicated policy subtype
- Status: accepted Phase 14 Slice 1.
- Decision: `external_production` is a closed target class. It reuses the
  common target row and Phase 13 external endpoint, protected credential and
  native-observation subtype, but every completed production registration also
  has exactly one `external_production_policies` row. Production policy cannot
  attach to managed or external-development targets, and production targets
  fail closed when that subtype is absent.

## D-0304: Target class is immutable
- Status: accepted Phase 14 Slice 1.
- Decision: no application, repository or database path may reclassify a
  target. Development-to-production, production-to-development and
  managed/internal-to-production changes require deactivation followed by a
  new target UUID and trust ceremony.

## D-0305: Production designation is a separate Web confirmation
- Status: accepted Phase 14 Slice 1.
- Decision: native identity confirmation does not designate a production
  target. A second authenticated-Web ceremony explicitly acknowledges that
  the observed target is production and that future mutations may affect real
  user state. The policy binds the administrator, time, exact observation
  UUID/digest and policy revision. Probe establishes observation only.

## D-0306: Production excludes development deployment and execution authority
- Status: accepted Phase 14 foundation.
- Decision: production has no DevelopmentSource baseline, workspace deployment,
  workspace execution or promotion-request authority. Phase 14 Slice 1 adds no
  automatic or Web production deployment. Any future support-addon deployment
  requires a separate architecture review.

## D-0314: Identity generation invalidates production authority
- Status: accepted Phase 14 Slice 1.
- Decision: each production policy owns a monotonic `identity_generation`.
  Endpoint address, port, protocol, Kodi credential, observed native
  identity/version or material transport-trust changes increment it and
  invalidate designation. Reachability cannot automatically restore trust;
  native identity and production designation must be explicitly reconfirmed.
  Future grants and approvals must bind the exact generation.

## D-0316: Production remains private-IP-only without DNS, WAN or SSH authority
- Status: accepted Phase 14 Slice 1.
- Decision: production registration inherits the Phase 13 structured IP-literal,
  private-LAN, fixed-port policy and its rejection of public, loopback,
  link-local, multicast, reserved and managed-Lab networks. Clients use no DNS,
  redirects or ambient proxy. This creates no SSH, shell, arbitrary URL, raw
  JSON-RPC, generic filesystem or lifecycle authority.

## D-0319: Production mutation suspension is deny-only and epoch-bound
- Status: accepted Phase 14 Slice 1.
- Decision: a singleton Web-controlled `production_mutations_blocked` state is
  seeded blocked and owns a monotonic policy epoch. Every block/unblock change
  advances the epoch so future approvals cannot revive across a cycle.
  Production policy also has a per-target mutation suspension. Clearing either
  denial grants no capability, target grant or operation by itself.

## D-0321: Production workspaces, execution and MCP deployment are prohibited
- Status: accepted Phase 14 foundation.
- Decision: all MCP target and workspace-target authorization passes through
  one target-class veto beneath the transport layers. `external_production`
  is denied in Slice 1, including indirect workspace deployment, execution and
  promotion paths. The dormant Production Remote Kodi preset remains
  unassignable and ineffective.

## D-0322: Production preserves the zero-new-tool architecture
- Status: accepted Phase 14 foundation.
- Decision: Phase 14 adds no MCP tool. The registry remains exactly fifteen
  tools, and Slice 1 exposes production administration only through
  authenticated Web routes. Later capabilities, if accepted, must fit the
  existing semantic tool catalog and centralized production veto.

## D-0323: Production inspection authority is generation-bound
- Status: accepted Phase 14 Slice 2.
- Decision: `mcp_production_target_scopes` grants only the closed `inspect`
  scope to one exact client, production target and current positive identity
  generation. The ordinary client-target grant remains independently required.
  Revocation is terminal, regrant creates a new UUID, and old generations remain
  durable history but can never become effective again.

## D-0324: Production Inspection is a dedicated immutable preset
- Status: accepted Phase 14 Slice 2.
- Decision: production reads require the exact assignable built-in `Production
  Inspection` preset with only `target.inspect`, on both client and target.
  It cannot be cloned. Read-only Inspection, Add-on Development, Full Lab
  Control, custom presets and dormant Production Remote Kodi do not authorize
  production inspection.

## D-0325: Only three existing tools inspect production
- Status: accepted Phase 14 Slice 2.
- Decision: only `target_list`, `target_status` and `addon_list` admit
  `external_production`, and only as read-only production-eligible registry
  entries. The other twelve tools remain production-ineligible. No tool or
  MCP input/output schema is added; the catalog remains exactly fifteen.

## D-0326: Direct production reads require fresh native identity
- Status: accepted Phase 14 Slice 2.
- Decision: every direct production read performs `JSONRPC.Ping`,
  `JSONRPC.Version` and `Application.GetProperties(name,version)` before status
  or inventory is publishable. The complete durable authority intersection is
  re-evaluated after network work. Exact identity drift atomically records a
  bounded mismatch, disables the target, advances generation once, invalidates
  designation and suspends mutation authority without changing Kodi.

## D-0327: Production discovery is exact and private
- Status: accepted Phase 14 Slice 2.
- Decision: `target_list` uses confirmed stored trust only and omits every
  production target lacking the complete grant, scope, generation, preset,
  policy, state and capability intersection. It makes no network call and
  exposes no endpoint, authentication, secret, observation or bridge data and
  makes no fresh reachability claim.

## D-0328: Production reads have separate bounded admission and no retry
- Status: accepted Phase 14 Slice 2.
- Decision: target listing is limited to two requests per second per client
  with burst two; status to one per five seconds per client-target; and add-on
  inventory to one per thirty seconds per client-target. Maps retain at most
  1,024 idle-expiring server-UUID keys without active eviction. Native reads
  are one per target and four globally beneath the existing eight-operation
  external ceiling. Rejection is immediate and the server never retries.

## D-0329: Mutation vetoes do not govern pure inspection
- Status: accepted Phase 14 Slice 2.
- Decision: the global `production_mutations_blocked` switch and per-target
  mutation suspension remain mutation-only deny controls. They are deliberately
  absent from the production inspection evaluator. Target disablement,
  designation invalidation, reverification/mismatch state and stale generation
  still deny reads.

## D-0330: Production inspection administration is Web-only
- Status: accepted Phase 14 Slice 2.
- Decision: exact preset assignment, generic target grant, current-generation
  inspect-scope creation and terminal revocation use authenticated Web
  administration with Trusted Host, same-origin and CSRF enforcement. MCP gains
  no grant management, bridge, screenshot, control, approval, deployment,
  workspace or lifecycle administration.

## D-0331: Production bridge trust is Web-only and independently revision-bound
- Status: accepted Phase 14 Slice 3.
- Decision: production pairing, confirmation, reverification and revocation are
  authenticated-Web administration. Each confirmed production bridge owns a
  positive monotonic `production_bridge_trust_revision` independent of native
  `identity_generation`; bridge-only drift stales visual authority but not
  native inspection, while native generation drift invalidates both.

## D-0332: Only explicitly reviewed bridge builds are production eligible
- Status: accepted Phase 14 Slice 3.
- Decision: an active immutable reviewed-build record binds semantic
  fingerprint, exact ZIP SHA-256, add-on/protocol versions, declared
  capabilities, Kodi versions, source reference, reviewer and time. Migration
  seeds no approval; source constants and known 1.1.7 evidence are not trust.

## D-0333: Production pairing requires possession and installed-target confirmation
- Status: accepted Phase 14 Slice 3.
- Decision: the Phase 13 one-time possession exchange is followed by exact
  active-build matching and a separate explicit Web confirmation of installed
  target identity and reviewed provenance. Pairing alone grants no MCP scope.

## D-0334: Production visual authority uses separate inspect and visual scopes
- Status: accepted Phase 14 Slice 3.
- Decision: immutable `inspect` rows remain native-generation-only. A separate
  `visual` row additionally binds the current bridge revision and requires a
  current inspect row. Inspect revocation atomically revokes active dependent
  visual rows; stale or revoked history never becomes effective again.

## D-0335: Production Visual Inspection is the sole screenshot preset
- Status: accepted Phase 14 Slice 3.
- Decision: the immutable assignable, non-clonable production-only preset has
  exactly `target.inspect` and `target.visual`. Both client and target must use
  it for screenshots. Existing/custom/full-control/development/dormant presets
  cannot authorize production pixels.

## D-0336: Production screenshots require the complete native and bridge intersection
- Status: accepted Phase 14 Slice 3.
- Decision: `screenshot_capture` publishes production pixels only after exact
  credential/client/preset/grant/inspect/visual/generation/revision/target/
  bridge/review/capability/transport checks and a fresh post-capture durable
  authorization check. Revocation during capture discards bytes.

## D-0337: Private-LAN HTTP requires exact configuration-bound acknowledgement
- Status: accepted Phase 14 Slice 3.
- Decision: immutable acknowledgement history binds target, native generation,
  bridge revision, protocol, IP literal, native and bridge ports, endpoint
  digest, transport-policy revision, administrator and time. HTTPS uses normal
  certificate validation and needs no HTTP acknowledgement.

## D-0338: Production screenshot identity is durable and cannot silently recapture
- Status: accepted Phase 14 Slice 3.
- Decision: production requires a client-scoped UUID bound to target and
  canonical parameters. Lookup precedes dispatch admission. Exact live replay
  returns identical in-memory pixels; durable completion without live pixels
  returns `screenshot_replay_unavailable`. Pending/ambiguous identities and
  protected bridge replay entries are never evicted to admit new work.

## D-0339: Production bridge capability maximum is screenshot only
- Status: accepted Phase 14 Slice 3.
- Decision: production records all reviewed advertised capabilities but only
  `screenshot` can become verified production bridge authority. Advertised
  workspace deployment or future capabilities remain denied without weakening
  external-development behavior.

## D-0340: Production screenshot pixels are memory-only
- Status: accepted Phase 14 Slice 3.
- Decision: Kodi Lab persists no production pixels, base64 response, thumbnail
  or preview. Durable receipt/audit data is bounded metadata: UUIDs,
  generations/revisions, dimensions, encoded size, SHA-256, outcome and time.
  The Web warns that the receiving MCP client may persist pixels independently.

## D-0341: Native and bridge trust generations are independent
- Status: accepted Phase 14 Slice 3.
- Decision: bridge unavailability, drift or revocation denies only visual use;
  native target list/status/inventory remains available while native trust is
  coherent. Native-generation change deletes Lab bridge authority and stales
  inspect, visual and HTTP acknowledgement bindings.

## D-0342: Slice 3 adds no tool and exactly four of fifteen are production eligible
- Status: accepted Phase 14 Slice 3.
- Decision: only `target_list`, `target_status`, `addon_list` and the existing
  `screenshot_capture` admit production. `screenshot_capture` alone gains the
  optional UUID schema field, required only for production. The other eleven,
  including `remote_action`, remain denied across HTTP, stdio and compatibility
  HTTP.

## D-0343: Production Remote Kodi is the exact production control maximum
- Status: accepted Phase 14 Slice 4A.
- Decision: built-in UUID `11000000-0000-4000-8000-000000000004` is immutable,
  assignable, non-clonable, production-only and has exactly `target.inspect`,
  `target.visual` and `target.control`. It is a maximum, not a grant; generic
  grant and independent inspect, visual and navigate scopes remain mandatory.

## D-0344: Production action taxonomy is closed
- Status: accepted Phase 14 Slice 4A.
- Decision: `up`, `down`, `left`, `right`, `back`, `home` and `info` are standing
  navigation. `play_pause`, `stop`, `previous`, `next`, `volume_up`,
  `volume_down` and `mute_toggle` are future approval actions and are hard
  denied in 4A. `context_menu` and `select` are prohibited in V1 production.
  External development retains all sixteen existing semantic actions.

## D-0345: Navigate authority binds inspect, native generation and bridge revision
- Status: accepted Phase 14 Slice 4A.
- Decision: immutable `navigate` requires active current `inspect` and binds
  exact client, target, native generation and reviewed bridge trust revision.
  Inspect revocation atomically revokes navigate dependents. Revoked or stale
  rows never revive; regrant uses a new UUID.

## D-0346: Production remote eligibility is action-aware and server-owned
- Status: accepted Phase 14 Slice 4A.
- Decision: `remote_action` is production-eligible in the shared registry, but
  centralized semantic policy beneath it permits only D-0344 navigation before
  adapter dispatch. Transports cannot select a method or fork policy, and the
  adapter sends only fixed mappings.

## D-0347: Both production mutation vetoes govern navigation
- Status: accepted Phase 14 Slice 4A.
- Decision: new work checks the global mutation block and per-target suspension
  before receipt claim and immediately before native send. Pre-send change is
  terminal with no mutation; possible post-send loss is ambiguous and never
  retried. Same-client terminal historical replay remains retrievable.

## D-0348: Production receipts durably bind governing authority
- Status: accepted Phase 14 Slice 4A.
- Decision: the Phase 11 base receipt remains the replay identity and gains a
  one-to-one immutable production binding for operation class, native/bridge/
  policy/mutation revisions, scopes, presets, grant and applicable HTTP
  acknowledgement. Required bounded audit is transactional with claim.
  Pending, ambiguous or incoherent evidence is protected and capacity never
  evicts it. Only coherent completed base, binding and audit rows expire
  atomically after the Phase 11 30-day replay window.

## D-0349: Native HTTP production control requires separate exact consent
- Status: accepted Phase 14 Slice 4A.
- Decision: native HTTP control acknowledgement binds target, native generation,
  protocol, literal IP, native port, endpoint digest and production/transport
  policy revisions. It states bridge HMAC does not protect native JSON-RPC and
  HTTP adds no confidentiality or integrity. HTTPS uses normal certificate
  validation and has no acknowledgement or bypass.

## D-0350: Seven transient production actions require one-shot Web approval
- Status: accepted Phase 14 Slice 4B.
- Decision: `play_pause`, `stop`, `previous`, `next`, `volume_up`,
  `volume_down` and `mute_toggle` require an exact request-bound Web approval.
  MCP may request and consume that one operation but has no queue, approval,
  denial, cancellation or grant-management authority. `context_menu` and
  `select` remain prohibited and no approval can override that classification.

## D-0351: Approval consumption and the Phase 11 receipt claim are atomic
- Status: accepted Phase 14 Slice 4B.
- Decision: approved-state consumption, base remote receipt, immutable
  production binding, bounded dispatch audit, consumed transition event and
  durable rate state commit in one SQLite transaction before native send. A
  rollback leaves the approval retryable; a committed pending receipt is the
  permanent replay barrier even if the process dies before send.

## D-0352: Production navigation uses lower immediate admission limits
- Status: accepted Phase 14 Slice 4A.
- Decision: navigation is two actions per second per durable client-target with
  burst three, one mutation in flight per target and four globally beneath the
  existing eight external-native operations. Admission is immediate, queues do
  not grow and replay lookup precedes new-work charging.

## D-0353: Slice 4A and Slice 4B are independently audited boundaries
- Status: accepted Phase 14 Slice 4A.
- Decision: 4A contains only standing navigation. 4B is a separate architecture,
  implementation and audit boundary for transient approvals. Completion of 4A
  alone grants no playback, volume, approval, deployment or lifecycle authority.

## D-0354: Transient request scope is immutable and depends only on inspect
- Status: accepted Phase 14 Slice 4B.
- Decision: `request_transient_control` binds exact client, production target,
  native generation and reviewed bridge revision. Creation and use require an
  active current inspect scope, exact Production Remote Kodi maxima and generic
  grant; navigate and visual are not prerequisites. Inspect revocation
  atomically revokes all active visual, navigate and transient-request rows.

## D-0355: Client authority revision covers enablement and preset assignment
- Status: accepted Phase 14 Slice 4B.
- Decision: positive monotonic `mcp_clients.authority_revision` advances only
  for enabled-state or preset changes. Credential, immutable generic grant and
  production-scope identity are bound directly. Generic target grants receive
  immutable UUIDs recorded in a permanent one-use identity ledger; direct
  delete/reinsert and normal remove/regrant can never reuse an old identity or
  revive an old approval.

## D-0356: Approval deadlines are bounded and terminal states cannot reopen
- Status: accepted Phase 14 Slice 4B.
- Decision: pending lifetime is at most fifteen minutes and approval opens at
  most five minutes from the Web decision. Configuration can shorten but no
  retry, preflight failure, outage or revisit extends either deadline. Denied,
  cancelled, expired and consumed rows are terminal.

## D-0357: Policy change invalidates approval; temporary reachability does not
- Status: accepted Phase 14 Slice 4B.
- Decision: exact credential/client/preset/grant/scope/native/bridge/build/
  capability/acknowledgement/policy/epoch changes cancel live approval with a
  closed semantic reason. A temporary bridge or native reachability failure
before claim leaves an approved row intact until its original deadline, and a
fresh exact authenticated bridge retry may restore reachability. Web mutation
block, target suspension, disablement and deactivation cancel affected live
approvals atomically with the governing revision transition.

## D-0358: Post-commit/pre-send loss is accepted at-most-once behavior
- Status: accepted Phase 14 Slice 4B.
- Decision: after the atomic claim, no code path reopens or redispatches the
  consumed approval. Crash before claim is retryable; crash after commit may
  mean the approved action never occurs. Possible post-send response loss is
  ambiguous. There is no recovery worker or deferred dispatcher.

## D-0359: Approval retention protects uncertain and incoherent evidence
- Status: accepted Phase 14 Slice 4B.
- Decision: pending/approved approvals, consumed approvals linked to pending or
  ambiguous receipts, and malformed, missing or mismatched approval/receipt/
  audit relationships remain protected and capacity-consuming. Only coherent
  terminal bundles older than policy retention are deleted atomically.

## D-0360: The implemented fifteen-tool catalog is the V1 contract
- Status: accepted Phase 14 validation preparation.
- Decision: V1 has exactly `target_list`, `target_status`, `addon_list`,
  `screenshot_capture`, `remote_action`, `development_source_list`,
  `workspace_create`, `workspace_status`, `workspace_tree`, `workspace_read`,
  `workspace_edit`, `workspace_validate`, `workspace_deploy`,
  `workspace_execute` and `promotion_request`. Exactly the first five named
  target tools are production-eligible. Earlier candidate logs, diagnostics,
  library, raw JSON-RPC, installer or lifecycle tools are not implied V1 gaps.
  Production gains no deployment tool.

## D-0361: Production bridge installation stays operator-owned
- Status: accepted Phase 14 validation preparation.
- Decision: support-addon deployment is deferred post-V1 and is not a Phase 14
  completion blocker. V1 bootstrap is manual operator installation of one exact
  reviewed release ZIP after published checksum verification. Kodi Lab owns
  reviewed-build registration, possession pairing, explicit provenance
  confirmation and revision-bound authority, but not installation, Kodi/host
  lifecycle or automatic upgrades. Any updater/deployer requires a new
  architecture and security gate; generic production deployment remains
  rejected.
- Rationale: installation establishes bytes under operator authority while the
  separate Lab trust ceremony proves only the exact installed identity it is
  permitted to use. Combining them would make the identity/screenshot bridge a
  generic software-management channel.

## D-0362: Corrected production bridge metadata requires a reviewed successor
- Status: accepted; packaging successor implemented and locally reviewed,
  publication and production use deferred.
- Decision: deterministic 1.1.7 includes both its README and `addon.xml` in the
  exact ZIP digest. Those packaged files describe external-development-only use
  and the README explicitly rejects production. Correcting either changes the
  ZIP SHA-256. Wording alone does not change the hard-coded semantic fingerprint,
  but reviewed production identity binds both values, so old hashes cannot be
  claimed for modified bytes. Keep 1.1.7 as historical Phase 13 evidence and
  require a separately authorized, independently reviewed packaging successor
  before production Stage B. The minimal recommended transition is 1.1.8 with
  synchronized descriptor/code version, recomputed semantic fingerprint and
  deterministic ZIP checksum, fresh review and a new reviewed-build record.

## D-0363: Compose and the first public SemVer belong to Phase 16
- Status: accepted Phase 14 validation preparation.
- Decision: no Compose definition or superseding documented public install
  mechanism exists at this checkpoint. Clean public installation remains a V1
  release acceptance item owned by Phase 16, not a Phase 14 closeout blocker.
  Static package version `0.0.0` remains the unreleased-development sentinel;
  Phase 16 owns selecting and validating `1.0.0`, public artifacts, checksums,
  installation docs and the release checklist.

## D-0364: Bridge 1.1.8 is the packaging-only production-validation candidate
- Status: accepted packaging/review slice; live registration and validation
  deferred.
- Decision: 1.1.8 corrects only packaged operator documentation and metadata,
  plus the synchronized add-on/fingerprint version identity. Runtime source,
  after normalizing the two version literals, is byte-identical to reviewed
  1.1.7. Protocol 1, the fixed endpoint, pairing/HMAC/replay behavior, exact
  operations, screenshot safety, persistent bridge UUID and advertised
  `screenshot` plus `workspace_deployment` capabilities are unchanged.
  Production uses only screenshot; workspace deployment remains restricted to
  external development.
- Identity: deterministic builder `kodi-lab-narrow-bridge-zip-v1` produces
  `service.kodi_lab_bridge-1.1.8.zip` with ZIP SHA-256
  `20a14873a46969cd3f1f962875a3905caa56ce6f088e95f6d3b2b8de6e666762`
  and semantic fingerprint
  `4a055b9479aeb72d2f8fe16d317ed525f69991908b28580d8a071be33aa26e5a`.
  The version-bearing semantic identity deliberately changes; README and
  descriptor wording are ZIP inputs but are not fingerprint inputs.
- Boundary: no live reviewed-build record, install, publication, tag, LAN or
  production contact, Stage A-D execution, deployment/update authority or
  Phase 15/16 work is authorized by this decision. Phase 14 remains PARTIAL.

## D-0365: One material production transition advances policy revision once
- Status: accepted Phase 14 Stage A remediation.
- Decision: one application transaction that publishes or changes coherent
  production native/policy state advances `policy_revision` exactly once,
  regardless of how many fields in that aggregate transition change. A
  successful reverification observation moves `invalidated` to
  `pending_native_identity` without advancing `identity_generation`; the
  generation already advanced when credential or endpoint trust changed.
  Repeated successful observations each replace the pending observation and
  therefore each advance revision once. Failed probes that change no durable
  trust/policy field do not advance it. Deactivation and its preset-authority
  cleanup are one coherent transition and therefore also advance only once.
- Enforcement: migration `0052_production_policy_revision_binding` binds every
  material production native-row update to the already-advanced policy
  revision, requires policy material updates to advance by exactly one and
  retains a non-deletable, non-replaceable high-water mark so native/policy
  delete/reinsert and SQLite conflict replacement cannot restore an earlier
  revision. The database guards remain authoritative.

## D-0366: Production screenshot maximum amendment is explicit and Web-only
- Status: accepted and implemented as the bounded Phase 14 remediation.
- Decision: `configured_capabilities` is the operator-approved closed semantic
  maximum, not native or bridge proof. Authenticated Web administration may add
  or remove only literal `screenshot` with an expected complete set, expected
  policy revision and exact human confirmation. Native verified capabilities
  do not change, bridge advertisement cannot enlarge the maximum, MCP has no
  administration route, and the fifteen-tool/five-production-tool boundaries
  remain unchanged. A database guard requires every material production
  configured-set change to match the immutable closed amendment history.
- Addition: advance the production policy once, bind the native row and
  high-water mark to that result, retain identity generation and accepted native
  inspection, and create no bridge, visual, acknowledgement or screenshot
  authority.
- Removal: atomically revoke or permanently stale bridge trust, pairing
  material, visual/navigate/transient-request scopes and live transient
  approvals while retaining receipts, acknowledgements, grants, native inspect
  scope, preset, enablement, reviewed-build registration and immutable history.
  A monotonic bridge tombstone and database removal guard prevent remove/re-add
  resurrection. Post-commit secret-file residue has no authoritative database
  reference.
- Future onboarding may suggest role-based maxima but may never silently
  approve them; Phase 16 onboarding remains deferred.

## D-0367: Screenshot validity is distinct from bridge reachability
- Status: accepted Phase 14 Lab-side remediation.
- Decision: an authenticated screenshot operation failure or a returned payload
  rejected by the existing bounded MIME, size, PNG or dimension validators
  rejects the screenshot and its receipts without, by itself, changing an
  otherwise current paired bridge from `reachable` to `unreachable`. No pixels
  publish and the request is not retried. Connection, timeout and transport
  failures retain the existing unreachable transition; authentication,
  identity and protocol failures retain their existing distinct fail-closed
  classifications. This changes neither screenshot validation nor bridge
  pairing, reviewed-build, acknowledgement, scope or revision semantics.

## Phase 14 Slice 2 formal completion record

- Status: **Phase 14 Slice 2 — Read-only production inspection: COMPLETE**;
  Phase 14 remains **PARTIAL**.
- Review basis: independent formal review of implementation checkpoint
  `1188562a64428af45c83ea325f496bb7fac4c5b9`, Alembic head
  `0048_production_inspection_scopes`, the exact fifteen-tool catalog and
  deterministic local/mock evidence only.
- Remediation: the review reproduced that a malformed identity-generation
  decrement could make retained old scope history current again. Migration
  `0048` now rejects generation decreases, preserving D-0323's permanent stale
  generation boundary; focused migration and production/transport/workspace
  regressions and the final authoritative gate passed.
- Deferred boundary: no bridge, screenshot, control, approval, support-addon
  deployment, workspace, lifecycle or other production authority was added.
  No real Kodi or LAN host was contacted, and Slice 3 was not begun.

## Phase 14 Slice 3 formal completion record

- Status: **Phase 14 Slice 3 — Production bridge and screenshots: COMPLETE**;
  Phase 14 remains **PARTIAL**.
- Review basis: independent formal review of implementation checkpoint
  `7cefda8c151b2334741883e784970f86886be3bb`, Alembic head
  `0049_production_bridge_visual`, the exact fifteen-tool catalog, bridge 1.1.7
  package/source identity and deterministic local/mock evidence only.
- Remediation: the review reproduced four fail-closed defects. Migration `0049`
  now forbids deletion or replacement of a production bridge revision row and
  forbids two clients from sharing one target/bridge replay UUID. The service
  derives a separate durable bridge request UUID per client receipt, discards
  volatile pixels if the required production metadata commit fails, and retires
  a failed production show-once pairing token. Repeated bridge invalidation is
  revision-idempotent once authority is already suspended.
- Validation: focused provenance/pairing/revision/scope/replay/privacy,
  production-inspection, Phase 13 compatibility, HTTP/stdio/compatibility,
  migration and bridge-backend suites passed before the final authoritative
  repository gate.
- Deferred boundary: no production remote control, approval, support-addon
  deployment, workspace, execution, lifecycle or other production authority
  was added. No real Kodi or LAN host was contacted, and Slice 4 was not begun.

## Phase 14 Slice 4A formal completion record

- Status: **Phase 14 Slice 4A — Constrained production navigation: COMPLETE**;
  Phase 14 remains **PARTIAL**.
- Basis: migration `0050_production_navigation`, exact fifteen-tool catalog,
  action-aware five-tool production registry, deterministic local/mock native
  and bridge evidence, protected receipt/audit proof and authoritative repository
  validation.
- Independent remediation: formal audit of implementation checkpoint
  `f5c4f5e8756043ba27a10019ee8a1bb86d42fdf7` found that retention permanently
  protected completed production bindings while generic cleanup was not
  terminal-state-aware. Cleanup now expires only coherent completed base,
  binding and audit evidence atomically after 30 days; pending, ambiguous,
  missing and mismatched evidence remains protected and capacity-consuming.
- Boundary: production permits exactly seven standing navigation actions. The
  seven transient actions are hard denied, `context_menu`/`select` remain
  prohibited, and no approval, deployment, execution, lifecycle, shell,
  filesystem, SSH, arbitrary JSON-RPC or support-add-on authority was added.
- Isolation: no real Kodi or LAN host was contacted and Slice 4B was not begun.

## Phase 14 Slice 4B implementation completion record

- Status: **Phase 14 Slice 4B — One-shot transient production approvals:
  COMPLETE**; Phase 14 remains **PARTIAL**.
- Basis: migration `0051_production_transient_approvals`, the unchanged exact
  fifteen-tool catalog, additive `remote_action` approval results, Web-only
  decisions, exact authority/revision bindings, atomic receipt claim and
  deterministic local/mock lifecycle, replay, migration and isolation proof.
- Boundary: production gains only the seven closed transient semantic actions
  after exact one-shot approval. Standing navigation and external-development
  behavior are unchanged; `context_menu`/`select`, deployment, execution,
  lifecycle, shell, filesystem, SSH, arbitrary JSON-RPC and support-addon
  deployment remain unavailable.
- Isolation: no real Kodi or LAN host was contacted and no later Phase 14 work
  was begun.

## Phase 14 Slice 4B formal completion record

- Status: **Phase 14 Slice 4B — One-shot transient production approvals:
  COMPLETE**; Phase 14 remains **PARTIAL**.
- Review basis: independent formal review of implementation checkpoint
  `579d377e0e2a4e56b9baaddf1c8d02f74c06005e`, Alembic head
  `0051_production_transient_approvals`, the unchanged exact fifteen-tool
  catalog and deterministic local/disposable evidence only.
- Independent remediation: the audit reproduced direct reuse of a removed
  generic grant UUID, deletion/recreation of the global mutation epoch, stale
  target-policy revision paths, bridge unreachability that could not recover,
  non-atomic live-approval invalidation and insufficient malformed retention/
  approval-receipt relationship guards. Completion remediation adds a permanent
  grant-identity ledger, monotonic direct-state guards, atomic Web cancellation,
  exact authenticated reachability recovery, one-to-one durable approval/
  receipt/audit constraints and stricter coherent cleanup.
- Validation: deterministic tests cover direct-state resurrection, temporary
  recovery, every atomic-claim rollback point including commit, concurrent
  consumption, immediate post-commit veto, post-commit loss, replay, retention,
  migration preservation and unchanged transport/workspace boundaries; the
  authoritative final repository gate passed on final content.
- Boundary: no production host or LAN contact, Stage D, support-addon
  deployment, later Phase 14 work, publication, tag or remote was used or
  begun.
## D-0368: Remote container state changes are explicit and backup-bound

- Scope: the wheel-installed, single-instance remote Web container only. This
  decision does not add broker, managed Kodi, bridge, or production authority.
- `compose up` validates private state and exact installed Alembic head, then
  starts Web under an exclusive state lock. It never creates a key or advances a
  schema. The V1 specification's automatic startup migration remains a future
  release question; this development deployment uses explicit commands because
  encrypted files and the master key must remain coherent with SQLite.
- One explicit `initialize` accepts an empty private volume and publishes a
  format marker after key creation and fresh migration. A complete Phase 1
  volume is adopted through explicit, backed-up `upgrade` before new-image Web
  startup; a partial volume fails closed. Administrator password setup remains
  prompted and separate.
- A stopped Web process is required for backup, upgrade, and restore. A single
  advisory lock coordinates Phase 2 Web and CLI processes; Phase 1 Web did not
  participate, so operators must stop that image before upgrading its volume.
- The backup is a private, manifest-verified directory inside the same named
  state volume. The SQLite online backup API produces a standalone database;
  the exact master key, encrypted secret trees, and all other durable data-root
  content accompany it. Its key digest is an identifier, not key material.
- Upgrade binds one pre-upgrade backup ID to an unfinished marker before
  migration and a success receipt after head verification. An unfinished marker
  blocks Web. Rollback requires that exact backup ID and a repeated confirmation,
  marks restore in progress, quarantines newer state, restores and verifies the
  prior DB/key set, then
  starts the matching previous application artifact. Image rollback alone is
  insufficient. There are no downgrade migrations or automatic retention.

## D-0369: Linux V1 broker container deployment foundation

- Decision gate: **A, accept broker container for Linux V1** on the isolated
  native-Linux evidence in `BROKER-CONTAINER.md`. The selected broker runs from
  the same installed wheel as Web in a separate Compose `lab` profile. The
  remote-only profile continues without a broker.
- This supersedes D-0024's planned Linux V1 host systemd unit while preserving
  its long-running broker and broker-created Unix socket semantics.
- Only the root broker container mounts the host Docker socket. It has no
  privileged mode, host PID/network namespace, or broad host filesystem mount.
  It drops all capabilities and restores only CHOWN, DAC_OVERRIDE, and FOWNER
  for the existing root-owned socket/artifact and private-state contracts.
  Docker socket possession is daemon-level authority, not a Docker sandbox.
- Web is UID/GID 10001 and receives only the private broker-socket volume,
  read-only. The broker's exact-UID `SO_PEERCRED` check remains mandatory. A
  container with both this volume and UID 10001 can impersonate Web; deployment
  control of that volume is therefore part of the trusted Docker-admin boundary.
  Unrelated containers lack the volume, and Web/Kodi receive no Docker socket.
- Broker and Web share the one Phase 2 data volume for trusted SQLite and fixed
  artifacts. The current Phase 2 backup rejects intentionally root-owned
  snapshot files; mixed-owner managed-state backup/restore is a separate gate
  before managed lab use or release. Same-volume backups still do not protect
  against volume loss; off-volume export/recovery remains required before
  public/production release.
- No host broker is installed. Docker Desktop and non-Linux peer/ownership
  behavior remain unaccepted until tested on those platforms.

## D-0370: Application rollback and managed artifact recovery are distinct

- This decision replaces D-0368's whole-data-root backup-set language and
  resolves the mixed-owner recovery gate left by D-0369.
- Phase 3.5 retains the accepted single private state volume and separate
  broker-socket volume. Numeric Web identity is `10001:10001`; broker-published
  snapshots stay `0:10001`, with `0750` directories and `0440` files. The
  broker still reads trusted SQLite and application artifacts; a separate
  snapshot volume would add migration without removing its Docker authority.
- Phase 2 application rollback now inventories only classified Web-owned
  durable state. It records and verifies numeric owner/group and safe modes,
  including read-only ZIP artifacts, plus hashes and SQLite/key identity.
  Root-owned snapshots, screenshots, and stages are excluded. Restore moves
  only application state to quarantine and leaves snapshots root-owned. The
  manifest records published snapshot hashes as external dependencies, and
  rollback refuses a changed snapshot set before mutation.
- Installation recovery is a separate stopped-state procedure: copy a verified
  application backup and published snapshot UUID artifacts off the volume,
  then reconstruct their fixed ownership on a genuinely new volume. The
  `recover-clean` command restores the application set without relying on a
  lost upgrade receipt; it writes a fail-closed pending marker. Published
  snapshot inventory requires exact root ownership, private modes, and hashes
  matching the application manifest.
- Phase 1 markerless state remains adoptable by explicit backed-up upgrade;
  Phase 2/3 single-volume state needs no volume migration. Remote-only state
  remains operable without a broker or snapshots. The full map and recovery
  procedure are in `MANAGED-STATE-RECOVERY.md`. Public export, retention,
  cross-artifact authenticity/atomicity, future Docker runtime reconstruction,
  and Docker Desktop remain deferred.

## D-0371: Curated runtime archives and installed catalog bootstrap

- Linux amd64 V1 distributes each exact reviewed Kodi image as a separate
  SHA-256-pinned `docker save` tar. The small source/build manifest, archive
  identity, catalog identity/profile, and passing validation report ship in
  the Kodi Lab wheel. No image archive ships in the wheel.
- The reviewed validation-host Docker image ID is an OCI index digest on Docker
  Engine 29. An isolated `docker load` of that exact archive registers the OCI
  config digest as its local image ID. Both immutable identities are recorded;
  bootstrap accepts only those exact IDs, verifies linux/amd64, and persists
  the actual local ID for unchanged broker exact-image provisioning. A tag is
  never an authority source.
- The explicit installed-artifact bootstrap validates all four images, then
  creates the exact curated definition/revision/artifact/profile/evidence rows
  in one transaction. Repeat is a verified no-op; conflicts fail closed. The
  one-shot broker-container command inspects Docker as root, drops to the Web
  state owner, validates and locks the initialized state, then writes SQLite.
  It adds no broker RPC or automatic Compose startup work.
- Bootstrap approval records that explicit administrator act. Kodi 22's
  separate Trusted Custom Build confirmation and global opt-in remain required.
  Publication, release authenticity/signing, retention, and other Docker
  engine identity translations remain future gates. See
  `CURATED-IMAGE-DISTRIBUTION.md`.

## D-0372: One managed Kodi Compose integration prerequisites

- A trusted installed-artifact `network-ensure` command owns creation or exact
  verification of the fixed `kodi-lab-managed-v1` bridge. It checks name,
  driver, three ownership/resource labels, subnet/IPAM, and non-host network
  settings, and refuses overlap with another Docker network. Host route overlap
  remains an explicit operator preflight. Broker provisioning independently
  verifies its existing network contract and gains no network mutation RPC.
- The lab-only Compose overlay attaches Web to that external managed network so
  its unprivileged JSON-RPC adapters can reach the broker-observed Kodi IP.
  Broker remains on the Compose network; managed Kodi has only the managed
  network. Web and Kodi receive no Docker socket or host networking.
- The installed Web runtime includes the WebSocket protocol package needed by
  Uvicorn's existing Kodi 21.3 mediated display. The broker's long-running
  capability set is unchanged. Only the catalog-bootstrap one-shot command adds
  SETGID and SETUID so it can drop to the Web state owner before SQLite writes.

## D-0373: Four canonical broker-owned managed lab instances

- The Linux amd64 lab is four disposable ManagedInstance pins beneath stable
  UUIDv5 Target identities, one per reviewed release. Compose owns Web,
  broker, volumes, and the product network prerequisite, not Kodi services.
  Bootstrap rejects any conflicting canonical row and safely completes absent
  rows; it does not start the runtimes.
- One installed broker-container command verifies the reviewed images and
  catalog, ensures the fixed managed network, then confirms only the reviewed
  Kodi 22 Preview revision and creates the four exact pins. Both an explicit
  Preview acknowledgement and invocation-scoped Trusted Custom Build opt-in
  are required. Normal product configuration and arbitrary custom-build
  eligibility remain unchanged.
- The installed Web-container lab command uses the existing application
  lifecycle service and restricted broker for per-runtime provision/start,
  bounded readiness, status, and stop. Selection is closed to `19 20 21 22`;
  each result is reported, independent successes remain, and any failure makes
  the command unsuccessful.
- A fresh Web process may find a running broker-owned Kodi container with no
  process-local readiness task. It starts one bounded readiness observation
  from the immutable execution profile on first running status observation.
  Visual and remote gates become usable only after that task passes. This is
  observation, not Docker adoption or a new lifecycle operation.
- Kodi 22 Beta 2's packaged Small/Mixed fixture has an exact MyVideos149 seed
  audited as configuration-only. The common fixture adapter admits only the
  four reviewed exact releases. Preview acknowledgement and the separate
  Trusted Custom Build gates still control managed-instance creation.

## D-0374: Linux amd64 V1 release-candidate artifact identity (historical four-archive candidate)

- The first local, unpublished release candidate is `1.0.0rc1`. Protocol and
  runtime revision versions are independent and unchanged. Both installed
  application images expose the package version, package-content digest and
  source-commit/version OCI labels.
- One canonical `BUNDLE.json` binds the committed source tree, Dockerfile and
  lockfile digests, source archive, application archive and both image IDs,
  deployment files, four exact reviewed runtime archives, copied wheel-owned
  runtime manifests/evidence, and required operator documentation. It and the
  complete file inventory are covered by `SHA256SUMS`. Local `.env` is generated
  after load with exact image IDs, not mutable tags.
- V1 RC makes no reproducible-build or independent-signature claim. The
  publication-time trust anchor is the separately authorized GitHub release
  account and published manifest/asset digests; GitHub attestations and
  detached signatures require a real controlled workflow or key and remain
  deferred. The candidate remains local and unpublished.
- The release Compose remains host-loopback-only. Broker Docker-socket access
  is effective host-root authority; Web has only its private broker socket.
  See `LINUX-V1-RELEASE.md` for the full inventory and recovery boundary.

## D-0375: Reviewed binary-package recipes supersede runtime archives for normal installation

- Normal runtime selection is a generation name. Matrix 19 currently resolves to
  one wheel-owned reviewed recipe revision for exact Kodi 19.4 on linux/amd64.
  The recipe pins the Ubuntu 22.04 base digest, Jammy signed repository identity,
  exact Kodi package versions and file hashes, assembly assets, execution
  profile, and validation contract. Nexus 20.5 and Omega 21.3 need separately
  reviewed recipes and acceptance; Piers 22 remains deferred. A future provider
  type can be added to the closed recipe model without replacing catalog or
  installation schemas.
- An explicit one-shot `kodi-lab-runtime install <generation>` command performs
  acquisition, local image assembly, validation, and exact catalog registration.
  The Docker-capable worker creates only fixed recipe inputs; Web receives no
  Docker socket or generic Dockerfile/package/shell request. The worker uses a
  private install lock and durable pending/acquiring/assembling/validating/ready
  or failed rows. When root runs the worker, only a forked state-owner child
  writes SQLite. The long-running broker protocol remains unchanged.
- A successful build alone grants no runtime eligibility. The shared catalog
  policy requires an approved exact revision, artifact and profile, passing
  validation evidence, and a ready install row whose recipe digest, acquired
  image, attestation, and validation match the reviewed wheel resource. Managed
  instances pin exact revisions and are never silently rebound by reassembly.
  The wheel keeps prior reviewed recipe files addressable by recorded identity
  so a later current recipe does not remove capabilities from an existing pin.
- Existing private 19.5 archives, catalog rows, and four-archive bootstrap remain
  supported during migration. The old unpublished release-candidate bundle is
  historical and still archive-based; a future release slice must replace its
  four-runtime archive requirement before publication.
- Subsequent status: `LINUX-V1-RELEASE.md` now defines the binary-free
  three-recipe candidate with Matrix 19.4, Nexus 20.5, and Omega 21.3. The
  four-archive bundle remains historical private evidence, and the separate
  Nexus/Omega review requirements above were completed before this status.

## D-0376: Signed APT trust and bounded dependency policy for Matrix

- The reviewed base is pulled by digest and verified as linux/amd64. Its Ubuntu
  archive keyring hash is checked before a fixed Jammy sources list is used.
  APT verifies signed InRelease metadata, package-index hashes, and downloaded
  package bytes. The recipe additionally verifies file hashes, versions, and
  architectures of `kodi`, `kodi-bin`, `kodi-data`, and
  `kodi-repository-kodi` before accepting the assembled image. A missing or
  changed critical package fails installation; no alternate distro, mirror,
  Kodi version, or source compilation is selected.
- Ordinary transitive runtime dependencies are authenticated through the same
  reviewed signed repository. The exact installed inventory and signed metadata
  digests are persisted in the acquisition attestation. They are observed
  identities, not a claim that the whole dependency graph is reproducible
  indefinitely from mutable Ubuntu pockets. A future recipe revision is needed
  when reviewed inputs or validation expectations change.
- Local image identity is the inspected immutable Docker image ID. A missing
  image remains unavailable to existing exact pins; an explicit reinstall
  creates and validates a new local artifact/revision. Docker images are
  reconstructible external state and are not included in application backups.
- Explicit assembly disables Docker layer reuse so each new installation
  obtains signed APT metadata and package files for its own acquisition
  attestation. Repeated `install` is an exact-image verified no-op while the
  latest ready artifact remains available.

## D-0377: Matrix 19.4 fixture and capability acceptance

- The new `small-mixed-v6` package is explicitly compatible with 19.4 and uses
  the audited Kodi 19 configuration-only MyVideos119 seed. Its semantic assets
  remain synthetic. The previous 19.5 fixture and private reference are intact.
- The fixed two-cycle recipe validation checks exact Kodi identity, readiness,
  non-root execution, Xvfb, llvmpipe GUI, 1280x720 PNG, navigation, library
  queries, and stop/restart. The normal managed lifecycle, reset, screenshot,
  navigation, and add-on services additionally passed isolated Matrix 19.4
  acceptance. Capability discovery for this revision reads the same ready
  recipe/eligibility evidence rather than a static version branch.

## D-0378: Local integrity binding and disposable acquisition network

- A ready reviewed-recipe installation stores a canonical acquisition
  attestation with a domain-separated HMAC-SHA-256 binding. The bound payload
  includes the entire attestation (recipe, provider, base, signed metadata,
  critical package hashes, full installed inventory and its digest, exact
  image ID), validation report, operation and revision UUIDs, and the complete
  validation-evidence identity and result. The binding and ready/approval
  transition commit together. Missing or invalid bindings are ineligible;
  older unbound ready rows require explicit reinstall and validation.
- The HMAC key is derived from the existing private `master-key` file under a
  separate purpose label. It is never stored in SQLite. The existing backup
  and restore contract preserves the exact key alongside the database and
  verifies its state marker. A missing, replaced, corrupt, or unsafe key fails
  eligibility closed; restore must not silently generate a replacement.
  This detects persisted provenance corruption or tampering by a database
  writer without key access. It does not defend against control of the Kodi
  Lab process, key, Docker daemon, or host/root trust boundary.
- Each assembly operation creates a labeled, non-attachable Docker bridge with
  an operation-derived name, builds only on that bridge, then removes it. This
  avoids reliance on Docker's default bridge. Ordinary outbound Docker NAT is
  needed for the reviewed Ubuntu repositories; the bridge is not an egress
  firewall or destination allowlist. No caller chooses a network, host mode,
  privilege, build argument, Dockerfile, package, or repository.
- Catalog eligibility describes persisted trust in an exact artifact identity.
  Current local image availability is checked separately before reuse and by
  broker provisioning. Deleting an image can leave catalog trust eligibility
  true while the old managed pin is unavailable; it does not trigger a pull,
  rebuild, or pin change.

## D-0379: Reviewed Nexus 20.5 shares Matrix acquisition and integrity model

- `ubuntu-noble-nexus-20.5` revision 1 is a separately digest-pinned wheel
  recipe for linux/amd64 Kodi 20.5 from signed Ubuntu Noble pockets. It uses
  the same fixed APT provider, per-operation bridge, local image assembly,
  two-cycle validation, HMAC-bound attestation, catalog eligibility, and exact
  managed revision pin as Matrix. No Nexus-specific installer or integrity key
  is introduced.
- The reviewed Noble base already owns UID/GID 1000 as the exact `ubuntu`
  account. Assembly accepts and renames only that expected account, clears its
  supplementary groups, and verifies the resulting `kodi` group set. Matrix
  continues through the absent-account creation path. A different occupied
  identity fails assembly.
- Isolated wheel-installed acceptance acquired and validated Nexus twice as
  distinct exact images. Deleting the first image made its original instance
  pin unavailable; explicit reinstall created a second revision without
  rebinding that instance. Matrix and Nexus managed instances then ran together
  from their own image IDs, with independent JSON-RPC endpoints, screenshots,
  and version-compatible `small-mixed-v6` / `small-mixed-v3` fixtures.
- Nexus reports JSON-RPC 13, Python 3.12, and MyVideos121; Matrix reports
  JSON-RPC 12, Python 3.10, and MyVideos119 in the accepted images. Nexus's
  synthetic Python add-on executed after normal ZIP installation and enablement.
  Omega remains a separate package, fixture, and live-validation review task.

## D-0380: Closed reviewed-runtime Web installation worker

- The normal `/runtimes` choices are exactly Matrix 19.4, Nexus 20.5, and
  Omega 21.3. Authenticated Web can request a generation and semantic operation
  UUID only. The broker derives the running installed broker image ID, the
  exact state and socket volume names, a fixed `kodi-lab-runtime install`
  command, and a deterministic worker name. It accepts no Docker image,
  command, mount, capability, package, URL, or network input from Web.
- The one-shot worker mounts the shared state and lock volumes and the Docker
  socket, runs without its own network, uses a read-only root filesystem, and
  receives only temporary `SETUID` and `SETGID` capabilities to preserve the
  existing state-owner SQLite transaction model. The long-running broker and
  Web Compose capability and mount sets are unchanged. The worker's UUID,
  generation, image ID, and resource identity are sealed in ownership labels;
  observation verifies these plus its fixed execution and mount contract.
- Broker admission refuses another running reviewed worker or a held global
  acquisition lock. Web first checks durable active rows, returning an active
  same-generation attempt or a semantic busy response for another generation.
  The existing worker file lock remains authoritative when an independent CLI
  installer races Web. A stopped or missing exact worker and a free lock permit
  durable interruption reconciliation without an elapsed-time failure rule.
- The existing installation table and phase states are sufficient; there is no
  jobs table or migration. A ready row is presented as Ready only while the
  existing eligibility policy verifies its approved revision and HMAC-bound
  evidence and the broker verifies its exact reviewed image remains present.
  Missing image bytes require explicit Reacquire. Existing managed-instance
  revision pins are never changed by reacquisition; the page links to the
  existing instance and says when it retains a previous exact revision.
  Successful acquisition keeps the installer's stopped-instance bootstrap
  behavior and does not start Kodi.

## D-0381: Disposable managed instances are the V1 model

- Decision: Kodi Lab-created V1 ManagedInstances are disposable and pin exact
  reviewed runtime revisions. Provision creates or recreates a stopped runtime
  container under the existing managed-state contract. Stop retains its
  provisioned container and writable layer; deprovision removes them while
  retaining the managed record and durable Kodi Lab state. Reset returns the
  runtime to its deterministic fixture under the existing reset contract.
- Preservation: validated Snapshots, including preserved Snapshots under the
  confirmed instance-deletion policy, are the bounded V1 preservation
  mechanism. Application state, exact revision records, and applicable
  backup/recovery contracts persist under their own existing rules.
- Rationale: a persistent managed-container mode would add lifecycle, storage,
  and recovery semantics without a demonstrated need for the V1 lab/testing
  goal. It is deferred post-V1, with no V1.x commitment. Dormant domain values
  may remain for historical or extensibility reasons and confer no V1 support.
- Boundary: external development and production Kodi targets may be long-lived
  installations. They are not Docker-managed disposable instances and retain
  their separate registration, onboarding, bridge, and policy contracts.

## D-0382: Opt-in release development-source mount

- Decision: the source-free Linux amd64 bundle includes a separate optional
  `compose.development.yaml` overlay. It binds one administrator-selected host
  directory into Web at fixed `/mnt/kodi-lab-development` and sets the existing
  `KODI_LAB_DEVELOPMENT_ROOT` to that fixed path. The same overlay binds that
  host directory read-only into broker at the same fixed path and sets broker's
  `KODI_LAB_DEVELOPMENT_ROOT` to it. Base release Compose exposes no source
  directory to either service. Repository and managed Kodi receive no source
  mount. This corrects the original Web-only wiring: D-0129's existing
  attached-source and promotion-sync broker operations already require source
  reads. Their semantic protocol and server-derived locators do not change.
- Access: the bind is read/write because the accepted reviewed promotion flow
  writes exact approved paths back to the original registered source. The Web
  service account receives only the host filesystem permissions that the
  administrator grants. Keep the root itself non-writable and grant project
  write permission only where patch-back is intentional, as D-0208 requires.
  Broker is root with Docker authority and gains read/confidentiality access
  within the selected root, including files unreadable to Web's UID. Select a
  dedicated root containing only intended development material. Broker verifies
  the fixed read-only mount and fails closed when absent, writable or wrongly
  configured. Shared Linux `openat2` source traversal forbids symlink and nested
  mount crossings beneath the opened root; unavailable confinement fails closed.
- Configuration: `linux-v1-bundle development-source` validates a canonical
  existing absolute non-symlink host directory and writes its quoted path only
  to local `.env`. Compose requires a nonempty variable and does not create a
  missing bind source. The staged artifact never contains the chosen path.
  Registration remains restricted to normalized relative locators through the
  existing no-follow validator; Web and MCP gain no host-path or mount API.
  Linux amd64 V1 is qualified; Windows Docker/Desktop source sharing needs a
  separate gate. Deployment does not automatically relabel host files for
  SELinux or AppArmor.

## D-0383: Shared OTP bridge pairing with distinct production policy

- Bridge 1.1.13 remains `otp-1` only and continues to reject `pair_bridge`.
  Production Web pairing stages the same authenticated provisional transaction
  and encrypted independent 32-byte credential used by external development.
  The existing pairing row and secret store support restart and lost-response
  recovery, so no migration or second pairing protocol is introduced.
- Production registration, private-LAN endpoint, confirmed native identity and
  designation, configured screenshot maximum, exact HTTP acknowledgement,
  reviewed build, and explicit installed-provenance confirmation remain separate
  requirements. The final Web confirmation performs the idempotent OTP commit;
  pairing alone grants no MCP authority and leaves mutation vetoes intact.
- The legacy long-token service/client methods remain for historical tests.
  Normal external-development and production Web routes no longer issue tokens
  or call `pair_bridge`.

## D-0383: Closed bridge ZIP and local deployment candidate provenance

- Bridge 1.1.13 keeps its accepted protocol 1 / `otp-1` behavior unchanged.
  Packaging admits only the nine explicit tracked release files; incidental
  source-directory files never enter the ZIP. ZIP paths, order, timestamps,
  modes and compression are normalized by builder v2.
- The local Linux amd64 candidate includes the bridge ZIP and `BRIDGE.json`.
  The provenance binds the exact committed source, bridge Git subtree, builder,
  file inventory, license and ZIP digest. `BUNDLE.json` binds that provenance
  and ZIP to the application images and reviewed runtime recipes. Verification
  checks both ZIP structure and bytes, not only a listed checksum.
- The bridge is Kodi Lab's companion artifact. The bundle continues to exclude
  Kodi binaries, packages and runtime image exports. This local candidate is
  for extended server testing before any RC or public release decision.

## D-0384: Two Kodi repository origins with a reviewed stable Phase 1

- `repository.kodi-lab` distributes only reviewed infrastructure, initially its
  own bootstrap ZIP and the exact bridge 1.1.13 ZIP. A separate opt-in
  `repository.kodi-lab-dev` is reserved for Phase 2. Workspace publication can
  never claim either repository ID, `service.kodi_lab_bridge`, or other reserved
  infrastructure identities. No development feed or MCP tool is added in Phase 1.
- The stable feed is unauthenticated read-only private-LAN HTTP on an explicit
  operator-selected bind address, port and durable base URL. Administrative Web
  remains loopback-only. The repository process has only a read-only feed mount
  and no database, secrets, master key, sessions, sockets, upload route or
  administrative application. HTTP has no malicious-LAN transport authenticity;
  a later reverse proxy can add TLS without changing feed semantics.
- A source-free candidate contains the generator, reviewed artifact and fixed
  stable manifest. Install configuration generates the `repository.kodi-lab`
  1.0.0 ZIP for the exact base URL. Generated bytes are recorded in separate
  installation provenance, never falsely listed as original bundle bytes.
  Identical inputs generate identical ZIP, index, checksums and tree digest.
  A published ID/version/byte identity is immutable; mismatched replacement
  fails closed. A complete verified generation is atomically selected for reads
  while immutable package paths remain available.
- Phase 1 stable bytes are reconstructible from candidate and configuration;
  preserve their provenance and verify reconstructed bytes before serving.
  Phase 2 must extend coherent backup to durable development artifacts. First
  development-update proof is operator-triggered in Kodi. Production policies
  remain unchanged and arbitrary workspace artifacts cannot be authorized by
  repository presence. Managed Kodi keeps direct controlled install as default.

## D-0385: Phase 2 development feed admission and approval

- `repository.kodi-lab-dev` 1.0.0 is a separately installed, opt-in repository
  pointing only at `/development/`. Its bootstrap ZIP is generated for the
  configured base URL and recorded in per-install provenance. The stable index
  and bridge bytes remain reviewed and separate.
- Only authenticated Web import of a bounded Python `plugin.video.*` ZIP can
  prepare a Phase 2 publication. The operator reviews exact ID, version, type,
  import identity, Kodi compatibility, ZIP hash, tree digest and development-only
  scope, then explicitly approves the same ID/version/hash. The public LAN
  server is GET/HEAD only and has no publication authority. Workspace-origin
  fields exist in the durable schema but workspace deployment is unchanged
  until Phase 3. No MCP or bridge protocol change is authorized.
- One reviewed policy reserves both repository IDs, `service.kodi_lab_bridge`,
  and Kodi Lab infrastructure namespaces. Version admission uses a strict
  `major.minor.patch` numeric subset whose ordering matches Kodi's add-on
  comparison. A different ZIP under an existing ID/version is rejected forever;
  a new advertised update must have a greater version. Source versions are not
  silently rewritten.

## D-0386: Durable publication, reconciliation and recovery

- Migration `0055_development_repository_publications` stores typed publication,
  feed-state and retention-hold records. ZIP bytes live in `repository_data`,
  outside SQLite and the administrative Web file-serving surface. One writer
  lock serializes approvals. An approved intent is durable before an atomic
  development pointer switch. The static service serves only acknowledged,
  hash-verified generations; startup reconciles known interrupted intents and
  blocks only development serving on unexplained state. Stable serving is
  independent.
- The current advertised version, two previous versions, recently superseded
  versions and artifacts with evidence holds are retained. Configurable global
  count/byte quotas reject publication when protected content consumes capacity.
  Retirement never advertises an older version as rollback and leaves a durable
  ID/version/hash tombstone. Rollback UI is deferred.
- A quiescent application backup includes the approved development subtree
  and SQLite snapshot under one manifest, with their feed identity and hashes
  cross-checked. Stable reviewed bytes are reconstructible and bound by exact
  stable provenance. Recovery requires the same stable candidate/base URL and
  restores the development subtree plus database before clearing the recovery
  marker. An interrupted restore must not serve development content.

## D-0387: Repository-mode workspace deployment remains human-gated

- The existing `workspace_deploy` operation accepts an explicit `direct` or
  `repository` mode; its default selects repository mode only after an
  external-development target has an operator-confirmed development repository.
  Managed and existing direct deployments keep their controlled path. No MCP
  tool, bridge method, or production authority is added.
- Web confirmation requires Kodi Lab to observe the exact installed
  `repository.kodi-lab-dev` descriptor through native inventory and the paired
  bridge. The operator confirms target refresh at the configured URL and Kodi's
  notify-without-auto-install setting. An HTTP feed alone does not prove Kodi
  installed the repository.
- An exact workspace generation, validation receipt, target/bridge identity,
  explicit add-on version, ZIP hash and predicted installed-tree digest bind
  each deployment request. Migration `0056` snapshots the validation receipt
  before the mutable latest-validation row can be replaced. Web approval checks
  those values again. Per-operation `publication_approved` is durable before
  the feed changes, so a shared publication cannot approve another request on
  restart. Publication
  leaves the operation pending for manual Kodi install/update. Only matching
  native identity and authenticated bridge tree evidence mark deployment
  succeeded. Execution and promotion retain verified-deployment requirements;
  promotion accepts the exact verified repository link as source proof, while
  direct promotion still requires its external source baseline.
- A same-ID/version package may be reused by another target-bound request only
  with identical bytes and workspace generation/provenance. That request still
  needs its own Web approval. Workspace edits never change pending or published
  bytes. Offline observation stays pending; identity/classification changes
  fail closed.

## D-0388: Reviewed candidate changes require an explicit repository transition

- Stable generations remain bound to the BUNDLE that produced them. The static
  server and ordinary generator continue to reject unexpected BUNDLE identities.
  An operator selects exact old and new verified source-free candidates, their
  BUNDLE digests, and a complete pre-transition backup for a one-off transition.
- The operation verifies old stable and development generations, reviewed
  candidate inputs, base URL, publication inventory, and backup binding before
  preparing new immutable generations. A durable intent records both candidate
  and feed identities before either pointer moves. Web and repository startup
  refuse service during an unfinished transition. Rerunning the exact request
  completes a known interruption; another request fails closed.
- Development artifact bytes, publication rows, holds, history and deployment
  links stay unchanged. Only the development feed generation and its existing
  SQLite state pointer advance to the new candidate identity. An explicit
  reverse transition to the backup's candidate precedes restoring its database
  and development bytes. The transition itself needs no schema or bridge change.
- A candidate may introduce a reviewed forward schema migration. Its bundle
  records the installed migration head; the retained pre-0057 candidate's
  existing 0056 capability marker supplies its legacy head. The transition
  requires the live database and complete backup at the old head and checks an
  unbranched Alembic ancestry path to the new installed head. It records both
  revisions in the durable intent and receipt, switches repository generations,
  then leaves the database at the old head for the explicit application upgrade.
  Web startup stays blocked until the new head and linked upgrade receipt exist.
  Read-only repository serving from verified new generations is safe during
  this stopped application interval, but the documented workflow keeps services
  stopped until upgrade completes. A missing or tampered transition receipt
  blocks repository-backed schema upgrade and Web completion.
- The original old-candidate backup is the A rollback boundary both before and
  after schema migration. A backup taken during the intermediate state retains
  its transition binding so an interrupted migration can restore and retry
  without losing provenance. Later backups also retain the linked application
  upgrade receipt, so a future same-head candidate rollback can restore an
  already migrated candidate coherently. Migration 0057 still refuses a lossy
  downgrade
  after amendment receipts; choosing the old backup then intentionally discards
  post-upgrade operations rather than downgrading their evidence.

## D-0389: Audited paired-development capability changes and narrow MCP presets

- An existing external-development target may change its configured maximum only
  through explicit authenticated Web administration. A fresh native probe and
  authenticated pinned bridge identity/capability observation must support the
  requested set. The target and bridge UUIDs, classification, pairing and secret
  reference remain unchanged. Exact replay returns the original receipt.
- The content-free external-target event table cannot preserve before/requested/
  resulting sets and bridge evidence. The production screenshot amendment table
  is intentionally restricted to production. Migration 0057 therefore adds a
  purpose-built immutable development receipt and transaction guard. It scopes
  the existing screenshot-removal trigger to production without relaxing its
  tombstone, scope, or approval conditions. Downgrade refuses to discard receipts.
- Custom MCP presets may contain an explicit nonempty subset of a non-production
  built-in ceiling. Existing built-ins and the client/target/grant/workspace/
  capability intersection stay intact. The physical development client needs
  only `workspace.inspect`, `workspace.deploy`, `development.execute`;
  `workspace.inspect` is technically shared by validation and bound-workspace
  read/status tools. This grants no promotion, remote control, or screenshot
  authority and does not bypass Web publication approval or installed-tree proof.

## D-0390: Repository transition preserves application-owned development files

- The networkless candidate-transition job runs as root only to coordinate the
  root-owned stable repository, transition evidence, and a temporary switch to
  the verified application state owner. Its development generation and atomic
  `CURRENT`/`ACK` writes execute as that state owner; atomic replacement must not
  leave a root-owned inode in the Web-writable development subtree. The
  transition no longer needs CHOWN authority for this path.
- The state root validated by the existing lifecycle lock supplies the canonical
  application UID/GID. The standard packaged image fixes that identity at
  `10001:10001`; development repository initialization assigns its subtree to
  the same identity. Stable generations and transition intent/receipts remain
  root-owned. No caller-supplied host UID/GID or path is accepted.
- The unchanged backup metadata validator independently checks the complete
  development subtree during the subsequent application upgrade. Transition
  checks the same metadata before and after its writes, including on retry and
  rollback, so provenance alone cannot bless unsafe filesystem ownership.

## D-0391: Repair only the reviewed legacy development pointer inode

- The accepted A candidate with BUNDLE
  `e68e66d271e8a36873f830526c53c1f253ef48afdda260e589a5e5eaaff41709`
  used a root transition job to atomically replace development `CURRENT` before
  D-0390 moved that write to the Web state owner. A real A installation and its
  completed transition receipt identify the resulting root-owned `CURRENT`;
  the unchanged application backup correctly refuses it.
- A later reviewed candidate may provide one networkless, Docker-admin-only
  compatibility command. It requires the exact A BUNDLE, its completed
  transition receipt, its live 0056 database, matching stable/development
  provenance, acknowledged generation, exact pointer bytes and mode, and a
  complete development subtree safety audit with no other defect. It accepts
  no caller path or UID/GID. A dedicated Compose profile grants CHOWN only to
  that one-shot job; repository transition and serving authority do not grow.
- The command changes only the already-open no-follow `development-data/CURRENT`
  inode from `0:0` to the packaged Web state owner `10001:10001`. It records a
  durable pending/completed receipt outside the served development subtree,
  so interruption can be retried without confusing a corrected inode with a
  completed audit. Completion follows the original backup ownership check and
  repository/DB verification. A canonical pointer without repair intent is a
  read-only no-op. The ordinary backup validator is unchanged and must pass
  afterward before any candidate transition.
- This command is not an installation repair framework. A different candidate,
  unsafe mode/content, extra ownership error, symlink, or unmatched provenance
  needs separate review. Since the accepted B package has no such command, it
  remains immutable and must be superseded by a newly built candidate before
  an affected A installation can use the supported compatibility operation.

## D-0392: External working source and explicit existing-workspace capture

- A registered local DevelopmentSource remains a normal human-editable project.
  Registration is an initial observation, not a frozen source version. Fresh
  scanning is explicit; Git is optional and never substitutes for the captured
  tree digest. Direct MCP writes to host-backed source are deferred.
- Capture reuses the existing workspace. It requires a ready workspace whose
  current tree equals its active captured baseline and no unresolved promotion
  or repository deployment. It safely stages a complete source tree, checks the
  exact add-on ID and a second source scan, then advances one generation for
  the entire changed tree. Equal bytes are idempotent and do not advance it.
- The original creation baseline ID/digest remain durable. Each capture records
  its own source digest/version and prior/resulting generation and baseline
  identity. Prior baseline bytes are archived under UUID-derived private state.
  The active baseline changes only with the workspace tree and receipt. A
  `capturing` workspace cannot be used for validation, deployment or editing;
  interrupted staging fails safely, while a partially switched capture is
  reconciled from exact old/new digests before returning to `ready`.
- Validation and deployment continue to bind workspace generation and exact
  bytes. Publications remain immutable; later external edits only make the
  working source newer than the captured/tested/deployed state. Historical
  receipts are not rewritten. Existing capture history is retained and blocks
  whole-workspace cleanup until a separately reviewed reference-aware cleanup
  exists. No automatic capture pruning is added.
- Migration `0058_workspace_source_capture` backfills active identity from
  existing 0057 workspaces without rewriting their bytes. The generation-0
  physical acceptance workspace remains representable. Downgrade refuses to
  discard any capture operation/evidence. Gate 1 adds storage/application
  foundations only; Web and MCP scan/capture surfaces require a later gate.

## D-0393: Separate fresh scan, explicit capture, and opt-in MCP authority

- The externally editable DevelopmentSource is observed only by an explicit
  fresh scan. Scan returns bounded identity/version/digest/time evidence but does
  not mutate the workspace, its generation or historical source registration.
  A later manual edit makes that scan historical; ordinary page load says
  `not_freshly_scanned` rather than asserting a live match.
- Web Capture is a separate authenticated, CSRF-protected operator action. It
  requires the expected generation and invokes the same Gate 1 capture service
  after fresh traversal; private edits and uncertain capture state block it.
  Receipt-based recovery is exposed only for recorded interrupted operations.
- The MCP catalog adds `workspace_source_scan` and `workspace_capture`, yielding
  seventeen tools. Inspection and mutation have distinct opt-in permissions,
  `development_source.inspect` and `workspace.capture`. Each call requires the
  exact source grant and workspace binding. Existing built-in presets and
  already created clients acquire neither permission automatically. Source edit
  authority remains deferred.
- Migration 0059 adds the exact tree digest to the latest validation row. It
  backfills only rows whose generation still matches the current workspace;
  historical stale rows without provable bytes remain unknown, not invented.
  Downgrade refuses to discard a digest that can no longer be derived from the
  current workspace. Validation/deployment/publication/verification receipts
  stay immutable.

## D-0394: Candidate verification binds migration head to packaged images

- Candidate staging derives the reviewed Alembic head from the clean source
  checkout and records it in both application image configs. Finalization
  independently runs the installed Web image to read its actual migration head
  and requires the image labels to match. The source-free verifier compares
  the BUNDLE migration identity with the digest-checked image config labels.
- This replaces the stale verifier assumption that every subsequent candidate
  must end at 0057. It retains an exact, networkless identity check for each
  candidate and rejects rehashed BUNDLE metadata whose head differs from the
  archived application images.

## D-0395: Managed display recovery retains historical runtime pins

- A managed container's private writable layer can retain Xvfb display-99
  lock and socket files after abrupt termination, while its private PID
  namespace no longer contains the old server. Reused PIDs can make X server's
  own lock check reject an otherwise restartable container.
- New reviewed runtime revision 2 performs a fixed, non-root display-99
  preflight before Xvfb starts. It checks trusted directory and artifact
  identity, both filesystem and abstract listener state, and process identity.
  Only proved-stale `/tmp/.X99-lock` and `/tmp/.X11-unix/X99` are removed.
  Uncertain or live state stops startup. Readiness requires the newly launched
  Xvfb child and its own lock before Kodi starts.
- Revision 1 recipe bytes and assets remain packaged for existing exact pins.
  A current managed container keeps its old image on application upgrade; no
  existing container is silently repaired or repinned. The reviewed revision
  change does not add a general container or filesystem repair capability.

## D-0396: Administrator-selected temporary reviewed-runtime network pool

- Linux V1 retains Docker automatic bridge allocation on clean hosts. Before
  reviewed acquisition, a bounded preflight checks capacity when Docker
  exposes its effective pools. If it does not, the worker lets Docker decide
  at network creation and reports its specific exhaustion error safely.
- An administrator may instead configure a private IPv4 parent CIDR and child
  prefix in the source-free bundle's unbundled `.env`. No universal subnet is
  selected. The installer checks host addresses, all visible routes and Docker
  networks; the worker repeats Docker and host-route checks on each allocation.
  The release broker and installation worker receive a fixed read-only host
  route file for this check, including one-off CLI acquisition. It chooses one
  free child subnet with Docker's atomic collision check. Web and MCP requests
  never carry subnet choices.
- Acquisition keeps a non-internal bridge for reviewed package downloads;
  validation keeps a different internal bridge for the disposable test Kodi.
  Neither bridge reuses the managed, Compose or repository networks. Exact
  operation and purpose labels bind cleanup to a specific Docker network ID.
- Normal cleanup removes the exact owned network after use. A later worker may
  reclaim a labeled abandoned bridge only for a durable terminal operation
  with no Docker container reference or running installation worker. No general
  Docker pruning authority is added.
- Operation records retain safe network creation/capacity codes. The specific
  Docker address-pool exhaustion code requires Docker's matching returned
  error. Unknown failures keep a generic or phase-specific safe code; raw
  Docker diagnostics and host inventory are not shown in Web.

## D-0397: Local bridge pairing reset for orphaned server targets

- A bridge ID identifies the Kodi bridge installation independently of a Kodi
  Lab target pairing. A locally confirmed reset preserves that ID, installed
  bridge, deployment history, rollback state, and the rest of the Kodi profile.
- The paired bridge settings action requires Kodi's own confirmation dialog.
  Confirmation clears the onboarding transaction/OTP state first, then removes
  the target ID, persistent credential, and replay ledger from the durable
  bridge state. An interruption before the latter write leaves the bridge
  visibly paired; an interruption after it leaves the old credential unusable.
- The Kodi service and local settings script serialize state changes with a
  private cross-process file lock. Cached signed responses are reauthenticated
  against current pairing state, so an obsolete credential cannot recover a
  cached receipt after reset. No HTTP reset operation or server-side target
  reconstruction is introduced. Fresh pairing uses the existing `otp-1` flow.

## D-0398: Existing approved development installs need truthful verification

- Planned repository deployment retains its newer-version proposal, target-bound
  Web approval, manual Kodi install/update, and operation-bound Recheck. Earlier
  records remain `planned` and retain their MCP attribution and approval times.
- A separate authenticated, CSRF-protected Web action may record
  `verified_existing` only for an already-installed exact current approved
  workspace publication on a confirmed external-development repository target.
  It requires fresh native Kodi identity/inventory and authenticated Bridge
  tree evidence matching the retained approved ZIP, its current feed binding,
  workspace generation, and validation receipt. The record attributes the
  verification to the Web operator and has no prior target approval time.
- Verification performs no installation, deployment, execution, grant, or
  publication. Exact repeats reuse the result; ambiguous or conflicting
  operations fail closed. A verified workspace-origin result may satisfy the
  existing execution provenance check, while all separate live Bridge and
  execution-authority gates still apply. Web imports cannot gain workspace
  execution provenance through this action.

## D-0399: Rich Small Movies retain embedded, offline fixture media

- `small-movies-v4` is a new immutable Small/Movies package for exact Kodi
  20.5. It coexists with historical packages. Its twelve real movie titles and
  release years provide representative folder and file identities; the clips,
  artwork, plots, genres, people, studios, ratings, sets, tags, and fixture IDs
  are Kodi Lab synthetic test material. Controlled fields do not claim to be
  factual metadata about the real films.
- The package embeds tiny playable synthetic MP4s, local NFOs, and synthetic
  artwork under the existing profile path. It reuses the audited Nexus
  configuration-only video seed and `metadata.local`; Kodi creates all movie
  rows through its native scan. Normal reset is deterministic and offline,
  without Internet scraping or preloaded library rows.
- The shared immutable media corpus remains deferred while Small and Medium
  use embedded media. Large-scale storage and timing measurements may justify
  reconsideration. Real title/year naming may support a separately authorized
  scraper acceptance mode later; this decision does not enable that mode.

## D-0400: Small Movies playback state is a post-scan fixture revision

- The accepted `small-movies-v4` package and digest remain immutable. Nexus
  `small-movies-v5` retains its exact embedded media, local metadata, and
  configuration-only seed bytes, but has a new package identity and digest
  because prescribed playback state changes fixture semantics.
- Kodi still creates every movie entity through its native scan. The v5 reset
  resolves exact packaged movie IDs from the scanned library, then uses only
  fixed private Nexus movie-state operations to apply five watched and two
  in-progress records. It reads state back and fails reset if the prescribed
  state is absent. Playback state lives only in each Kodi instance database;
  the configuration seed has no movie or history rows.
- An in-progress movie has playcount zero and therefore remains in the Kodi
  unwatched class. The tiny synthetic media is not played to create resume
  points. This decision grants no Web/MCP movie-mutation or arbitrary JSON-RPC
  authority, and does not apply to external targets. Matrix/Omega post-scan
  state behavior remains unproven.

## D-0401: Correlated Kodi execution errors stay on the existing operation

- A fixed workspace execution may persist only Kodi's integer JSON-RPC error
  code and a bounded sanitized message on its existing durable operation.
  Historical rows remain valid with both fields null. Replay returns the same
  persisted safe evidence without redispatching Kodi.
- MCP authorization and the fixed execution request remain unchanged. Failure
  text retains the broad code and has no success-shaped `structuredContent`.
  No generic diagnostic lookup, raw response, JSON-RPC data, Kodi log,
  traceback, or addon exception channel is added.

## D-0402: Managed current-state projection uses one runtime ordering revision

- `managed_instances.runtime_state_revision` orders mutable runtime projection
  writes. It starts at zero and is distinct from the immutable RuntimeRevision
  artifact pin. Lifecycle operation rows remain historical records and are not
  rewritten by status convergence.
- Every supported mutation that can invalidate a managed runtime observation
  advances the revision with its existing per-instance reservation, before
  external mutation. Exact finalization or abandonment advances it again.
  Trusted status captures the pin and revision before Broker/Kodi/readiness I/O,
  then atomically compares and sets the managed-instance and associated managed
  target current fields only if both identities still match and no mutation
  lease exists. Web restart alone advances nothing. External targets, fixture
  assignment, stopped-only source copy/sync and workspace execution are outside
  this ordering token.
- Targeted managed status reads and converges only the requested managed
  instance and its associated target. Its Broker inventory selection is scoped
  to that instance before Docker inspection. Explicit collection status may
  read and converge the full managed collection.
- A dispatched Broker runtime mutation whose response/quiescence cannot be
  confirmed remains reconciliation-required and blocks conflicting mutation.
  Stable snapshots and lease expiry alone cannot clear this Class-B state;
  read-only status remains available. In particular, repository replacement may
  retain `application_started=0` at Stop because add-on application has not
  begun. Only the exact `broker_runtime_dispatch_unconfirmed` reason permits
  pre-application reconciliation, without falsifying that historical marker.
  Interrupted lifecycle work without durable dispatch proof is treated more
  conservatively than D-0113's earlier stable-snapshot warning policy. An
  interrupted running-source snapshot capture after its Stop marker is likewise
  blocking even if later runtime snapshots appear stable. Definitive Broker
  failures and known pre-dispatch replacement failures retain ordinary recovery.
- Explicit administrative recovery of Class B is future work. This decision
  adds no Broker ledger, fencing, process epoch, or exactly-once Docker claim.
  Unsupported direct Docker mutation remains outside V1 ordering guarantees.

## D-0403: Workspace execution admits one fixed negative Kodi RPC diagnostic

- `workspace_execute` defaults to its unchanged bounded plugin smoke. Its only
  additional intent is a fixed negative Kodi RPC diagnostic for a disposable
  managed target with the same owned workspace, exact successful deployment,
  readiness, provenance, rate limit, and `development.execute` admission. The
  external-development execution bridge remains smoke-only; production remains
  ineligible. The caller selects only the closed intent, never method, params,
  path, add-on, or target outside the existing grant.
- Kodi Lab sends exactly `KodiLab.NonexistentDiagnosticMethod` with no params.
  A correlated Kodi JSON-RPC error truthfully fails the existing execution
  operation as `jsonrpc_error`; its numeric code and sanitized bounded message
  use the D-0401 fields. The existing request fingerprint binds the intent,
  while replay returns the original operation and never redispatches. No new
  schema, operation table, Web control, or generic JSON-RPC authority is added.

## D-0404: Observe one bounded addon exception on failed disposable smoke

- A synchronous fixed plugin smoke can return a Kodi JSON-RPC error while Kodi
  also logs a Python addon exception. The existing Kodi error remains the
  execution result. A matching exception is an observation within that call
  window, not proof that it caused the RPC failure.
- Only `workspace_execute` smoke on an exact successfully deployed generation
  of a disposable managed target can collect this observation. The broker
  resolves the running operation, workspace, deployment, addon identity, runtime
  revision, and current labeled container from trusted state. Its two closed
  operations capture a signed cursor immediately before invocation and read one
  fixed Kodi log window after a correlated Kodi error. The request names only
  the execution operation and opaque cursor. No path, container, addon, method,
  URL, log filename, time range, or filter is caller selectable. Web and MCP
  never receive Docker authority or raw log bytes.
- A complete Python traceback must contain a frame under the exact installed
  addon tree and be followed by exactly one matching plugin-root GetDirectory
  failure in the post-cursor window. Ambiguous, malformed, rotated beyond the
  fixed reader's safe continuation, oversized, stale, or unavailable evidence
  yields no summary. The parser constructs only an identifier-shaped exception
  class, addon-relative Python file, and bounded positive line number. V1 omits
  all exception detail text, source lines, traceback, and absolute paths.
- Migration 0064 adds nullable bounded status, class, file, line, and detail
  columns to the existing execution operation. Historical NULL means not
  collected, including ineligible executions. `no_match` means a readable
  bounded window contained no matching exception. `rejected` means readable
  evidence was malformed, oversized, ambiguous, or could not safely continue
  the cursor. `unavailable` means capture or observation failed. These three
  statuses carry no exception details; `matched` means exactly one complete
  correlated summary was persisted. Failure finalization writes
  these fields with the existing 0062 Kodi error in one transaction. Exact
  replay reads the stored fields without Kodi dispatch or log observation.
  MCP appends only a matched observational sentence to the existing failure
  text. Successful smoke, the fixed negative diagnostic, external targets,
  production, Bridge, and delayed post-success failures remain outside scope.

## D-0405: Recover only an exact timed-out disposable workspace smoke

- A timed-out dispatched workspace smoke remains an immutable ambiguous
  execution. Recovery has its own one-shot durable operation and never rewrites,
  redispatches, deletes, or reclassifies the original execution. Original
  request replay still reports its original ambiguity.
- Migration 0065 records the smoke intent, exact managed container ID, and
  managed runtime state revision atomically with the pre-dispatch marker. The
  execution already binds client, workspace, target, successful deployment,
  generation, and addon; the deployment retains the immutable runtime revision
  and destination digest. Historical NULL identity is recovery-ineligible.
- V1 admits only one timeout-classified ambiguous smoke on a managed disposable
  target, with the bound workspace still ready, its source still attached,
  the original client/workspace binding, source and target grants,
  and both existing `target.control` and `development.execute` permissions.
  External, persistent/protected, fixed diagnostic, non-timeout, undispatched,
  multiple-blocker, and identity-mismatch cases remain blocked. No caller may
  supply Docker identity or a recovery action.
- The separate operation owns a runtime-invalidating mutation lease and state
  revision through containment and verification. Broker stop targets the exact
  original container and must return and reread a stopped postcondition. This
  quiescence proof ends the original script even when Kodi had remained
  responsive. The supported exact-container start path restores that same
  managed instance. Kodi readiness, exact deployment destination verification,
  addon version and enablement, and final container/process identity are all
  required for `recovered`.
- `recovered` is the sole outcome that removes that execution's reconciliation
  blocker. `contained_but_not_ready` explicitly retains a blocker after proven
  containment; `still_requires_admin` retains it when containment is unproven.
  Any other reconciliation record remains independent. Exact recovery request
  replay returns the same operation without another lifecycle call. An expired
  recovery lease terminalizes as `still_requires_admin`; no exactly-once Docker
  effect is claimed after an unconfirmed Broker response.
- The application service is transport-neutral so a future Web administrator
  action can use this same primitive. V1 exposes only the narrow MCP tool;
  Web action and historical-row repair are outside this decision. No generic
  reconciliation, Docker, remote action, or Bridge authority is added.

## D-0406: Record later containment of historical workspace execution ambiguity

- Pre-0065 workspace executions can have a dispatched timeout ambiguity but no
  trusted original container identity. They remain ineligible for D-0405
  recovery. A separate immutable administrative containment resolution records
  a later fact; it never changes the execution or its replay.
- Eligibility requires exactly one blocking historical workspace execution on
  the same managed disposable instance, a successful matching deployment, a
  later successful lifecycle STOP as the latest lifecycle operation, no active
  mutation lease, durable stopped state and an expected runtime revision.
  Current broker inventory must identify exactly one stopped owned container
  whose image ID matches the instance's pinned runtime artifact.
  A stopped Docker container has no running process in its runtime. The
  resolution holds SQLite's writer reservation through that observation, so
  supported lifecycle and other leased mutations cannot race its proof.
- The record binds one execution, the managed instance, the local administrator,
  request ID, retained STOP operation, observed runtime revision, and completed
  stopped proof. Duplicate execution or request IDs are forbidden. Its STOP
  history is retained during normal lifecycle pruning. The record is immutable
  and a populated migration cannot be downgraded.
- Reconciliation omits only the exact historical execution with a completed
  valid containment record. Other blockers still block. START remains a
  separate ordinary lifecycle action after containment. No lifecycle effect or
  historical container identity is inferred by this action.
- Only the authenticated local administrator Web API can request this action,
  with same-origin, CSRF, exact request ID, expected revision, and explicit
  confirmation. No MCP tool or grant exposes it. The Web presentation may be
  added separately; the API communicates the original ambiguous state.

## D-0407: Explain MCP workspace capture concurrency at the tool boundary

- Real coding-agent runs exposed ambiguity about `workspace_capture`'s
  `expected_generation`: it is the currently observed workspace generation,
  not the generation requested for creation. The existing optimistic
  concurrency and no-op capture behavior were correct, but the advertised
  descriptions and generation-conflict error were unnecessarily sparse.
- Keep the explicit pre-capture generation check and the later conditional
  database update. A changed capture advances one generation; an unchanged
  source leaves it unchanged. A pre-admission generation conflict reports the
  authorized workspace ID, supplied generation, observed generation, and that
  no capture occurred. The observation may already be stale, so guidance
  requires a fresh `workspace_status` before any deliberate retry.
- Keep the existing first MCP error line and `isError=true`; append bounded
  text without changing the closed success schema. Clients comparing the entire
  error text byte-for-byte will see additional lines. There is no automatic
  retry or rebase, permission expansion, new tool, or migration. Related tool
  descriptions distinguish workspace IDs and timeout-ambiguous execution
  recovery without changing their behavior.

## D-0408: Make the MCP workspace edit contract explicit

- The existing `workspace_edit` mutation guard was safe, but its advertised
  schema did not explain the mode payloads. Keep the flat wire shape and closed
  validation. Expose conditional schema requirements and field descriptions:
  create and replace take `content` as the complete initial or resulting file;
  exact patch takes the complete exact current file as `expected_text` and the
  complete resulting file as `replacement_text`. `expected_generation` is the
  currently observed workspace generation before mutation.
- A stale generation retains `workspace_generation_conflict`. An exact patch
  preimage mismatch now returns `workspace_patch_preimage_mismatch` instead of
  the misleading generation code. Both edit errors keep the MCP `isError=true`
  envelope and add bounded recovery guidance without file content or paths.
  Clients branching on the old code for preimage mismatch must recognize the
  new code; successful request fields and results stay compatible.
- Ownership, source binding, path confinement, UTF-8 and size limits,
  generation CAS, exact preimage matching, atomic publication and rollback,
  and the absence of an edit replay ID are unchanged. No migration or new
  authority is required.

## D-0409: Apply managed runtime pins to stopped-container observation and actions

- Practical functional use found a stopped Nexus container whose runtime revision
  label differed from its persisted immutable pin. Inventory now carries the
  validated label. Stopped status uses the existing `runtime_revision_mismatch`
  failure, with no normal lifecycle actions; reset and snapshot admission also
  reject a known mismatch. A missing revision observation fails closed.
- Start and Stop resolve the current persisted pin at the broker and compare it
  with fresh container metadata before either Docker mutation or an idempotent
  result. Existing ownership, container-ID, state, and postcondition checks
  remain. Status is advisory: a later revision change still fails at dispatch.
- Snapshot creation retains the approved
  `runtime_revision_precondition_failed` reason when the broker reports that
  exact known failure; other broker failures keep their existing bounded
  presentation. Runtime projection CAS and lifecycle history are unchanged.
  This decision adds no automatic recovery or special handling for the Nexus
  evidence container.
- Live practical verification exposed a separate Delete entry point that did
  not consult the same mismatch status. The dashboard and confirmation page now
  block a known mismatch, and direct Delete admission checks it before queuing.
  Broker Stop/Deprovision revision checks remain the dispatch-time protection.

## D-0410: Explicit administrator reconstruction of a stopped revision mismatch

- Practical functional use found that D-0409 correctly contained a stopped
  wrong-revision managed container but left no supported way to reconstruct the
  same disposable instance. Add an administrator-only **Reconstruct managed
  runtime** operation. It is eligible only for one provably owned, stopped
  container with a valid observed revision different from the persisted pin.
  Matching, running, missing-label, ambiguous, and unowned containers are not
  eligible. Ordinary lifecycle and broker revision checks remain unchanged.
- The confirmation warns that the current writable layer is discarded, no
  safety Snapshot is created automatically, and external preservation is the
  administrator's responsibility. Snapshot success is not a prerequisite.
  A comparison token binds the exact observed container, image, old revision,
  desired pin/image, and runtime-state CAS value; it grants no authority
  without the authenticated administrator session, CSRF check, and fresh
  server inspection.
- Before removal the application and broker recheck the immutable desired pin,
  its approval and local image/network availability. A new closed broker
  operation independently verifies the active typed reconstruction and lease,
  owned CAS, exact old ID/image/revision, persisted desired pin, Kodi Lab labels,
  deterministic name, and stopped state. Only that exact container can be
  removed non-forcibly without volume removal, followed by exact-ID and
  fixed-label absence proof. There is no generic force or ignore-revision mode.
- The operation records confirmed identity, phases, old-state disposal, fresh
  container identity, bounded failures, and interrupted reconciliation. After
  proven removal, ordinary provisioning recreates a fresh stopped container
  from the same pin and instance identity. Container-bound deployment evidence
  is invalidated. Fixture assignment, attachments, and Snapshot history remain;
  an existing assigned fixture is applied through the existing reset baseline
  path. Fixture failure retains a provably stopped, correctly pinned container
  and is reported separately from runtime reconstruction.
- A definitive failure before removal leaves the old container. Proven removal
  followed by definitive provision failure leaves the instance coherently
  unprovisioned with its pin unchanged, allowing ordinary provision or reset.
  Uncertain broker or process outcomes require read-only reconciliation, with
  exact old, absent, expected fresh stopped, and ambiguous states distinguished.
  Nothing retries destructive work automatically. An administrator may resolve
  a proven state; any subsequent destructive retry requires a fresh
  confirmation and lease. Ambiguous ownership still requires intervention.
- Web/API is the only reconstruction authority in V1. The installed `lab`
  command remains status-only for this condition and now prints the existing
  `runtime_revision_mismatch` failure rather than an ordinary stopped summary.
  No agent-facing MCP capability is added.

## D-0411: Reaffirm an exact stale development-source materialization without overwrite

- Practical Matrix development exposed a recovery dead end: an unchanged
  workspace deployment found the installed add-on disabled, then marked the
  source copy stale; Enable required verified copy confidence, while Copy/Sync
  refused the existing destination solely because confidence was stale.
- An unchanged workspace sync preserves verified source-copy confidence only
  when its trusted result matches the same runtime, container, source digest,
  and complete destination digest. Changed or uncertain syncs retain their
  existing stale or unknown transitions.
- A stopped-target Copy/Sync may reaffirm **stale** successful same-instance,
  source, add-on, runtime, and container evidence only when the broker freshly
  captures the complete destination, finds its digest equal to the retained
  successful digest, captures the source twice, finds its digest unchanged from
  that evidence, and proves there is no delta to apply. It performs no Docker
  write. The ordinary successful Copy/Sync record then restores verified
  confidence. Missing/unknown evidence, changed source, destination drift,
  extra files, and identity drift remain replacement conflicts.
- This does not infer provenance from file existence or stale confidence alone,
  add a force option, or permit overwrite of an untrusted destination. The
  broker remains the filesystem authority. A disabled inventory result is
  retained in failed workspace deployment evidence so the enablement reason
  is distinguishable from a missing or wrong-version add-on.


## UI observation V1 (2026-10-07)

The fresh-script transport below is the original implementation checkpoint;
D-0412 and D-0413 supersede it with lifecycle-owned resident Bridge trust.

- `inspect_ui_state` reuses `target.inspect` and the target adapter boundary.
  A provider-independent closed schema contains actual focus and explicitly
  associated container/item state, with null unavailable values and deterministic
  summary. GUI observations are bounded and non-atomic.
- One generic protected hybrid collector serves qualified Matrix 19.4, Nexus
  20.5 and Omega 21.3. Direct generic ListItem reads are prohibited following
  the preserved Kodi collector-bookkeeping crash; labels/conditions use the GUI
  messenger. No Kodi-major field/provider branches are introduced.
- An authenticated, empty-payload Bridge operation serves paired external targets.
  Managed targets have no external pairing authority; their fixed installed
  Bridge script uses UUID-correlated bounded native transport. Inspection never
  installs/enables the Bridge. Native fallback has explicitly lower association
  richness. Existing production review, identity and transport checks remain.
- Per-target host admission gates aggregate collectors, independently of GUI
  navigation. Bridge collectors also serialize inbound acquisition. Paths,
  artwork, arbitrary condition catalogs and visible-item enumeration remain V2.

## D-0412: Use the resident authenticated Bridge for bounded UI observations

D-0413 resolves the provisional managed pairing-source assumption below.

- Keep schema v1, focus/container/item association and protected hybrid collection
  independent of transport. No generic direct ListItem reads are permitted.
- Managed observation is intended to use a durable target/runtime/container
  and Bridge identity binding with existing encrypted request-authentication secrets.
  Existing pairing owns OTP policy, onboarding, and credential establishment.
  Observation consumes its trusted established result; no new OTP flow is added.
  Integration is blocked on an authoritative managed pairing source: the existing
  pairing records and coordinator accept external targets only. The observation
  binding must not become a second trust or credential owner.
- Retain per-call authorization, fresh broker admission and native application identity,
  signed per-response Bridge identity and the final runtime-pin check. Revocation or
  replacement cannot authorize an already in-flight response.
- Reuse HTTP client setup, but close request connections; the persistence prototype
  exposed a Kodi service-stop issue with idle keep-alive workers. Collectors check
  service cancellation between GUI batches. Do not claim GUI-transaction atomicity.
- Unavailability may take one conservative native fallback. Auth/identity mismatch
  fails closed; it does not justify a weaker provider or a hidden rebind.

## D-0413: Managed lifecycle owns resident Bridge trust without external pairing

- This resolves the managed trust-source blocker in D-0412 and supersedes the
  earlier fresh-script managed observation transport. Managed targets do not
  enter the external OTP pairing registry, coordinator or onboarding flow.
- `managed_bridge_trust` is the authoritative managed record. It references the
  target and runtime revision, pins the admitted container and authenticated
  Bridge UUID/protocol/build identity, and records state and generation. Existing
  target-bound encrypted secret storage is reused as a storage primitive;
  managed lifecycle owns credential creation and retirement.
- After ordinary start/readiness, lifecycle preparation may establish trust for
  an installed, enabled approved Bridge. A prepared credential reference precedes
  one fixed broker operation in the exact owned, validated container. Signed
  identity confirmation and fresh runtime/generation checks precede activation.
  No caller secret, code or filesystem path is accepted by that operation.
- Observation only consumes active trust. It never installs, provisions,
  repairs or retires credentials. Missing trust leaves existing native managed
  operations available and permits only conservative native observation.
  Authentication failure, revocation or identity drift fails closed.
- Same-container restarts reuse persistent identity and credential state.
  Replacement, reset, restore, revision changes and deletion invalidate host
  authority and retire the encrypted credential. Restored profile bytes are not
  authority to reuse an old binding. Confirmed same-container drift or revocation
  requires explicit managed lifecycle replacement, not observation recovery.
- The resident collector, protected GUI dispatch, per-target gate, connection
  closing policy and schema semantics remain unchanged. External pairing and
  credential ownership remain unchanged; managed establishment uses no user OTP.

## D-0414: External screenshots have an independent capture preference

- Screenshots are read-only inspection. An administrator can disable external
  capture through a persistent preference without changing technical capability,
  Bridge credentials, identity/trust generations, MCP scopes, transport
  acknowledgements or production control policy and approvals.
- The preference has its own monotonic revision and last administrator/time.
  Updates use compare-and-swap. New external records initialize enabled when their
  configured maximum includes screenshot; migration copies that maximum without
  changing existing trust or grants. Missing preferences fail closed.
- Capture admission, cached replay and result publication require enabled policy.
  Results bind the admitted preference revision, including the final MCP
  publication check. Off/on cannot publish an old capture or revive cached pixels.
  Durable operation receipts remain, preventing a completed request from being
  redispatched when volatile pixels are discarded. Bridge receipts also bind the
  preference revision durably: a pending request surviving process loss cannot
  adopt an old capture after off/on. Legacy receipts bind the initial revision.
- Ordinary client permissions, target authorization, technical capability and
  authenticated transport remain required. Production reviewed builds, OTP,
  identity drift and revocation retain their existing behavior. Managed screenshots
  and lifecycle trust are outside this preference.
- Historical production screenshot capability removal remains a separate,
  destructive compatibility operation. Re-enabling the preference creates no
  credentials, grants or scopes and cannot restore retired authority. Changed
  preferences refuse downgrade to a schema that cannot enforce them.

## D-0415: One administrator operation establishes production read-only access

- A selected existing compatible client can receive one exact production target
  grant, its current native inspection scope and, when its immutable preset and
  the target support screenshots, the current Bridge-bound visual scope in one
  transaction. No credentials, client presets, control scopes or trust are created
  or repaired. No other client or target is enrolled.
- Existing production preset contracts remain required. Production Inspection,
  Production Visual Inspection and Production Remote Kodi clients are compatible;
  ordinary development-profile Read-only Inspection and custom clients remain
  incompatible. Missing `target.inspect` is reported explicitly. A client without
  `target.visual` receives inspection only. The operation does not remove authority
  from an existing control relationship; that relationship requires explicit review.
- Requests bind expected client authority revision, target policy revision, native
  identity generation and applicable Bridge trust revision. SQLite writer
  reservation precedes all reads; validation, policy initialization, grant, scopes
  and their existing security events commit together or roll back together.
- A target without prior assignment/history receives the existing production
  read-only preset appropriate to its configured maximum. Existing target presets
  are preserved. Incompatible or narrowed target policy requires administrator
  intervention rather than an automatic expansion affecting other agents.
- Current active read scopes are reused. Missing scopes can be created only without
  corresponding historical scopes; revoked/stale bindings and removed grants need
  explicit review through existing administration. A disabled client or one without
  an active credential is not repaired. Repeated current-state requests write no
  duplicate authorization or audit; callers refresh expected state after initial
  preset assignment advances the existing target policy revision.
- Screenshot preference is independent: an off preference does not prevent scope
  establishment, and existing admission/replay/publication gates still deny capture.
  Reviewed builds, authenticated transport, exact HTTP acknowledgement, native trust,
  target isolation and production mutation/development vetoes are unchanged. This
  operation performs no live Kodi I/O and adds no MCP tool or Web UI workflow.

## D-0416: Durable household Connect coordinates existing production trust

- Household Kodi remains `external_production`. An authenticated Web administrator
  coordinates registration, bounded native observation/confirmation, production
  designation, safe enablement, explicit HTTP acknowledgement, reviewed OTP
  pairing, read-only target policy and final fresh verification. Existing native
  and Bridge audit/provenance records remain authoritative. No MCP grants/scopes,
  remote-control consent or managed lifecycle changes are introduced.
- A caller-retained operation UUID, durable preallocated target UUID and active
  endpoint reservation precede registration. SQLite endpoint guards prevent other
  administrator paths from adopting that reservation. POSIX locks serialize workers
  sharing the same data-root, and release on process loss. Different operation IDs
  at an occupied endpoint conflict; retries never adopt a target by name/version/IP.
- Initial Connect stages an accurate endpoint/identity/revision-bound HTTP
  disclosure. Only explicit acceptance of a previously returned current disclosure
  records acknowledgement through the existing service. Pairing secrets and pixels
  remain subject to the existing unencrypted HTTP risks; consent cannot be invented.
  The safe target maximum is assigned before disclosure because assignment advances
  native policy revision. A durable native binding pins the reserved endpoint,
  confirmed observation, generation and resulting policy revision. Acknowledgement
  validates the expected binding and writes its audit evidence under one SQLite
  writer reservation; final publication also checks the original binding and token.
  Concurrent edits cannot silently move consent to the target record's newer state.
- A reviewed bootstrap candidate is checked before consuming OTP; it creates no
  approval or trust. Existing authenticated full identity/capability/provenance
  checks still precede commit. Unknown, retired or modified builds fail closed.
- A bootstrap checkpoint precedes dispatch. Known rejection permits explicit
  correction; uncertain outcomes never cause another OTP attempt. Existing durable
  credentials recover lost commits. Missing credential after uncertain bootstrap
  requires explicit local abandonment plus Bridge reset in Kodi. This deliberately
  bounds recovery without redesigning the shared OTP state machine.
- Intermediate native enablement cannot admit agents: production admission checks
  require the household operation to be connected. Final SQLite publication binds
  the fresh native/Bridge snapshots and Production Visual Inspection maximum.
  Mutations remain suspended, screenshots use their independent initial preference,
  and a later administrator preference is preserved on retry. Explicit abandonment
  retires local trust/deactivates the target, preserves history and releases the
  endpoint reservation; it never resets or controls the remote Kodi.
  Retried cleanup accepts an already deactivated target, so interruption before
  recording abandonment can converge without restoring credentials or authority.
- Authority retirement and file deletion are separate durable steps. Existing shared
  Bridge revocation/native invalidation and native credential deactivation journal
  only opaque reference/store/target identities in the authority-retirement transaction.
  New external credentials register an obligation before secret-store writes; a
  serialized publication transaction rechecks lifecycle state and atomically transfers
  the reference to live ownership. Replacement journals displaced references in that
  transaction. Cleanup rechecks obligations under the same SQLite writer lock and
  deletes both final and interrupted temporary files before acknowledgement.
  Deletion obligations survive restart and are acknowledged only after idempotent
  file deletion succeeds. Abandonment checks residual Bridge trust after deactivation
  and cannot release its reservation while pending deletion obligations remain.
- Only the shared OTP service's definitive pre-dispatch/pre-consumption classification
  may reopen the bootstrap checkpoint. A post-consumption identity/protocol failure
  remains uncertain even when unsigned discovery metadata is replayed. Persisted
  credentials recover the existing transaction; otherwise reset/abandonment is required.
- Migration 0071 adds coordinator persistence/reservation guards and the opaque
  credential-retirement journal, and refuses populated downgrade. Historical pre-coordinator migration fixtures retain their
  ordinary production contracts. See `HOUSEHOLD-ONBOARDING.md` for API, transitions,
  deployment assumptions and the conservative pre-persistence recovery limitation.

## D-0417: Shared server-rendered Web administration navigation

- Authenticated admin pages inherit `web/templates/base.html`, which includes
  one navigation component. Pages retain their existing content, form hooks and
  scripts, and select the active Home, Kodi, Agents, Development or Settings
  section. Login remains a separate unauthenticated page.
- `/kodi`, `/development` and `/settings` are authenticated, read-only landing
  pages linking to existing administration. Agents uses `/mcp-clients`; Home
  retains the managed instance overview. This adds no backend authority,
  configuration mutation, migration, pairing or deployment workflow.
- A compact desktop sidebar becomes an in-flow disclosure menu at widths up to
  960 pixels. Its button reports expanded state; hidden links leave keyboard
  navigation; Escape returns focus. Without JavaScript, links remain visible.
- The application enables the new navigation through Jinja globals. Older
  running processes sharing this checkout's templates retain links to existing
  routes until their own authorized restart, avoiding absent landing routes.

## D-0418: Automatically recognize this release's verified official Bridge

- Ordinary users install the single Kodi Lab Narrow Bridge add-on through the
  built-in stable repository. They do not separately review official builds.
  Existing release staging already binds the clean source inventory, builder,
  semantic fingerprint, exact ZIP digest and bundle inputs; the stable publisher
  emits verified package/index hashes and an immutable generation identity.
- Web startup can insert the existing immutable reviewed-build record only after
  the installed generation validates against the deployment's configured bundle
  digest/base URL and the application's compiled official Bridge ZIP digest.
  Missing pins/artifacts, mismatch or an incomplete transition fail closed.
  A remote claim, arbitrary uploaded ZIP or self-consistent unofficial feed cannot
  establish this authority. Startup verifies installed package identity; its
  bundle field comparison does not independently hash actual BUNDLE.json bytes.
- Recognition preserves both uniqueness keys and serializes lookup/insert. It
  never overwrites or broadens an existing manual/conflicting record or revives a
  retired release. It writes no target, pairing, policy, permission or managed
  trust records. Retirement and custom-build administration remain operational.
- Future compatible Kodi Lab releases update the existing release and backend
  compatibility pins, then register their own verified artifacts automatically.
  Older backends do not accept arbitrary future versions. All existing native
  identity, protocol, OTP, signed Bridge identity, consent, production provenance
  and authority checks stay in force. Development pairing stays on its existing
  workflow, without additional grants or a production registry requirement.
- This automates the existing local build allowlist. The public semantic
  fingerprint and OTP/HMAC identity are not attestation of remote installed code.
  Stronger remote artifact attestation would require separately authorized design.

## D-0419: Opt-in native LAN MCP through an HTTPS-only narrow proxy

- The existing shared Web `/mcp` Streamable HTTP authority is reused, with exact
  `2026-07-28` and `2025-11-25` support. No replacement server, identity store,
  permission default, grant or scope changes are introduced. Local loopback and
  modern-only stdio remain available.
- An administrator configures an actual LAN HTTPS `/mcp` URL, certificate,
  explicit private bind and subnet ACL in an external proxy. Compose keeps its
  plaintext Web publication on loopback. The supplied nginx template proxies
  only exact `/mcp`, preserves Host/Origin/bearer headers, rejects queries and
  disables request logging; it is not automatically deployed.
- MCP validates Host/Origin before protocol/auth dispatch, rejects duplicate
  authority/auth/protocol headers and bounds wildcard port matching. Only the
  exact configured HTTPS authority/origin is added. Existing SDK validation and
  all authentication/authorization remain in force. Application access logging
  redacts MCP request paths/queries so a rejected query cannot disclose a token.
- Agents prefers LAN only after a bounded credential-free TLS request verifies
  the anonymous Bearer 401 and an opaque per-process instance marker. Missing,
  redirected, untrusted, public-resolving or mismatched endpoints fail closed to
  Local / stdio. Readiness grants no authority, does no Kodi I/O and cannot prove
  connectivity or certificate trust on another agent computer.
- See `docs/NATIVE-LAN-MCP.md` for certificate/configuration prerequisites and
  protocol/client limits. Qualification uses isolated state and synthetic Kodi
  adapters; real second-host acceptance requires separate retained evidence.

## D-0420: Optional unencrypted MCP on an explicitly trusted private LAN

- Local-only exposure remains the default. HTTPS settings and certificate
  validation are unchanged. A separate HTTP enable flag requires a complete
  exact `/mcp` URL, explicit RFC1918 IPv4 bind address and private subnet ACL.
  Every DNS answer must equal that interface for anonymous readiness; the
  connection is pinned to that address, without proxies, redirects or a token.
- A separately installed MCP-only proxy uses that interface/subnet, preserves
  bearer/Host/Origin headers, strips cookies, rejects queries and denies other
  routes. Bootstrap settings do not start a listener or change publications.
  HTTP and HTTPS reuse the same transport, identities, permissions and grants;
  there is no migration or automatic authority expansion.
- Agents distinguishes HTTPS, Trusted LAN HTTP and local/stdio. HTTP is never
  selected automatically. Its warning explains plaintext token and traffic
  interception, modification and replay. Marker readiness proves reachability,
  not authenticated server identity. HTTPS remains preferred when available.
- Setup and rollback are documented in `docs/NATIVE-LAN-MCP.md`. Qualification
  uses disposable services/credentials and direct second-host requests. No
  production activation or firewall/router change is part of this work.

## D-0421: HTTP internal proxy health is separate from LAN acceptance

- Supersedes D-0420's server-originated HTTP ingress readiness check. Web's
  Docker source is intentionally outside the trusted client subnet; it must
  not need an IP/firewall exception merely to verify its own proxy/upstream.
  HTTPS configuration, probe and existing deployment behavior remain unchanged.
- The HTTP proxy adds a filesystem Unix listener to the same MCP-only server
  block, with `allow unix:` alongside the existing private TCP subnet ACL.
  Exact Host/Origin, `/mcp`, bearer gate, upstream, cookie/query restrictions and
  permission/grant authority remain identical for both listeners. No health TCP
  port, administrative endpoint, Docker subnet or second authority is introduced.
- Installed Web mounts only a stable protected root:10001 directory read-only.
  Proxy master group 10001, directory 0750, no symlinks or extra POSIX access
  ACLs, and preservation of the directory inode across stop/start are required.
  Nginx's socket 0666 is confined by that directory. Default Compose does not
  mount or start this optional path; the opt-in overlay refuses missing paths.
- Internal readiness validates safe metadata, private DNS/bind configuration,
  anonymous same-instance 401 Bearer and exact bounded authentication-failure
  JSON over that socket, with a two-second whole-operation deadline. It fails
  closed on missing/stopped proxy, wrong upstream, unsafe filesystem metadata,
  malformed/oversized response or timeout and never falls back to LAN ingress.
- Agents describes internal service health accurately. It cannot establish
  external LAN/WAN connectivity or successful authenticated tool access; direct
  acceptance from a genuine allowed LAN client remains a separate activation
  prerequisite, together with firewall/router exposure verification and fresh
  state/rollback preservation. HTTP is still never selected automatically.

## D-0422: Inactive RC1 OSTree acquisition foundation

- Phase 2 adds a closed `flathub_ostree` acquisition definition independently
  of the unchanged v1 signed-APT recipe model. The schema-2 RC1 definition is
  Preview, development-only, user-triggered, and explicitly inactive. It is not
  in the installable recipe registry, CLI selectors, Web actions, or broker
  generation allowlist. Parsing and acquisition evidence confer no approval.
- Wheel-owned configuration fixes the three x86_64 refs, immutable commits,
  metadata hashes, Kodi source revision, public-key bytes, primary fingerprint,
  and authorized signing subkey. Acquisition has no request-supplied URL,
  revision, key, destination, command, or Dockerfile. No moving-channel fallback
  or maintainer-hosted binary archive is supported. Only metadata/signature
  test fixtures and public trust material are included; no Kodi payload is
  packaged in the wheel or repository.
- The acquisition component receives a trusted private worker staging root,
  creates fixed operation-scoped children, and holds a nonblocking file lock.
  All path ancestors and sensitive files are opened without following symlinks.
  Commands use fixed argument arrays, a controlled environment, time/output/
  process/file/disk limits, and process-group cancellation. Disposable fresh
  repositories are used; persistent cache reuse is not implemented. Remnants
  from hard termination are rejected, never treated as verified or silently
  deleted. Normal failure/interruption and context exit remove only owned work.
  GPGME's detached agents are stopped through pidfds only when their kernel UID
  and exact private operation home match; other GPG homes and agents are untouched.
- Verification requires OSTree integrity, GPG verification, exact full signer
  identities, signed ref/collection bindings, architecture and dependency
  metadata, upstream source and RC1 release metadata, critical content hashes,
  and complete checkout inventories. Reviewed Freedesktop absolute symlinks
  remain hashed data and are never traversed on the host. Evidence explicitly
  covers only the three OSTree trees, not supporting Ubuntu dependencies or a
  managed image; existing APT attestations and eligibility remain unchanged.
- Supporting dependency closure, final image assembly, native add-on loading,
  lifecycle validation, installation attestations, Preview admission, capability
  approval, and Web/broker integration require later qualification. No database
  migration, installation, runtime approval, image build, or deployment is part
  of this foundation phase.

## D-0423: Inactive RC1 local image assembly and acceptance

- Phase 3 adds a separate inactive OSTree assembly entrypoint alongside the
  unchanged signed-APT assembler. No catalog, worker command, broker allowlist,
  eligibility, database, or Web installation action is activated.
- Acquisition yields a process-local live operation lease, invalidated on
  context exit. Assembly copies through descriptor-relative nofollow reads,
  recomputes the Phase 2 inventory over the bytes actually copied, and compares
  every component identity, inventory, count and byte total. Docker receives a
  bounded compressed anonymous memfd with kernel write/grow/shrink seals. Mutable
  checkout paths and a verification report alone cannot authorize assembly.
  The descriptor closes on success/failure/interruption; acquisition removes its
  own staging. No persistent binary cache is introduced.
- A wheel-owned Ubuntu 26.04 support manifest pins the base digest and all 97
  additional/updated DEBs, versions, architectures, exact repository paths and
  SHA256 values. Review evidence binds each package to Ubuntu-signed InRelease
  and Packages hashes. Subsequent downloads require exact hashes and sizes;
  missing historical packages fail without replacement. Docker builds offline;
  APT orders only explicitly supplied local DEBs with repositories disabled and Docker networking disabled, then APT, gpgv and the base signing-key package are removed. The final
  installed package inventory must equal the reviewed 178-package closure.
- The final stage copies the merged support filesystem, omitting archive/cache
  layers, and the exact upstream app/Freedesktop/Mesa trees. Libraries remain in
  /app, /opt/fdo and /opt/mesa. The explicit Freedesktop loader is retained, as
  are Python 3.14 library paths and Mesa llvmpipe configuration. Acquisition,
  compiler and diagnostic toolchains are not added. No Kodi binaries enter the
  repository or wheel. Fixed image assets are separately hash-bound in evidence.
- The trusted assembly creates the missing xbmcbin/addons symlink to
  /app/lib/kodi/addons, rejecting any preexisting destination, and installs the
  fixed loader-based Xrandr wrapper. Native acceptance uses separately verified
  upstream Greynetic 22.0.5 as a disposable container test fixture; it does not
  expand the production acquisition component closure or ship this add-on.
- UID/GID 1000, display :99 at 1280x720x24, container-local session D-Bus, internal
  port 8080 and fixed profile remain the contract. The supervisor budgets eight
  seconds within the broker's ten-second Docker stop timeout. Normal stops must
  quit Kodi, reap the session and Xvfb, exit zero and leave no display artifacts;
  forced termination is explicit failure evidence. Runtime acceptance tests use
  isolated containers without mounts/devices/ports/capabilities or host D-Bus.
- Stock upstream TagLib 2.3.1 is preserved and its missing Kodi Matroska fast-scan
  patch is disclosed. Basic development-runtime acceptance does not claim media
  metadata equivalence to a patched Kodi dependency build. No overlay is added.
- Assembly evidence is bounded and inactive, not a signed installation
  attestation or eligibility approval. The original schema-2 recipe deliberately
  retains its unqualified/inactive policy flags pending production integration.
  Reproducibility means exact inputs and validated installed contents; Docker
  history/filesystem generation timestamps are not claimed byte-reproducible.

## D-0424: Exact Preview installation through the existing reviewed worker

- Add only the backend `piers` selector for the separately pinned RC1 installation
  descriptor. The published stable/Web recipe list is unchanged. Original APT
  and inactive acquisition resource digests remain immutable.
- Persist exact recipe/revision/release consent in the existing bounded pending
  installation evidence. The state-owning application writes it; the broker
  verifies it read-only and retains its existing peer UID and capabilities.
  Repeated pending handoffs use the same exact operation, while interrupted
  active phases use existing worker/lock reconciliation. No migration is needed.
- Keep one state machine, worker lock, state-owner transactions and HMAC seal.
  Provider dispatch uses the existing verified acquisition/assembly path and a
  fixed socketless network helper, removed before sealed context capture. The
  worker remains offline with its original mounts/capabilities; Web and Kodi
  receive no Docker socket. No Kodi binaries or persistent binary cache ship.
- A ready RC1 transaction requires its exact runtime acceptance, records Preview,
  and does not bootstrap an instance. Eligibility additionally requires exact
  scoped recipe consent, approved revision, provider provenance, image/profile,
  capabilities and unchanged HMAC binding. General prerelease opt-in cannot
  approve RC1, RC2, final or a substituted recipe/image.
- Use existing RPC/readiness/framebuffer helpers with the Phase 3 RC1 checks,
  including actual Python, FFmpeg, GUI change and clean two-cycle shutdown.
  Stock TagLib's known Matroska limitation remains disclosed, without an overlay.
- Deployed Web activation, production installation records, provisioning,
  deployment and any security-policy changes remain outside this phase. See
  `docs/RC1-INSTALLATION-BACKEND.md` for implementation and activation boundaries.

## D-0425: Development Web admission for the exact reviewed RC1 runtime

- Extend existing runtime cards, polling and actions with the exact `piers`
  reviewed selector in Development view. Installation and instance creation
  require the typed recipe/revision/release acknowledgement on the backend.
  Ready means independently eligible sealed evidence and exact local image
  presence. Preserve stable recipe bytes, installation evidence and defaults.
- Create the disposable RC1 pin through `ManagedInstanceService`. Its initial
  fixture is unassigned; normal reset therefore recreates a clean profile.
  Do not inherit a Beta fixture's approval. Clarify that distinction on the
  existing reset confirmation page.
- Use a single periodic screenshot preview and the existing authenticated
  screenshot path. Pause, bounded intervals and hidden-tab suspension do not
  grant control authority. Reuse managed UI observation for a bounded read-only
  window/item projection; remove the obsolete Live display controls from the
  instance page while retaining the existing mediated API compatibility.
- An exact reviewed text overlay generates Bridge 1.1.16 with optional canonical
  `kodi_release` while preserving its major/minor field. Keep the stable Bridge
  source, publisher and original 1.1.15 approval descriptor byte-for-byte intact;
  select the new reviewed descriptor only for exact RC1. Require exact release
  identity for prereleases; preserve legacy identity serialization. Generic
  service-ZIP installation remains unsupported. The candidate is qualified
  using a reviewed experimental fixture, not an expanded ZIP permission.
- Docker's null and empty CMD encodings both mean no entrypoint arguments.
  Normalize only those two representations in image-default verification;
  supplied commands, entrypoints, health checks and security checks stay exact.
- Reset retires resident Bridge authority. The existing consumer refuses an
  invalidated anchor until lifecycle trust is re-established; do not weaken
  that gate to conceal the limitation. Production activation, deployment,
  publication and changes to production state remain separately authorized.

## D0426 — RC1 reviewed Bridge installation and reset recovery

Use a closed wheel-owned candidate overlay for exact Kodi 22.0 RC1, admitted by
an explicit administrator action under the existing START lifecycle operation
and mutation lease. Extend the existing prepared-trust broker operation with a
strict optional installation flag; default stable wire requests remain unchanged.
The broker independently requires the live typed lease and verifies the complete
fixed candidate inventory. Generic uploaded service ZIPs remain rejected.

Reset retires the old authority. The same explicit reviewed action after native
startup installs the candidate into the clean profile and confirms fresh resident
trust. Observation cannot repair or adopt an invalidated anchor. MCP permissions
and target grants are unaffected. See [RC1 Bridge and installation UX](RC1-BRIDGE-AND-INSTALLATION-UX.md).

## D-0427: Recover a completed non-timeout ambiguity only after independent stop

- Extend D-0405 only for a dispatched `smoke` with classification `ambiguous`,
  failure `execution_ambiguous`, terminal ambiguous state, finalization phase,
  complete original container/runtime identity, and no retained result or Kodi
  error. This does not establish why its response was ambiguous or infer success.
- Require the latest associated lifecycle operation to be a successful,
  reconciliation-free stop, requested strictly after the completed execution.
  All execution/stop timestamps must be coherent UTC and not in the future.
  The managed projection must be stopped at a later revision, and fresh broker
  inventory must identify the original container as stopped. Missing, stale,
  contradictory, running, or competing evidence fails closed.
- Non-timeout recovery also requires the workspace's current generation to match
  the exact successful deployment/execution generation. Timeout gates are unchanged.
  Existing identity, ownership, grants, disposable-target, sole-blocker, lease,
  runtime pin, broker isolation and authorization checks remain unchanged.
- Eligibility is rechecked under the existing write transaction. The existing
  exact-container stop and reread, start, readiness, destination provenance,
  add-on inventory and final process/container checks remain mandatory. A new
  recovery receipt binds the original execution and records the verified flags;
  the original execution and later stop history remain immutable. Request replay
  is idempotent; a new recovery request after resolution is rejected.
- Only a completed `recovered` receipt resolves this exact blocker. Failure keeps
  the block; an operator must leave the target stopped through supported lifecycle
  controls. No historical containment extension, generic force action, schema
  repair, original execution retry, or external/production authority is added.

## D-0428: Resolve window semantics in the central observation layer

Preserve original window/dialog IDs and labels, and add version-scoped semantic
resolution after existing provider normalization. Prefer useful Kodi names,
authoritative core definitions, exact active skin/version mappings, then
conservative classification. Candidate or ambiguous data never supplies a
verified name. Load schema-validated built-in data and administrator skin-only
extensions at startup; local files cannot override core definitions. Resolve
named/core windows without remote calls. For skin candidates, bracket a fresh
authorized observation with skin/version checks rather than associating later
identity with an earlier sample. Keep Bridge wire format, target authorization,
navigation and focus/container association unchanged. See
[Window mappings](WINDOW-MAPPINGS.md) for provenance, extension rules and tests.

## D-0429: Cache only collection scheduling

Avoid repeating a complete GUI collection on successive skin candidates by
caching only a bounded, expiring scheduling hint scoped to target/runtime
identity. The hint chooses when fresh skin/version checks surround collection;
it never supplies identity or semantic data. Container/endpoint/identity changes,
failures, absent applicability and expiry remove hints. Skin updates and profile
changes lack a complete ordered invalidation stream, so ID/version caching is
not authoritative. Every skin mapping still needs fresh matching identity on
both sides of a full authorized observation. Unknown/ambiguous summaries retain
the numeric foreground ID instead of borrowing a base-window name.

## D-0430: Return semantic UI resolution by default in schema version 2

Every MCP and HTTP UI inspection includes semantic resolution automatically.
The project is pre-release; there is no legacy projection, request switch or
parallel serializer. Preserve original Kodi IDs and labels beside one compact
semantic group per base window and foreground dialog. Keep semantic name, role,
source, status and relevant skin identity/version; mapping files retain detailed
source revisions without repeating commit hashes in routine responses.

Use UI schema version 2 to identify this deliberate contract change. Mapping
schema version 1 and the resident Bridge collection contract are independent
and unchanged. Unknown, ambiguous and unverified results retain their explicit
status, and mapping version gates, authorization and association checks remain
mandatory. See [Window mappings](WINDOW-MAPPINGS.md).
