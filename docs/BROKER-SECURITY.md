# Restricted broker security contract

This document derives from `V1-SPEC.md` plus approved decisions D-0018 through
D-0037. It describes the Phase 2 implementation boundary; it authorizes one
read-only Docker inventory operation but no Docker or Kodi lifecycle behavior.

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

All require the authorized UID. There is no anonymous operation and no generic
execution, raw Docker, shell, raw payload, file, path, environment, caller-driven
inspection, or lifecycle operation.

The unprivileged `BrokerClient` exposes explicit methods with those four names.
Its shared call implementation is private. It generates canonical UUIDs,
validates outgoing envelopes, uses the bounded framing/timeouts, requires response
version and correlation-ID consistency, validates operation-specific results, and
translates transport, authorization, remote, and malformed-response failures into
typed client exceptions.

## Read-only Docker inventory boundary

`managed_container_inventory` is the only Docker-backed operation. The trusted
adapter offers one semantic method and constructs the Docker API client with the
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
silently truncated. Docker `running` maps to `running`, `exited`/`dead` to
`stopped`, known intermediate states to `transitional`, and other strings to
`unknown`. Health is `healthy`, `unhealthy`, `starting`, `not_configured`, or
`unknown`.

Environment, command/entrypoint, mounts and host paths, raw labels, networks and
addresses, ports, logs, daemon information, capabilities/security options, raw
inspect responses, mutable image tags, and arbitrary annotations are excluded.
The adapter has no create, start, stop, restart, remove, exec, image, volume,
network, label-mutation, or generic passthrough method. Observations are not
persisted in SQLite.

## Audit and failure behavior

Every completed or denied connection records bounded structured fields suitable
for journal output: request ID, peer PID/UID/primary GID, safely parsed operation
and protocol version, result/error code, and elapsed duration. Authorization
denials are logged. Successful inventory adds only its returned count. Full
bodies, malformed raw bytes, inspect objects, container labels, and environment
data are never logged.

Path/identity/permission failures prevent startup. Missing peer credentials deny
the connection. Frame, JSON, version, schema, operation, and parameter failures
prevent dispatch. Unexpected handler failures become a stable `internal_error`
response without traceback or exception detail; an internal diagnostic record is
retained. Raw Docker errors are translated rather than returned.
Inventory failures are translated to `docker_daemon_unavailable`,
`docker_inventory_failed`, `invalid_managed_container_metadata`, or
`inventory_limit_exceeded`; underlying Docker exception text does not cross the
protocol boundary.
