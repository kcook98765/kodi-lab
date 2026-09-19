# Kodi Lab

Kodi Lab is a self-hosted environment for developing and testing isolated Kodi
installations. The repository currently includes the Phase 1 domain/storage
core, the completed Phase 2 restricted-broker foundation, the first Phase 3
single-runtime slice, and the initial authenticated Phase 4 localhost Web UI.
The broker exposes
only typed health/capability calls and bounded semantic inventory, provision,
start, stop, and deprovision operations for exactly owned disposable containers.
It does not expose raw Docker access. The first Kodi 21.3 runtime uses a fixed
process-health observation and unprivileged, bounded JSON-RPC readiness path.
The Web slice uses a locally bootstrapped administrator, opaque server-side
sessions, CSRF/origin protection, semantic lifecycle actions, and ephemeral
readiness polling. Additional runtimes and broader product surfaces remain later
work.

Phase 8 also includes a narrow uploaded-ZIP first slice. An authenticated upload
is validated into an immutable private artifact, then an asynchronous managed
operation may install one absent Python video plugin on a healthy disposable
suppression runtime. This is Lab-controlled validated filesystem
materialization—not Kodi's native Install-from-ZIP installer. It performs no
replacement, upgrade, enablement, execution, dependency/repository action, bind
mount, or caller-directed filesystem/JSON-RPC operation.

[`docs/V1-SPEC.md`](docs/V1-SPEC.md) is the authoritative v1 product and
architecture specification. [`docs/PROJECT-HANDOFF.md`](docs/PROJECT-HANDOFF.md)
records the development host host baseline and coexistence constraints for development.

## Development

The supported development host baseline is Python 3.12 on Ubuntu 24.04.

```console
uv sync --dev
./scripts/check-smoke
./scripts/check-fast
./scripts/check
```

`./scripts/check-smoke` is the inner-loop lane: static checks plus a deliberately
selected representative pytest core. `./scripts/check-fast` is the broader
sub-slice gate and skips only tests marked `slow`. `./scripts/check` remains the
complete authoritative milestone, pre-commit, and final gate. Passing smoke or
fast does not substitute for the full gate when a milestone requires it.
Migration fixture design and performance policy are documented in
[docs/TEST-PERFORMANCE.md](docs/TEST-PERFORMANCE.md).

For optional local path overrides, copy `.env.example` to `.env` and edit the
two existing bootstrap paths. A `.env` file is ignored by Git and is not
required in production; the same variables may be supplied by the environment.

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the complete local foundation
check and [`docs/IMPLEMENTATION-PLAN.md`](docs/IMPLEMENTATION-PLAN.md) for the
phased scope.
