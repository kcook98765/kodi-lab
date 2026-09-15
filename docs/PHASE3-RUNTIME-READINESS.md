# Phase 3 Kodi 21.3 runtime and readiness contract

This document records the approved executable supplement for the first Phase 3
runtime. `V1-SPEC.md` remains authoritative; this contract is deliberately
limited to one disposable Kodi 21.3 Omega stable instance.

## Immutable runtime build

`runtime/kodi-21.3/manifest.json` records the exact Ubuntu 26.04 base digest,
official Ubuntu package sources, Kodi package version
`2:21.3+dfsg-1ubuntu1`, repository input hashes, build parameters and timestamp,
resulting image ID, and build-log digest. The pinned inputs and immutable output
are reproducible evidence, but changing upstream repository metadata means the
project does not promise byte-identical rebuilds from the package repository.

The image runs as non-root user `kodi`. Its fixed entrypoint starts internal
Xvfb on `:99`, waits for that display, and launches Kodi under a D-Bus session
with software rendering. It propagates termination and exits when Kodi exits.
The seeded profile is disposable container-layer data and enables JSON-RPC HTTP
on internal port 8080. There are no declared volumes, host binds, published or
exposed ports, VNC/noVNC services, host display, GPU/device access, added
capabilities, or protected legacy inputs.

The fixed image healthcheck runs only `pgrep -x kodi.bin`. Healthy maps to Kodi
process `running`, unhealthy to `not_running`, starting to `starting`, and
missing/malformed health to `unknown_error`. It is not a JSON-RPC or usability
probe.

## Trusted execution profile

One immutable execution profile is registered per runtime revision. For this
runtime it fixes:

- runtime kind: `kodi`
- application identity: `Kodi`, version `21.3`, tag `stable`
- internal endpoint: port `8080`, path `/jsonrpc`
- process observation: `docker_healthcheck`
- readiness probe: `kodi_jsonrpc`
- total readiness deadline: 90 seconds
- polling cadence: one second
- individual HTTP timeout: three seconds

The profile is create-only and has no command, environment, mount, host port,
network, arbitrary Docker option, or arbitrary JSON-RPC field. A material change
requires a new runtime revision/profile.

## Observation and readiness

The privileged broker accepts only managed-instance UUID and an immutable
container-ID precondition. Trusted SQLite state supplies the pinned revision,
profile, fixed network, and port. Fresh Docker inspect must prove exact Kodi Lab
ownership, revision, running state, health data, sole attachment to
`kodi-lab-managed-v1`, and one IPv4 address in `10.250.0.0/24`. Only normalized
process state and the minimal managed endpoint DTO cross the broker boundary.
The broker does not execute commands or make HTTP requests.

The unprivileged adapter performs exactly `JSONRPC.Ping` and
`Application.GetProperties` for `name` and `version`. It validates HTTP success,
response size, JSON-RPC version, request ID, result shape, application name, and
exact version/tag. It has no generic method call and cannot substitute an
endpoint outside the broker-produced managed-network DTO.

The readiness layers are independent:

1. existing normalized Docker container state;
2. Kodi process state from the required image healthcheck;
3. JSON-RPC unavailable, responsive, or protocol-error state;
4. usability only after valid ping and exact Kodi 21.3 stable identity.

Retryable absence and connectivity failures poll within one total 90-second
monotonic deadline. Invalid JSON-RPC and unexpected identity/version fail
immediately. Final reasons distinguish container absence, process failure,
JSON-RPC unreachability or invalidity, identity/version mismatch, and total
timeout while preserving the most specific last observed failure.

Clean shutdown uses the existing broker stop operation and its fixed 10-second
Docker timeout. No kill or restart fallback is added.
