# Restricted broker security contract

This document derives from `V1-SPEC.md` plus approved decisions D-0018 through
D-0045. It describes the Phase 2 implementation boundary; it authorizes read-only
Docker inventory, disposable create-only provisioning, and explicit start/stop
reconciliation plus stopped-only deprovision for owned containers.

## Threat model and privilege boundary

The root broker is trusted to mediate future narrowly defined Docker operations.
The `kodi-lab` application account and all request data are less trusted. Docker
daemon access is host-root-equivalent, so application code must never receive the
Docker socket or Docker-group membership.

Primary abuse cases are unauthorized local peers, request-supplied identity,
operation-name dispatch tricks, malformed or oversized input, unsafe socket-path
replacement, handler exception disclosure, and attempts to turn semantic
operations into generic shell/Docker/filesystem execution. The implementation
uses a protected filesystem endpoint, kernel authentication, exact-UID
authorization, strict bounded schemas, a closed operation mapping, and safe
errors/audit fields as independent controls.

Root is not implicitly authorized as an application RPC peer. It already has
host privilege and does not need the broker as an escalation route.

## Production process and filesystem contract

- Broker process: root.
- Application process identity: user and primary group `kodi-lab`.
- Runtime directory: `/run/kodi-lab`, `root:kodi-lab`, mode `0750`.
- Stream socket: `/run/kodi-lab/broker.sock`, `root:kodi-lab`, mode `0660`.
- Listener family: Unix only; no TCP or HTTP listener.
- Activation: long-running service, not systemd socket activation.

The production constructor refuses non-root startup, unresolved identities, or a
`kodi-lab` user whose primary group is not the dedicated `kodi-lab` group. A
future unit should use systemd `RuntimeDirectory` facilities, but no unit is
included until package installation paths are known. On development host, the approved
eventual service name currently belongs to the protected legacy broker, so no new
unit may be installed during coexistence.

The broker validates the runtime directory with `lstat`, requires exact metadata,
and requires the socket to be its direct child. A symbolic link or non-socket at
the endpoint is fatal. An existing socket must have expected metadata and is
probed: a live listener is fatal; a refused connection marks it potentially
stale. Before unlinking, the broker revalidates the same device/inode, type, and
metadata. After binding and applying ownership/mode, it revalidates the directory
identity. Shutdown removes only the verified socket inode created by that broker.
It never deletes the runtime directory or repairs unexpected objects.

## Authentication and authorization

The complete authentication chain is Unix-socket access plus Linux
`SO_PEERCRED`. The accepted socket yields peer PID, effective UID, and primary
GID. Authorization compares only the kernel-provided UID with the numeric UID
resolved from `kodi-lab` during production startup.

PID and primary GID are audit fields. Supplementary groups, request data,
environment variables, usernames, executable paths/names, and caller-provided
UID/GID values are never authorization inputs. No bearer token, password, shared
secret, API key, or TLS layer is used.

## Protocol v1

Transport is an `AF_UNIX` stream. Every JSON document is strict UTF-8 and has a
four-byte unsigned big-endian length prefix. The maximum JSON payload is 65,536
bytes, checked from the header before reading or allocating the announced body.
The client sends one request, closes its write side, receives one response, and
the broker closes the connection. Already-buffered trailing data is rejected.
Read and write timeouts are five seconds.

The request contains exactly:

```text
version: integer 1
request_id: lowercase canonical UUID string
operation: 1-64 lowercase letters/digits/underscore, beginning with a letter
params: operation-specific object
```

Unknown fields, missing fields, invalid UTF-8, malformed JSON, duplicate object
keys, non-finite JSON constants, unsupported versions, invalid UUIDs, and invalid
operation-specific parameters fail before handler execution. The parser relies on
the small frame and non-recursive operation schemas rather than a generic depth
analyzer.

Success contains exactly `version`, `request_id`, `ok: true`, and typed `result`.
Error contains exactly `version`, a safely recoverable request ID or null,
`ok: false`, and a stable error code/safe message. Python tracebacks, exception
representations, full requests, filesystem internals, environment values, and raw
future Docker errors are excluded.

Protocol version 1 is the only accepted version. There is no negotiation or
downgrade. Request IDs correlate work but do not authenticate peers or promise
exactly-once delivery. There is no replay cache. Future mutating operations must
instead use idempotence or explicit conflict/state preconditions.

## Operation registry and client

The registry is an explicit dictionary of definitions containing a fixed name,
parameter model, result model, handler callable, and authorization requirement.
It never resolves request names through attributes, imports, executable paths, or
commands. Typed parameter validation completes before a handler receives data;
typed result validation completes before serialization.

Phase 2 contains exactly:

- `ping`: empty parameters; returns healthy status and protocol version.
- `broker_info`: empty parameters; returns package and protocol versions.
- `capabilities`: empty parameters; returns only the registered names and protocol
  version.
- `managed_container_inventory`: empty parameters; returns only the bounded typed
  Kodi Lab-owned container inventory described below.
- `provision_managed_container`: canonical managed-instance UUID only; resolves
  trusted state and creates or verifies one disposable managed container.
- `start_managed_container`: strict managed-instance, immutable container-ID, and
  normalized runtime-state preconditions; returns only verified running state and
  whether a start call was performed.
- `stop_managed_container`: strict managed-instance, immutable container-ID, and
  normalized runtime-state preconditions; returns only verified stopped state and
  whether a stop call was performed.
- `deprovision_managed_container`: strict managed-instance, immutable container-ID,
  pinned runtime-revision, and stopped-state preconditions; removes only the
  verified container and returns a minimal verified-removal result.
- `observe_managed_kodi_runtime`: strict managed-instance UUID and immutable
  container-ID precondition; trusted persisted state fixes revision, process
  health semantics, managed network, and port, and the result contains only
  normalized process state plus one canonical managed endpoint.

All require the authorized UID. There is no anonymous operation and no generic
execution, raw Docker, shell, raw payload, file, path, environment, caller-driven
inspection, generic create/remove/restart/kill/exec, or generic lifecycle
operation.

The unprivileged `BrokerClient` exposes explicit methods with those nine names.
Its shared call implementation is private. It generates canonical UUIDs,
validates outgoing envelopes, uses the bounded framing/timeouts, requires response
version and correlation-ID consistency, validates operation-specific results, and
translates transport, authorization, remote, and malformed-response failures into
typed client exceptions.

## Read-only Docker inventory boundary

`managed_container_inventory` is the only read-only Docker-backed operation. The
trusted adapter constructs the Docker API client with the
fixed local endpoint `unix:///var/run/docker.sock`, a five-second timeout, and
the fixed exact filter `io.kodi-lab.managed=true`. Caller data cannot select a
daemon endpoint, label, filter, container name/ID, or inspect parameter. The
repository uses the maintained Docker SDK for Python rather than a CLI or
subprocess boundary.

The adapter revalidates inspect data and includes a container only with both:

- `io.kodi-lab.managed=true`
- `io.kodi-lab.instance-id=<lowercase canonical UUID>`

These labels are trusted selection metadata for future Kodi Lab-created Docker
objects. They are not caller authentication; `SO_PEERCRED` exact-UID authorization
still completes before request parsing, schema validation, dispatch, or Docker
access. Unlabelled, wrong-value, legacy-labelled, and name-only lookalikes remain
outside managed scope. Exact ownership with a missing/malformed instance UUID or
a duplicate instance UUID fails the complete operation with
`invalid_managed_container_metadata`.

The result has at most 128 entries, sorted by canonical instance UUID. Each entry
contains only the managed instance UUID, immutable full container ID, immutable
image content ID, normalized runtime state, and normalized health state. More
than 128 matching summaries fails with `inventory_limit_exceeded`; it is never
silently truncated. Docker `running` maps to `running`;
`created`/`exited`/`dead` map to `stopped`. Restarting, removing, and paused
states map to `transitional`, and other strings to `unknown`. Health is
`healthy`, `unhealthy`, `starting`, `not_configured`, or `unknown`.

Environment, command/entrypoint, mounts and host paths, raw labels, networks and
addresses, ports, logs, daemon information, capabilities/security options, raw
inspect responses, mutable image tags, and arbitrary annotations are excluded.
The adapter has no generic create, restart, remove, exec, image, volume, network,
label-mutation, target/mutation, or raw passthrough method. Its only public
mutations are semantic provision/start/stop/deprovision methods; provision alone
may perform proof-gated rollback removal of the exact container it just created.
Inventory observations are not persisted in SQLite.

## Fixed Kodi process and endpoint observation

`observe_managed_kodi_runtime` is the only broker addition for Phase 3
readiness. Authorization still completes before framing, parsing, validation,
resolution, or Docker access. The request accepts only canonical
`managed_instance_id` and a previously observed immutable 64-hex container ID;
there is no network, port, URL, HTTP, JSON-RPC, method, command, or inspect input.

A read-only SQLite resolver loads the ManagedInstance's exact revision and its
immutable trusted execution profile. Fixed ownership labels select exactly one
container. Fresh inspect must prove immutable ID, managed and instance labels,
revision label, and running state. The profile requires a Docker healthcheck;
healthy, unhealthy, and starting are normalized to bounded Kodi-process states,
while missing or malformed health fails as unknown/error rather than inferring
process life from container state.

The container must have exactly one network attachment, named
`kodi-lab-managed-v1`, and exactly one canonical IPv4 address inside
`10.250.0.0/24`. The returned port is the profile-owned 8080, never caller data.
The minimal response excludes raw network dictionaries, other addresses, host
network state, labels, environment, commands, logs, and raw inspect. The broker
does no Docker exec and no Kodi HTTP/JSON-RPC; application probes remain in the
unprivileged adapter. Stable errors distinguish missing profile/container,
identity/revision conflicts, non-running state, endpoint failure, Docker failure,
and malformed managed metadata.

## Existing-container two-state reconciliation

`start_managed_container` and `stop_managed_container` share one private trusted
adapter flow. Both accept exactly canonical managed-instance UUID, previously
observed immutable 64-lowercase-hex Docker container ID, and normalized expected
runtime state. Neither ID is a discovery key. After peer authorization and strict
schema validation, fixed exact ownership labels select exactly one candidate. A
fresh inspect then proves selected immutable ID, managed label, canonical matching
instance UUID, expected immutable ID, and normalized state.

The operation name fixes the desired state (`running` for start, `stopped` for
stop). If fresh observed state already equals desired state, the broker returns
success with its operation-specific `*_performed=false` regardless of stale
expected state. If mutation is required, expected state must equal fresh observed
state; only stopped-to-running and running-to-stopped transitions exist.
Transitional and unknown observations fail without mutation. Inventory and both
mutations use the same normalization function, so inventory state can be used
directly as a later precondition.

Start supplies Docker only the trusted selected immutable ID and no caller
lifecycle options. Stop additionally supplies the broker-fixed 10-second timeout.
After either mutation returns, a fresh inspect repeats ownership, instance, and
immutable-ID validation and must prove the desired normalized state. There is no
polling/retry, kill fallback, restart, restart-policy control, or Kodi/application
readiness claim.

## Semantic stop mutation

`stop_managed_container` accepts exactly:

```text
managed_instance_id: lowercase canonical UUID
expected_container_id: immutable 64-lowercase-hex Docker container ID
expected_runtime_state: running | stopped | transitional | unknown
```

The expected container ID is only an optimistic precondition; it is never a
discovery key. After peer authorization and strict schema validation, the trusted
adapter queries with both fixed labels `io.kodi-lab.managed=true` and
`io.kodi-lab.instance-id=<requested UUID>`. It requires exactly one match,
inspects the Docker-selected immutable ID, and immediately revalidates the managed
label, canonical matching instance UUID, immutable container ID, and normalized
runtime state from fresh inspect data.

If that exact owned container is already stopped, the result is successful
desired-state convergence with `stop_performed=false`, regardless of the caller's
expected state. Otherwise only current `running` plus expected `running` may
mutate. Transitional and unknown current states fail closed. Docker receives only
the selected immutable ID and broker-fixed 10-second timeout; the protocol has no
signal, force, kill, or timeout field, and the broker performs no kill fallback or
retry through a separate Docker API call.

After Docker stop returns, the adapter freshly inspects the same immutable ID,
revalidates ownership and instance identity again, and requires normalized
`stopped` state. A missing container, changed ownership/identity, read failure, or
non-stopped state is a postcondition failure rather than success. The response
contains only managed-instance UUID, container ID, `runtime_state=stopped`, and
`stop_performed`.

Immutable-ID comparison prevents a newly selected replacement container B from
being stopped for a stale observation of A. An unavoidable Docker-level TOCTOU
interval remains between the final pre-stop inspect and stop call: another
privileged Docker actor can change A's state during that interval. Exact-ID
targeting and post-stop verification bound that risk, but do not provide a lock,
lease, transaction, or guarantee against a later restart. The synchronous client
uses a fixed 35-second response window derived from bounded list/inspect/stop API
calls; other client operations retain five seconds.

## Audit and failure behavior

Every completed or denied connection records bounded structured fields suitable
for journal output: request ID, peer PID/UID/primary GID, safely parsed operation
and protocol version, result/error code, and elapsed duration. Authorization
denials are logged. Successful inventory adds only its returned count. Full
bodies, malformed raw bytes, inspect objects, container labels, immutable
container IDs, and environment data are never logged. Stop audit records add only
the validated managed-instance UUID, whether stop was performed, and resulting
semantic state; stop failures retain the managed-instance UUID and stable code.

Path/identity/permission failures prevent startup. Missing peer credentials deny
the connection. Frame, JSON, version, schema, operation, and parameter failures
prevent dispatch. Unexpected handler failures become a stable `internal_error`
response without traceback or exception detail; an internal diagnostic record is
retained. Raw Docker errors are translated rather than returned.
Inventory failures are translated to `docker_daemon_unavailable`,
`docker_inventory_failed`, `invalid_managed_container_metadata`, or
`inventory_limit_exceeded`; underlying Docker exception text does not cross the
protocol boundary.
Stop failures additionally use `managed_container_not_found`,
`ambiguous_managed_container`, `container_identity_precondition_failed`,
`runtime_state_precondition_failed`, `container_state_not_stoppable`,
`docker_stop_failed`, and `post_stop_verification_failed`. Raw Docker exception
details remain internal.

## Disposable managed-container provisioning

The provisioning request contains only `managed_instance_id`. A read-only trusted
resolver independently loads canonical Target, ManagedInstance, pinned
RuntimeRevision and RuntimeDefinition, and the revision's separate immutable
RuntimeContainerArtifact from SQLite. A missing relationship/artifact or a
persistent instance fails before Docker access. Approval and Trusted Custom Build
selection are historical application concerns and are not re-run or caller
parameters.

The adapter requires the pre-existing bridge network `kodi-lab-managed-v1`,
subnet `10.250.0.0/24`, and its three exact ownership/resource labels. It never
creates or substitutes a network. Docker assigns the address dynamically. The
fixed create request uses name `kodi-lab-managed-<uuidhex>`, immutable image ID,
the managed/instance/runtime-revision labels, the trusted profile's explicit
non-root numeric UID/GID, restart `no`, non-privileged host configuration, and
only the managed network endpoint. Command, entrypoint,
environment, ports, mounts, volumes, binds, devices, added capabilities, host
namespaces, custom security options, and resource limits are absent. Image
defaults are compared during verification; images declaring volumes are rejected.

Existing same-name state is never adopted. Exactly one label-selected container
is idempotent only when every fixed create invariant matches and state is running
or stopped; drift and duplicate ownership fail. A new container must freshly
verify exact ID/name/labels/image, profile-derived user, image defaults, network, no publication or
storage, restart/security profile, and stopped state before success.

On post-create failure, cleanup performs another exact-ID inspect and removes
only when all ownership/revision labels still match. It uses non-forced removal
and never touches a pre-existing container, image, network, or volume. Uncertain
identity/ownership or cleanup failure produces
`partial_provisioning_requires_reconciliation` and leaves the object untouched.
Provisioning failures use stable prerequisite/conflict codes for trusted state,
artifact/image/network availability, ambiguous/drifted containers, create,
post-create verification, and uncertain partial state. The typed client uses a
fixed 45-second response window derived from the bounded Docker calls; neither
raw Docker exception text nor inspect data crosses the protocol or audit event.
Start reuses the common not-found, ambiguity, metadata, identity, state,
daemon, and inventory errors; its additional stable errors are
`container_state_not_reconcilable`, `docker_start_failed`, and
`post_start_verification_failed`. Successful start audit adds only managed-instance
UUID, whether start was performed, and resulting semantic state. No immutable
container ID or Docker response is logged.

## Stopped-container deprovision

`deprovision_managed_container` accepts exactly:

```text
managed_instance_id: lowercase canonical UUID
expected_container_id: immutable 64-lowercase-hex Docker container ID
expected_runtime_revision_id: lowercase canonical UUID
expected_runtime_state: stopped
```

The broker first resolves the canonical ManagedInstance read-only and requires
its persisted revision pin to equal the expected revision. The Docker adapter
then discovers with fixed exact managed and instance labels, requires exactly one
match, and freshly inspects the selected ID. Removal requires exact immutable ID,
deterministic managed name, managed/instance/runtime-revision identity labels,
and normalized stopped state. The caller ID and revision are concurrency
preconditions, not Docker discovery inputs. A missing target is a typed not-found
failure rather than claimed success; duplicate ownership is an integrity failure
and is never narrowed using the caller ID.

Docker receives only the selected immutable ID, `force=false`, and volume removal
disabled. The operation never performs stop, kill, or lifecycle convergence.
After remove returns, exact-ID inspect must report absence and the fixed-label
lookup must find no remaining container for the instance. A replacement or an
unverifiable postcondition therefore fails rather than returning false success.
Only the container is removed: Target, ManagedInstance, runtime catalog/artifact,
image, managed network, and storage remain.

Ownership, instance identity, revision identity, immutable container identity,
deterministic name, and stopped state are mandatory security invariants.
Unrelated non-identity Docker configuration drift is not treated as ownership,
but also does not permanently prevent removal of an otherwise proven owned
stopped container. There remains an unavoidable interval in which another
privileged Docker or database actor could change state; exact-ID mutation and
postconditions reduce but do not eliminate that TOCTOU without a lock or lease.

Deprovision errors add `managed_instance_state_unavailable`,
`runtime_revision_precondition_failed`, `container_must_be_stopped`,
`docker_remove_failed`, and `post_deprovision_verification_failed`, while reusing
not-found, ambiguity, invalid-metadata, immutable-ID, daemon, and inventory codes.
Raw Docker/storage exceptions remain internal. Audit records include only the
managed-instance UUID, runtime-revision UUID, removed boolean, and normal bounded
request/peer/outcome fields; they exclude container IDs, labels, inspect data,
database rows, and exception representations.

## Phase 2 closeout evidence and deferred work

The security boundary has both fake/security-test coverage and controlled live
development host evidence. The live path ran an ephemeral privileged broker with an
authorized unprivileged peer over a non-production Unix socket. The peer was
denied direct Docker-socket access. Through the typed client, the broker
provisioned one disposable stopped container from trusted SQLite state,
inventoried it, started it, observed it running, stopped it, observed idempotent
stopped convergence, and proof-gated its deprovision. Independent Docker checks
confirmed immutable identity, required labels, isolated managed network, and the
absence of ports, mounts, caller command/environment, or elevated privilege.
Post-deprovision checks found no owned validation container, while canonical
database state and the empty managed network remained.

Protected legacy containers, networks, broker process, and socket retained their
identities and state throughout validation. The repository does not create or
remove the managed network; product ownership of that lifecycle is deferred.
The intended production unit name still collides with the protected legacy
`kodi-lab-broker.service`, so production deployment remains blocked pending a
separate naming/migration decision. Neither that deployment concern nor deferred
persistent-instance storage weakens the completed disposable broker boundary.

## Promotion-verification Kodi log authority

The broker adds only `capture_promotion_kodi_log_cursor` and
`read_promotion_kodi_log_window`. Both accept a verification UUID, instance
UUID, and exact immutable container identity; the read also accepts only the
opaque broker-generated cursor. Trusted SQLite state independently proves the
succeeded source promotion, single-use handoff reserved to this verification,
running verification, exact target/runtime/container, and expected add-on ID.
Cursor capture occurs while that handoff is unconsumed. Window reading occurs
only after exact sync has atomically consumed it and published fresh ordinary
deployment evidence.

The broker resolves only `/home/kodi/.kodi/temp/kodi.log` and
`/home/kodi/.kodi/temp/kodi.old.log`. There is no path, glob, command, shell,
offset, limit, or generic archive/read input. Archive bytes, file identities,
cursor internals, container IDs, and full logs are absent from public DTOs and
ordinary audit history. Reads are bounded, regular-file-only, correlate append
or normal rotation, and return incomplete on truncation, recreation ambiguity,
oversize data, or broker restart cursor loss.

## Uploaded-ZIP materialization authority

The Phase 8 ZIP broker operations accept only canonical instance, artifact and
server-generated installation-operation UUIDs plus the exact expected container
identity. Trusted SQLite state resolves the immutable accepted artifact,
normalized payload, add-on ID, destination, runtime revision, suppression
profile and runtime UID/GID. A caller cannot represent a path, archive bytes,
destination, mode, ownership, extraction flag, URL, command, Docker option, or
Kodi install request.

Observation classifies only the fixed managed add-on destination as absent,
exact normalized payload, or partial/unexpected. Application additionally
requires the durable application marker, exact stopped disposable container,
suppression environment and absent destination. Docker receives a private
server-built normalized tar stream and the fixed `/home/kodi` root. Complete
post-write readback must match every expected type, mode, digest and profile
UID/GID with no unexpected member. ZIP metadata never supplies runtime
ownership or modes, and neither operation invokes Kodi's native installer.

## Phase 10 managed screenshot operation

`capture_managed_kodi_screenshot` is the sole broker visual operation through
the completed Phase 10 boundary.
Its strict request contains only canonical ManagedInstance UUID, expected
64-hex managed container identity, expected RuntimeRevision UUID, and
server-generated capture UUID. Trusted local persistence independently resolves
the UUID-derived staging location and one of the four exact current curated
RuntimeRevision/profile identities for Kodi 19.5, 20.5, 21.3, or 22 Beta 2.
Version strings or nearby RuntimeRevisions cannot establish capability.

The pinned image does not contain an `xwd` executable. The Docker adapter
therefore executes only one literal `/usr/bin/python3 -I -c <fixed-program>`
argument vector. That broker-owned program performs X11 `GetImage` against the
fixed Xvfb `:99` root and reads the server setup's root depth, pixmap format,
image byte order, scanline padding, visual class, RGB precision and channel
masks. Only the closed 1280x720 TrueColor depth-24, 32-bpp, 32-bit-padded RGB888
layout is admitted. A versioned private envelope carries those observed fields
and raw pixels to the unprivileged decoder; unfamiliar formats fail rather than
being guessed. Request schemas reject command, executable, argv, path, display,
format, converter, timeout, Docker object, network address, and port fields.

The selected container must have the exact Kodi Lab ownership and revision
labels and be running and healthy both before and after capture. The raw frame
is capped, private, mode 0600, removed on broker failure, and never transported
in framed JSON. Broker results and audit fields contain only semantic UUIDs, the
`x11_get_image` classification, and bounded byte count; they contain no artifact
path, X display, command, container internals, visual metadata, or raw error.

The exact Kodi 21.3 view-only live display reuses this operation unchanged. It
adds no broker method, VNC/noVNC endpoint, browser/container connection, input
channel, caller-selected path/display/command/format/rate/timeout, or generic
execution authority. The unprivileged application consumes each private raw
artifact through the same strict decoder and validator, removes staging, and
keeps the resulting display PNG in memory only.

## Phase 11 Slice 1 MCP boundary

The read-only MCP foundation adds no broker method and no new broker caller
authority. MCP adapters never import the broker client, Docker adapter, X11,
raw Kodi transport, or filesystem paths. `target_list` and `target_status`
reuse the unprivileged transport-neutral `ManagedRuntimeStatusService`; that
service retains the existing restricted broker boundary for its bounded
managed-container observation.

MCP client identity, bearer verification, presets, exact target grants, target
policy, static tool metadata, and request admission all live in the ordinary
application process. They cannot authorize a broker operation directly.
Future MCP tools must first use an independently approved Kodi Lab application
service; adding registry metadata alone never creates broker, Docker, shell,
path, or raw JSON-RPC authority.

Phase 11 Slice 2's `kodi-lab-mcp-stdio` process does not change this boundary.
It forwards bounded modern MCP requests to authenticated loopback HTTP and does
not import or connect to the broker, Docker, Kodi, persistence, repositories,
or application services. Possession of the executable without a bearer grants
nothing. Possession of a bearer grants only that durable client's authority as
recomputed by `/mcp` on each request.

Phase 11 Slice 3 adds no broker operation. `addon_list` reaches Kodi only through
the existing `ManagedInstanceAddonInventoryService` and its fixed read-only
inventory adapter. `screenshot_capture` reaches the broker only through the
existing `ManagedScreenshotService` and exact
`capture_managed_kodi_screenshot` primitive described above. MCP cannot select
an X11 display, backend, command, path, format, quality, dimensions, timeout or
cache location. Registry capability metadata and `target.visual` permission do
not directly authorize a broker call.

The exact validated PNG returned to MCP is the successful durable capture's
in-memory result. The service still owns private raw staging, atomic latest-cache
publication, failed-refresh retention, per-instance exclusion and the global
four-capture bound. Neither direct HTTP nor stdio gains a broker client or
filesystem path. Image bytes/base64, hashes, cache names, paths and broker
identifiers are excluded from MCP request audit logs.

Phase 11 Slice 4 adds no broker operation or broker caller authority.
`remote_action` first passes the ordinary MCP authority intersection and a
durable application-owned replay receipt, then calls the existing
`BrowserRemoteService`. That service continues to obtain only the fixed bounded
managed-runtime observation and constructs the closed Phase 10 Kodi requests in
the unprivileged process. MCP cannot select a broker method, Docker object,
endpoint, JSON-RPC method/parameters, player ID, path, command, or retry.

The receipt is not broker protocol state and contains no broker request ID,
container/network identity, endpoint, raw Kodi document, or screenshot bytes.
It adds application-level replay protection across HTTP and stdio while the
underlying remote adapter retains its single-attempt transport semantics.

Phase 11 Slice 5 adds no broker operation or broker caller authority. Custom
preset creation copies only ordinary application-database permission rows; it
cannot name a broker method, path, runtime, container, endpoint, command, or
Kodi request. Exact legacy `2025-11-25` is only an SDK wire adaptation in the
same unprivileged HTTP process. It enters the identical authentication,
admission, registry, authorization, service, and receipt boundaries as modern
traffic and never creates a broker session or cached authority. The broker
cannot observe or trust protocol revision, `Mcp-Session-Id`, client metadata, or
caller-declared transport classification.

## Phase 12 Slice 1 development inspection boundary

Slice 1 adds no broker operation and no broker caller authority. Development
source discovery reads only path-free persisted registration observations.
Workspace status/tree/read pass through the unprivileged Phase 9
`AgentWorkspaceService`; MCP cannot name a data root, source locator, host path,
broker method, Docker object, container, command, or shell.

The Phase 9 service alone derives the UUID workspace root and performs
descriptor-relative no-follow traversal. Only byte-present ready workspace
trees and single-link regular UTF-8 files are readable. `O_NONBLOCK` prevents an
injected FIFO from blocking file open, and descriptor identity is rechecked
after open. No workspace bytes cross the broker boundary.

## Phase 12 Slice 2 workspace mutation boundary

Slice 2 adds no broker operation, broker caller authority, Docker access, Kodi
operation, shell, Git, subprocess, or generic filesystem surface. Replay-safe
creation calls the unprivileged Phase 9 `AgentWorkspaceService.queue_creation`;
the MCP layer contributes only ordinary SQLite mapping, quota, and exact owner
binding state. Source capture and private workspace layout remain wholly inside
the existing Phase 9 service.

`workspace_edit` never derives or opens a root. It passes workspace UUID,
expected generation, normalized semantic relative path, and bounded UTF-8 text
to the existing Phase 9 create/replace/exact-patch methods. Descriptor-relative
no-follow traversal, full-tree integrity capture, atomic publication, rollback,
and reconciliation remain in that unprivileged service. MCP cannot select a
broker method, source/storage/destination path, container, command, executable,
environment, ownership, mode, or Docker/Kodi parameter.

## Phase 12 Slice 3 validation and retention boundary

Slice 3 adds no broker operation, caller authority, Docker/Kodi access,
subprocess, shell, Git or arbitrary test surface. `workspace_validate` passes
only workspace UUID and exact generation to the unprivileged Phase 9 service.
That service alone derives storage, takes workspace coordination, recaptures
bounded trees and parses captured `addon.xml`; the MCP adapter never walks or
opens workspace files and accepts no command, module, selector, script,
environment, package manager or caller timeout.

Retention is application policy and exposes no MCP tool. The bounded sweep
performs metadata decisions and queues only the existing typed Phase 9 cleanup
service, whose UUID-derived no-follow identity/deletion protections remain
authoritative. It adds no direct directory removal and no broker involvement.

## Phase 12 Slice 4 workspace deployment boundary

Slice 4 adds exactly two fixed broker operations for the new application
service: `copy_sync_mcp_workspace` and
`verify_mcp_workspace_destination`. MCP never calls the broker and cannot name
these operations. The unprivileged application passes only server-persisted
managed-instance, workspace, exact generation and deployment-operation UUIDs
plus the already observed exact container precondition.

The privileged resolver re-derives workspace storage, source identity, attached
destination, runtime UID/GID, current container anchoring and prior copy
evidence from trusted database/configuration state. It requires a running
deployment row in the exact sync phase, ready present workspace bytes, stable
add-on identity and generation, matching internal disposable target/source and
the current runtime revision. The Docker adapter then reuses the existing
stopped-only overwrite/add copy engine and manifest readback. Caller-supplied
paths, destinations, ownership, modes, delete flags, commands, package formats,
container identities, lifecycle options and arbitrary Docker operations remain
unrepresentable.

The verification operation accepts only deployment-operation, target and exact
observed-container preconditions and returns bounded identity/version/match and
video-plugin eligibility. It returns no path, digest, member list, raw XML or
container identifier beyond the pre-existing ephemeral broker result needed by
the application boundary; the durable Slice 4 evidence deliberately does not
persist container IDs. Execution itself remains unprivileged fixed Kodi
JSON-RPC and adds no broker operation.

## Phase 12 Slice 5 promotion-request boundary

Slice 5 adds no broker operation or broker caller authority. MCP supplies only
semantic request/workspace/generation/target UUIDs to the unprivileged
application. That application validates persisted Slice 4 evidence and calls
the existing Phase 9 promotion-plan preparation service; plan review performs
no source mutation.

Only a confirmed Web administrator approval may enter the existing Phase 9
source-apply and verification choreography. The already restricted broker
semantics still derive canonical DevelopmentSource, backup, target and
container details from trusted state. MCP cannot select or invoke source apply,
rollback, copy, reload, log capture, broker methods, paths, containers,
commands, Docker operations, Git or repository publication. The mandatory
durable verification child and existing source-success boundary are unchanged.

## Phase 13 Slice 1 has no broker authority

External-development registration and native read-only JSON-RPC verification
are unprivileged application/Web functions. Slice 1 adds no broker operation,
caller, path, Docker access, lifecycle action, bridge installation, deployment,
execution or production authority. The verifier receives only the persisted
structured endpoint and a credential recovered by the application secret
facility; it cannot invoke the broker.

## Phase 13 Slice 2 has no new broker authority

External target status, add-on inventory and semantic remote control remain
unprivileged direct native JSON-RPC operations. The application resolves the
persisted target and protected credential, validates the fixed endpoint, and
selects a closed semantic adapter only after the complete MCP authority
intersection. It cannot call the broker or select a broker method, container,
path, command, lifecycle action, bridge, deployment or execution operation.
Existing managed-target tools continue through their existing services and
broker boundaries unchanged.

## Phase 13 Slice 3 has no new broker authority

Bridge pairing, authenticated bridge identity/capability probes and external
screenshot capture are unprivileged application-to-external-target operations.
They cannot call the broker. The server derives the fixed bridge endpoint from
persisted target state and the configured bridge-port allowlist; MCP cannot
provide a URL, path, port, secret, signature or operation.

The narrow Kodi add-on captures the GUI through Kodi's fixed
`TakeScreenshot(<bridge-owned-path>,sync)` built-in, immediately reads and
deletes one unpredictable file below its own `special://temp` subdirectory,
and returns bounded normalized PNG bytes in the authenticated response. No
caller supplies a path and the bridge exposes no file operation. It has no
host path, container, network, Docker, lifecycle, deployment, execution,
shell, log, generic filesystem, profile or arbitrary Kodi-method authority.
Existing managed
`screenshot_capture` continues to use only
`capture_managed_kodi_screenshot`; its broker request and protected-runtime
isolation are unchanged. Migration `0045_external_bridge_pairing` creates no
broker action, allowlist entry or privileged caller.

## Phase 13 Slice 4 has no new broker authority

External workspace deployment is performed by the separately authenticated
Kodi bridge and fixed execution by the existing direct native JSON-RPC target
adapter. Neither path can call the privileged broker. Migration
`0046_external_bridge_development` adds no broker action, socket message,
allowlist, container identity, filesystem root or host lifecycle operation.

The bridge's local add-on-directory transaction is not broker authority: it is
limited to the manifest add-on ID under Kodi's own add-on root and receives no
host path. It cannot stop/restart Kodi, manipulate a container/profile, run a
command or reach a DevelopmentSource. Managed Phase 12 deployment retains its
existing broker constraints unchanged. Production roots and protected runtime
fingerprints remain outside both external adapters.

Installed-tree provenance parsing may omit a real, non-recursive
`__pycache__` directory only when every direct child is a bounded single-link
regular `.pyc` file. This canonical exclusion is not broker or filesystem
authority: the bridge receives no path, does not read or delete the derived
bytes, and rejects links, nested directories, unknown names and over-limit
caches.

Phase 13 formal-review remediation D-0302 changes no broker boundary. Bridge
1.1.7 protects live screenshot replay identity under bounded capacity, enforces
the native adapter's aggregate deadline and rejects screenshot hardlinks. It
adds no broker operation, caller, path, runtime, container, Docker, shell,
filesystem or lifecycle authority.

## Phase 14 Slice 1 has no broker authority

The production foundation is database, application and authenticated-Web policy
only. It reuses the existing direct external native verifier and protected
secret store and adds no broker request, allowlist entry, fixed path, privileged
caller, runtime/container operation or lifecycle action. The centralized MCP
veto prevents production adapter dispatch. Global and per-target mutation
switches are deny-only policy state and cannot invoke the broker.

No production target was contacted. Production data/runtime roots and protected
managed resources remain outside the external-production representation.
