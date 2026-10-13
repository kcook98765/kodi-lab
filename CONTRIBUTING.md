# Contributing

Read `docs/V1-SPEC.md` and the relevant decision records before changing
architecture or host integration. Preserve the legacy lab boundary in
`docs/LEGACY-LAB-BOUNDARY.md`.

Install the locked development environment:

```console
uv sync --dev
```

For optional source-development path overrides, copy `.env.example` to `.env`
in the checkout and edit its two bootstrap paths. The file is ignored by Git;
the same variables may be supplied through the environment. Bundle installations
generate their separate `.env` with `linux-v1-bundle configure`; do not copy the
source-development template into a deployment bundle.

Run development commands as the repository owner. Do not use `sudo` for `uv`,
Python, pytest, or any check script; the check lanes refuse UID 0 so routine work
cannot leave root-owned source, cache, virtual-environment, or Git artifacts.
Separately scoped live host-validation scripts may require the privileged broker
boundary, but they are not development check lanes.

Choose the narrowest validation level that covers the change:

| Level | When | Command |
| --- | --- | --- |
| **FOCUSED** (default, target under 5 minutes) | Ordinary implementation | `./scripts/check-focused tests/runtime/test_manifest.py --source src/kodi_lab/runtime/manifest.py` |
| **INTEGRATION** (target under 15 minutes) | Real interaction across subsystems | `./scripts/check-integration --focused tests/runtime/test_manifest.py --integration tests/runtime/test_distribution.py --source src/kodi_lab/runtime/manifest.py` |
| **RELEASE** | Release candidates, publication-sensitive or broad shared changes, security boundaries, persistence/migrations needing broad confidence, or explicit request | `./scripts/check` |

The selected test paths can be files or directories under `tests/`. The focused
command runs each path in a separate pytest invocation, then Ruff format/lint
and mypy on those paths and any `--source` Python paths. Run **every new test
file and every materially changed test file individually**, even when a broader
selection also includes them. Add directly affected subsystem tests. The
integration command includes that focused pass and runs its additional selected
interaction tests together. Neither command starts a live Kodi or Docker
acceptance environment.

Use one representative live Kodi only when actual Kodi interaction changed:
Omega 21.3 by default, or the affected Matrix/Nexus generation when behavior is
generation-specific. Multiple generations are for cross-generation behavior.
Simultaneous Kodi is reserved for multi-instance behavior, managed-network or
cross-generation state isolation, broker concurrency, and explicit release
qualification; the accepted Matrix/Nexus/Omega simultaneous result needs no
routine reconfirmation. Otherwise run sequentially. If acquisition did not
change, an exact known-good validated runtime may be reused. Cold acquisition is for
recipe, package trust, assembly, worker, attestation generation, clean-host
acquisition, or explicit release qualification changes.

`./scripts/check` remains the complete authoritative release gate: every test,
Ruff, strict mypy, package build, Alembic history, and package import. It is
**not automatically required after every Codex task**. When RELEASE coverage is
warranted, run it once on final bytes, without competing live Kodi, DinD,
runtime acquisition, or other resource-heavy acceptance. Clean-host/DinD and
runtime acquisition acceptance are separate release qualification procedures,
not automatic companions to the full gate. Report the achieved level as
FOCUSED PASS, INTEGRATION PASS, or RELEASE PASS.

The older `check-smoke` and `check-fast` commands remain available for explicit
use. `check-fast` excludes only the 99 tests marked `slow`, so it is much closer
to a full suite run than a focused lane. Do not use either as a required step
before every commit.

Keep changes within the currently authorized implementation phase. Record new
architectural choices in `docs/DECISIONS.md`; do not silently amend the v1
specification.
