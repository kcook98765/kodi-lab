# Contributing

Read `docs/V1-SPEC.md`, `docs/PROJECT-HANDOFF.md`, and the relevant decision
records before changing architecture or host integration. Preserve the legacy
lab boundary in `docs/LEGACY-LAB-BOUNDARY.md`.

Install the locked development environment and run the full foundation gate:

```console
uv sync --dev
./scripts/check
```

The check is provider-neutral, uses the locked uv environment, stops at the
first failure, and writes build artifacts only to a temporary directory.

For normal iteration, `./scripts/check-fast` runs pytest without the narrowly
marked expensive tests, plus formatting, lint, strict typing, and the cheap
Alembic-head check. It is a convenience lane only. `./scripts/check` remains the
complete authoritative milestone and pre-commit gate and does not skip slow
tests.

Keep changes within the currently authorized implementation phase. Record new
architectural choices in `docs/DECISIONS.md`; do not silently amend the v1
specification.
