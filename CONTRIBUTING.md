# Contributing

Read `docs/V1-SPEC.md`, `docs/PROJECT-HANDOFF.md`, and the relevant decision
records before changing architecture or host integration. Preserve the legacy
lab boundary in `docs/LEGACY-LAB-BOUNDARY.md`.

Install the locked development environment and run the full foundation gate:

```console
uv sync --dev
uv run pytest
uv run ruff format --check .
uv run ruff check .
uv run mypy src tests migrations
uv run python -m build
uv run alembic history
```

Keep changes within the currently authorized implementation phase. Record new
architectural choices in `docs/DECISIONS.md`; do not silently amend the v1
specification.
