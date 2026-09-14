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

Keep changes within the currently authorized implementation phase. Record new
architectural choices in `docs/DECISIONS.md`; do not silently amend the v1
specification.
