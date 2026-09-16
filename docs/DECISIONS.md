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
