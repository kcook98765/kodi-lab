# Contributing

Read `docs/V1-SPEC.md` and the relevant decision records before changing
architecture or host integration. Preserve the legacy lab boundary in
`docs/LEGACY-LAB-BOUNDARY.md`.

Install the locked development environment and run the full foundation gate:

```console
uv sync --dev
./scripts/check
```

Run development commands as the repository owner. Do not use `sudo` for `uv`,
Python, pytest, or any check script; the check lanes refuse UID 0 so routine work
cannot leave root-owned source, cache, virtual-environment, or Git artifacts.
Separately scoped live host-validation scripts may require the privileged broker
boundary, but they are not development check lanes.

The check is provider-neutral, uses the locked uv environment, stops at the
first failure, and writes build artifacts only to a temporary directory.

The test workflow has three tiers:

- `./scripts/check-smoke` is the inner development loop. It runs formatting,
  lint, strict typing, and an explicit representative pytest core without the
  migration lifecycle matrix, Docker/container suites, or long integration
  scenarios.
- `./scripts/check-fast` is the broader sub-slice gate. It runs pytest excluding
  only tests marked `slow`, the same static checks, and the cheap Alembic-head
  check.
- `./scripts/check` is the complete authoritative milestone, pre-commit, and
  final gate. It runs every test plus package and migration validation.

During a feature task, run directly affected focused tests while editing,
`./scripts/check-smoke` after meaningful changes, `./scripts/check-fast` before
live validation or after a coherent implementation slice, and the full
`./scripts/check` once before the final commit and report. Passing smoke or fast
does not substitute for the full gate when a milestone requires it.

Keep changes within the currently authorized implementation phase. Record new
architectural choices in `docs/DECISIONS.md`; do not silently amend the v1
specification.
