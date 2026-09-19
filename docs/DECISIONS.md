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
  treated as an absent declaration bound during cross-checking.
- Preflight: required external dependencies must be recognized and enabled.
  Required missing or disabled dependencies block; optional missing is allowed.
  Kodi Lab never installs or automatically enables dependencies. Closed Kodi
  API/runtime dependency IDs use Kodi's own representation and existing
  inventory where possible; unclassifiable cases remain unknown.

## D-0142: Dependency versions are exact strings, not ordered versions

- Status: approved for Phase 8 copied-add-on enablement
- Decision: Kodi Lab implements neither SemVer nor generic string ordering and
  preserves declared/reported strings exactly. The available fixed Kodi APIs
  expose declarations and installed versions but no authoritative
  compatibility Boolean. Every required version constraint therefore blocks
  this first slice with `dependency_version_unverified`, including exact string
  equality; equality is not promoted into a compatibility claim.
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

## D-0209: Post-promotion Kodi verification remains a separate bounded stage

- Status: approved architecture; not implemented in Phase 9 Slice 3
- Boundary: Slice 3 performs no stop, copy/sync, start, inventory verification,
  log read, broker call, Docker action, or Kodi action. A later Kodi validation
  failure will not automatically undo a successful canonical source promotion.
  Slice 4 remains required before canonical Phase 9 patch-back is complete.

## D-0210: Promotion state and evidence cross the source boundary explicitly

- Status: Slice 3 subset approved and implemented; Slice 4 verification remains
  unimplemented
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
