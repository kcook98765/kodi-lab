# Kodi Lab

Kodi Lab is a self-hosted environment for developing and testing isolated Kodi
installations. The repository currently includes the Phase 1 domain/storage
core and the completed Phase 2 restricted-broker foundation. The broker exposes
only typed health/capability calls and bounded semantic inventory, provision,
start, stop, and deprovision operations for exactly owned disposable containers.
It does not expose raw Docker access, and Kodi runtime/readiness behavior remains
for later phases.

[`docs/V1-SPEC.md`](docs/V1-SPEC.md) is the authoritative v1 product and
architecture specification. [`docs/PROJECT-HANDOFF.md`](docs/PROJECT-HANDOFF.md)
records the development host host baseline and coexistence constraints for development.

## Development

The supported development host baseline is Python 3.12 on Ubuntu 24.04.

```console
uv sync --dev
./scripts/check
```

For optional local path overrides, copy `.env.example` to `.env` and edit the
two existing bootstrap paths. A `.env` file is ignored by Git and is not
required in production; the same variables may be supplied by the environment.

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the complete local foundation
check and [`docs/IMPLEMENTATION-PLAN.md`](docs/IMPLEMENTATION-PLAN.md) for the
phased scope.
