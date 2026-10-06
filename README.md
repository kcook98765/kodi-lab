# Kodi Lab

Kodi Lab is a self-hosted Web application for developing, testing, and
inspecting Kodi installations. Remote-target mode connects to an existing Kodi;
managed-lab mode provisions disposable Kodi instances in isolated containers.
The stable Linux amd64 V1 release set is Matrix 19.4, Nexus 20.5, and Omega
21.3. The earlier four-runtime lab (including Kodi 19.5 and Piers Beta 2)
remains private compatibility evidence, not the installable V1 set. Windows,
macOS, and ARM64/Apple Silicon host support are post-V1. The local `1.0.0rc1`
candidate is a prerelease and has not been published.
For a source checkout, use the [development setup](CONTRIBUTING.md) and the
[remote-Web Compose startup](docs/REMOTE-MODE-CONTAINER.md#build-and-initialize)
to connect to existing Kodi targets. For managed V1, start from a clean committed
checkout and [stage a local bundle](docs/LINUX-V1-INSTALL.md#candidate-staging),
then follow the [Linux amd64 installation guide](docs/LINUX-V1-INSTALL.md) to
acquire Matrix 19.4, Nexus 20.5, or Omega 21.3 through reviewed recipes.

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
The four historical prebuilt runtime archives remain private test artifacts;
the current release bundle distributes reviewed recipes and no Kodi binaries.
See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).

Stopping a provisioned managed instance retains its container and writable
layer; deprovisioning removes them while retaining the managed record and
durable Kodi Lab state. Fixtures, Snapshots, preserved Snapshots, and recovery
contracts provide bounded preservation. External/production Kodi targets may
be long-lived installations and are not disposable managed containers.

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

The current V1 MCP registry contains eighteen tools: `addon_list`,
`development_source_list`, `promotion_request`, `remote_action`,
`screenshot_capture`, `target_list`, `target_status`, `workspace_capture`,
`workspace_create`, `workspace_deploy`, `workspace_edit`, `workspace_execute`,
`workspace_execution_recover`, `workspace_read`, `workspace_source_scan`,
`workspace_status`, `workspace_tree`, and `workspace_validate`. Discovery exposes
only the subset allowed by the client's current permissions.
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

Registered-source scan and capture use the separate opt-in permissions
`development_source.inspect` and `workspace.capture`. The narrow
`workspace_execution_recover` tool requires both `target.control` and
`development.execute` and applies only to an exact timed-out ambiguous smoke on
an internal disposable target. It contains the original execution without
rewriting or redispatching it; see D-0405 in [DECISIONS.md](docs/DECISIONS.md).

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
AgentWorkspaces; MCP creation binds one durable owner atomically. At that
checkpoint, the exact 15-tool catalog supported bounded inspection, creation,
editing, validation, internal disposable deployment, fixed execution, and
promotion-plan request. D-0277
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
explicit test setting. That checkpoint retained the fifteen-tool catalog.
Eligible paired external targets use the existing `screenshot_capture`, while
exact bridge-backed `workspace_deploy`, native fixed `workspace_execute` and
promotion evidence use their existing schemas. See
[`docs/PHASE13-EXTERNAL-TARGETS.md`](docs/PHASE13-EXTERNAL-TARGETS.md).

For a development bridge, install
`bridge/service.kodi_lab_bridge` as an ordinary Kodi service add-on. Keep it
disabled until the external target is registered, native-verified, explicitly
confirmed/enabled and configured with the required capability maximum. Enable
bridge 1.1.14, obtain its four-character code, and enter that code with the
allowlisted bridge port in Kodi Lab's Web onboarding flow. Kodi Lab uses the
shared `otp-1` protocol and confirms the authenticated bridge identity.
Re-pairing rotates the secret
and revocation disables screenshot/deployment authority immediately. The add-on
exposes only identity, capability, screenshot and pathless content-addressed
deployment operations; fixed execution remains native. Establish the exact
DevelopmentSource baseline in Web before deployment and use only Web receipt
reconciliation after ambiguity. With HTTP,
HMAC protects integrity but PNG bytes are cleartext to observers on that LAN;
use normally validated HTTPS when confidentiality is required.

## Production targets

Phase 14 keeps production separate from external development. Exactly five of
the eighteen current MCP tools are production-eligible: `target_list`,
`target_status`, `addon_list`, `screenshot_capture` and `remote_action`. Production has no
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
bridge 1.1.14 with its four-character `otp-1` code and explicitly confirms
provenance. Kodi Lab does not
install/update production software or own Kodi/host lifecycle. Automatic bridge
updates, hosted-repository bootstrap, generic production add-on installation,
raw JSON-RPC, shell/SSH/filesystem access and workspace deployment to production
are outside V1 authority.

A source-free local deployment candidate built from this checkout includes the
exact reviewed
`service.kodi_lab_bridge-1.1.14.zip` and `BRIDGE.json` provenance under `bridge/`.
The bundle verifier checks the ZIP hash, closed inventory, descriptor, license,
and Git source identity. Historical 1.1.7/1.1.8 packages remain separate review
evidence; they are not the current `otp-1` package. See the [bridge trust model](docs/BRIDGE.md),
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
