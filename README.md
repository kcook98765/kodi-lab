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

## MCP stdio

Phase 11's primary protocol is modern `2026-07-28`. Its canonical stdio command
is a thin modern-only proxy to the running loopback MCP HTTP authority:

```console
KODI_LAB_MCP_TOKEN='<token shown once by Web administration>' kodi-lab-mcp-stdio
```

Generic stdio-capable clients should configure command
`kodi-lab-mcp-stdio`, set `KODI_LAB_MCP_TOKEN` in the child environment, and
place no token in argv. The default authority is
`http://127.0.0.1:8100/mcp`. Development/tests may set `KODI_LAB_MCP_URL`, but
only an exact plain-HTTP loopback `/mcp` URL is accepted. Every target-scoped
tool still requires an explicit `target_id`; stdio creates no selected target or
session. See [`docs/PHASE11-MCP.md`](docs/PHASE11-MCP.md) for security and
client configuration details.

The current production catalog is exactly `addon_list`,
`development_source_list`, `promotion_request`, `remote_action`, `screenshot_capture`, `target_list`,
`target_status`, `workspace_create`, `workspace_deploy`, `workspace_edit`,
`workspace_execute`, `workspace_read`, `workspace_status`, `workspace_tree`, and
`workspace_validate`.
Screenshots are returned as native MCP `image/png` content through either
transport; stdio does not write a temporary image or bypass the HTTP authority.

Phase 12 workspace mutation requires `workspace.edit`, available only in the
Add-on Development and Full Lab Control built-ins. Creation is request-ID
replay-safe and limited to exact Web-granted registered sources, four present
bound workspaces/client, 32 globally, and one creating/client. Editing is
generation-bound UTF-8 create/replace/exact-patch over existing Phase 9 paths.
`workspace_validate` performs only exact-generation closed structural checks;
it accepts no command, test selector, script, environment, or timeout. MCP-owned
terminal bytes retain seven days, while ready workspaces of disabled clients
quarantine for 30 days before safe abandonment and the same seven-day grace.
Active-client ready, currently deployed and reconciliation-required workspaces
never age-delete. Exact-generation deployment requires `workspace.deploy`,
same-generation successful validation, exact ownership/source/target grants and
an internal disposable target. `development.execute` invokes only the fixed
copied Python video-plugin smoke shape from exact deployment evidence. Both are
request-ID replay-safe across HTTP/stdio and expose no paths, lifecycle options,
add-on selection or raw JSON-RPC. `promotion_request` requires
`development.request_promotion` plus exact successful validation, deployment
and fixed execution evidence. It prepares the existing Phase 9 plan for an
authenticated Web administrator to review, approve or reject; MCP cannot
approve/apply and receives no diffs, paths or logs. MCP has no abandon,
cleanup, general execution, shell, Git, repository publication, Docker, or
caller-selected storage path.

Exact legacy `2025-11-25` clients may use authenticated Streamable HTTP at
`/mcp`. Compatibility is stateless and bounded: there is no session/default
target, `Mcp-Session-Id`, GET stream, alternate credential, legacy stdio, or
fallback from malformed modern traffic. Every target-scoped call still supplies
an explicit `target_id`, and both protocol revisions use the same current
credential, preset, grant, target-policy, registry, capability/state, rate,
replay, and privacy decisions.

Web MCP administration can create a custom preset by cloning one active
immutable built-in. The clone receives a new UUID and a creation-time permission
snapshot. V1 does not provide per-permission editing, custom rename, or custom
deletion, and future built-in permissions do not silently broaden an existing
clone. **Phase 11 — MCP core is COMPLETE.** Its independently reviewed
implementation checkpoint is `3faeeb808ca1a86ab5b1a8a0038827fe9b76b57a`,
with Alembic head `0038_mcp_remote_control` and 1,928 collected tests.

**Phase 12 — MCP development workflows is COMPLETE.** Independent review
accepted implementation checkpoint `dc6f7857b222aff31467222e8d9eff8b96df6328`,
Alembic head `0043_mcp_promotion_request`, and 2,020 collected tests. Web
administrators grant exact DevelopmentSources and bind eligible existing
AgentWorkspaces; MCP creation binds one durable owner atomically. The exact
15-tool catalog supports bounded inspect/create/edit/validate, internal
disposable deployment, fixed execution, and promotion-plan request. D-0277
through D-0279 make shared replay retention lifecycle-aware, evidence-coherent,
and authoritative to the bounded promotion journal. Web alone reviews and
approves canonical source apply and mandatory verification. MCP still has no
shell, Git/repository publication, arbitrary execution, external target, or
production mutation authority. See
[`docs/PHASE12-MCP-DEVELOPMENT.md`](docs/PHASE12-MCP-DEVELOPMENT.md).

Phase 13 Slices 1-4 add Web-only external-development registration, fixed
native verification/tools, and an authenticated screenshot/deployment bridge;
Phase 13 remains PARTIAL. Password and bridge secrets require an operator-provided
`KODI_LAB_EXTERNAL_TARGET_MASTER_KEY` containing a base64url-encoded 32-byte
key. The key is never generated into the repository or stored in SQLite.
Structured native endpoints default to allowlisted port `8080`; the separate
bridge allowlist defaults to `8765`. Loopback is available only through the
explicit test setting. The MCP catalog remains the same fifteen tools:
eligible paired external targets use the existing `screenshot_capture`, while
exact bridge-backed `workspace_deploy`, native fixed `workspace_execute` and
promotion evidence use their existing schemas. See
[`docs/PHASE13-EXTERNAL-TARGETS.md`](docs/PHASE13-EXTERNAL-TARGETS.md).

For a development bridge, install
`bridge/service.kodi_lab_bridge` as an ordinary Kodi service add-on. Keep it
disabled until the external target is registered, native-verified, explicitly
confirmed/enabled and configured with the required capability maximum. In Web, create
the show-once pairing token; enter it locally in the add-on settings, set the
allowlisted bridge port, enable the add-on, then use Web to run the possession
proof and confirm the displayed bridge identity. Re-pairing rotates the secret
and revocation disables screenshot/deployment authority immediately. The add-on
exposes only identity, capability, screenshot and pathless content-addressed
deployment operations; fixed execution remains native. Establish the exact
DevelopmentSource baseline in Web before deployment and use only Web receipt
reconciliation after ambiguity. With HTTP,
HMAC protects integrity but PNG bytes are cleartext to observers on that LAN;
use normally validated HTTPS when confidentiality is required.
