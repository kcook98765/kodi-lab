# Kodi Lab

Kodi Lab is a self-hosted Web application for developing, testing, and
inspecting Kodi installations. Remote-target mode connects to an existing Kodi;
managed-lab mode runs four curated Kodi versions (19.5, 20.5, 21.3, and 22 Beta
2) in isolated containers. The managed mode requires Linux amd64 and Docker.
The local `1.0.0rc1` candidate is a prerelease and has not been published.
See the [Linux amd64 installation guide](docs/LINUX-V1-INSTALL.md).

Web administration controls credentials, target registration, grants, and
infrastructure. The restricted broker exposes only bounded operations on owned
resources. MCP clients cannot request shell, Git, Docker, arbitrary paths, or
raw JSON-RPC access.

Web administration owns infrastructure, registration, credentials, presets,
grants, production scopes, pairing, approvals and lifecycle. MCP is a stateless
target/development interface with explicit target UUIDs, bounded schemas,
durable replay safety and no shell, Git, Docker, caller-owned paths, arbitrary
execution or raw JSON-RPC.

The original Kodi Lab code and the Kodi Lab bridge are MIT licensed. Kodi,
Ubuntu, Python, Mesa/Xvfb, and other runtime components retain their own
licenses. Kodi Lab is independent of and not affiliated with the Kodi project.
The four prebuilt runtime archives are **not yet approved for public
distribution**; their compliance review is pending. See
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

[`docs/V1-SPEC.md`](docs/V1-SPEC.md) is the authoritative v1 product and
architecture specification.

For the local Linux amd64 release-candidate installation path, see
[`docs/LINUX-V1-INSTALL.md`](docs/LINUX-V1-INSTALL.md) and the
[`release artifact contract`](docs/LINUX-V1-RELEASE.md). The Kodi-version
addition and retirement audit is in
[`docs/KODI-VERSION-EXTENSIBILITY.md`](docs/KODI-VERSION-EXTENSIBILITY.md).

## Development

The supported development host baseline is Python 3.12 on Ubuntu 24.04.

```console
uv sync --dev
./scripts/check-focused tests/runtime/test_manifest.py
./scripts/check-integration --focused tests/runtime/test_manifest.py --integration tests/runtime/test_distribution.py
./scripts/check  # release coverage only
```

The focused command is the default for ordinary changes; select every new or
materially changed test file and its affected subsystem. Use integration when
real subsystem interaction warrants it. `./scripts/check` is the complete
release gate and is not mandatory for every task. The selection rules and
measured costs are in [CONTRIBUTING.md](CONTRIBUTING.md) and
[docs/TEST-PERFORMANCE.md](docs/TEST-PERFORMANCE.md).

For optional local path overrides, copy `.env.example` to `.env` and edit the
two existing bootstrap paths. A `.env` file is ignored by Git and is not
required in production; the same variables may be supplied by the environment.

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the complete local foundation
check and [`docs/IMPLEMENTATION-PLAN.md`](docs/IMPLEMENTATION-PLAN.md) for the
phased scope.

For a foreground, artifact-installed host that connects to remote Kodi, see
[`docs/REMOTE-HOST-ARTIFACT.md`](docs/REMOTE-HOST-ARTIFACT.md). This Ubuntu
procedure does not validate Windows or macOS host operation.

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

The authoritative V1 MCP catalog is exactly `addon_list`,
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

**Phase 13 — Remote targets is COMPLETE.** It adds Web-only external-development
registration, fixed native verification/tools, and an authenticated
screenshot/deployment bridge. Independent formal review closed D-0302's
replay-capacity and native-deadline remediation without adding production
authority. Password and bridge secrets require an operator-provided
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

## Production targets

Phase 14 keeps production separate from external development. Exactly five of
the fifteen MCP tools are production-eligible: `target_list`, `target_status`,
`addon_list`, `screenshot_capture` and `remote_action`. Production has no
workspace/deployment tool. Native inspection, bridge-backed visual inspection
and remote control each require exact Web-assigned presets, grants and current
generation/revision-bound scopes.

Production remote control is server-owned and semantic. Standing authority can
permit only `up`, `down`, `left`, `right`, `back`, `home` and `info`.
`play_pause`, `stop`, `previous`, `next`, `volume_up`, `volume_down` and
`mute_toggle` require one-shot Web approval. `context_menu` and `select` are
always prohibited. Mutation vetoes start blocked, exact request replay cannot
dispatch twice, and ambiguity stops rather than retries.

The production bridge supplies identity and bounded screenshots, not arbitrary
control. Operators manually install an exact reviewed release ZIP after
checking its published SHA-256; Web then registers the reviewed build, pairs
with a show-once token and explicitly confirms provenance. Kodi Lab does not
install/update production software or own Kodi/host lifecycle. Automatic bridge
updates, hosted-repository bootstrap, generic production add-on installation,
raw JSON-RPC, shell/SSH/filesystem access and workspace deployment to production
are outside V1 authority.

Bridge 1.1.7 remains exact Phase 13 review evidence, but its packaged README
and metadata describe development-only use. Correcting those bytes changes the
reviewed ZIP digest. Bridge 1.1.8 is the local packaging-only corrected
candidate; it is not published, Web-registered or installed on production, and
Stage A-D remain unexecuted. Stage B must use its exact independently accepted
artifact. See the [bridge trust model](docs/BRIDGE.md),
[production operations](docs/PRODUCTION-OPERATIONS.md), the
[non-executed validation runbook](docs/PHASE14-VALIDATION-RUNBOOK.md) and the
[redacted evidence template](docs/PHASE14-EVIDENCE-TEMPLATE.md).

Kodi Lab follows least authority: start with Production Inspection, add visual
authority only for screenshots, and use Production Remote Kodi only when
bounded control is deliberately intended. Production screenshot pixels are not
durably retained by Kodi Lab, although a receiving MCP client may persist them.

The machine-specific production-profile acceptance fixture is retained in
private local evidence. The product's production-target authorization and
inspection tests remain in this repository.
