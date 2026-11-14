# MCP tools and permissions

Use this reference to choose what an agent may do. A tool appearing in
`tools/list` is not permission to call it on every Kodi. Modern and legacy MCP
transports share this complete inventory and the same authorization checks.

## Start with your Kodi environment

| Environment | Starting permission posture | Where to configure access in the Web UI |
| --- | --- | --- |
| **Managed Kodi** | A basic client has no Kodi access. Explicit **Trusted Managed Lab — Full Development** covers all installed, approved disposable versions, including versions installed later; it grants no external access. | **Agents → Managed Kodi agent**: enable the role. For narrower inspection, use **Advanced → Managed Kodi — Read-only inspection**, client/Kodi policies and exact target grants instead. Humans install missing versions in **Managed Kodi**. |
| **External Production** | Connecting Kodi grants no agent access. **Production Inspection** with an exact target grant and current inspect scope permits inspection. Screenshots and remote navigation default **Off**; development and lifecycle operations are prohibited. | **Agents**: client preset, **Grant**, then **Grant current inspect scope**. On the individual Kodi: **Allow screenshots** and **Allow remote navigation**. Playback/volume have separate approval controls. |
| **External Development** | Pairing grants no agent access or blanket write permission. The simple connection flow's capability maximum starts with inspection and screenshots; **Allow screenshots** defaults **Off**. Control, inventory and deployment need explicit additional support and authority. | **Agents → Advanced administration**: client/Kodi policies and exact target grants. On that Kodi: **Allow screenshots**; **External Development Kodi → Advanced options → Amend development capability maximum** for additional verified capabilities. |

A managed role replaces individual managed grants for its eligible instances.
To restrict an agent to particular managed Kodi or inspection only, revoke that
role and use the narrower client/Kodi policies and exact grants. The role never
changes separately approved external access. Active-use, resource and durable
replay guards remain in force.

## Read the indicators

**Managed**, **Prod** and **Dev** refer to the three environments above.

- **Role** — granted after the explicit Trusted Managed Lab role is enabled;
  the row's ownership and runtime prerequisites still apply. Legacy/custom
  clients instead need the stated permissions in both client and Kodi policies
  plus the exact target grant, where supported.
- **Opt-in** — Off in the starting external setup; an administrator must enable
  the stated toggle or capability maximum as well as policy and grants.
- **Conditional** — supported only with the stated policy, grants and
  prerequisites; inspect the row for its limits.
- **Prohibited** — unsupported for that target class; a Web override cannot
  enable it.
- **N/A** — no Kodi target argument or target operation; source/workspace
  ownership and permissions still apply. `managed_status` is a managed-only
  summary, despite having no target argument.

**How to read a row:** `screenshot_capture` needs `target.inspect`. On managed
Kodi the role can supply authority, subject to supported capture and current
runtime identity. On either external class, **Opt-in** means **Allow screenshots**
must be On for that exact Kodi, in addition to the client/Kodi policies, target
grant, verified capture capability, paired/reviewed Bridge and transport consent.
Production also needs its current inspect scope. A separate visual preset or
scope is not required. Enabling a toggle does not prove Kodi is reachable or
that a capture will succeed.

## Authority checks that still apply

1. **Client and credential:** choose a preset in **Agents** and issue an unexpired
   credential. Disablement, revocation and permission changes are rechecked.
   Production built-in presets have fixed permissions; custom presets cannot
   expand production eligibility.
2. **Exact Kodi and settings:** external access needs its own policy and target
   grant; production additionally needs confirmed designation, current native
   identity and scope. Capability maxima limit available operations. Production
   emergency blocking and per-target suspension override remote-action toggles.
3. **Project/source/workspace:** an agent acts only on its owned projects and
   workspaces with exact source authority. Owned intake establishes the private
   source/workspace; manually registered sources need grants in **Agents →
   Advanced administration**. Later deployment binds an exact target and adds
   that target's independent checks.
4. **Native prerequisites:** capabilities, reconciled state, readiness, reviewed
   runtime support and native/Bridge identity can still deny an authorized call.
   External HTTP screenshots need transport-risk consent; managed capture has
   no external screenshot toggle. External Development's semantic-remote maximum
   is separate from production's **Allow remote navigation** preference.

Jump to [inspection and GUI](#target-inspection-and-gui),
[managed lifecycle](#managed-lifecycle),
[target-bound development](#target-bound-development), or
[target-independent project tools](#target-independent-projects-packages-and-workspaces).

<!-- mcp-tool-matrix:start -->

## Target inspection and GUI

| Tool | Purpose | Managed | Prod | Dev | Required permissions / Web controls |
| --- | --- | --- | --- | --- | --- |
| `target_list` | List accessible Kodi targets | Role | Conditional | Conditional | `target.inspect`; filters by current access, not all registered Kodi. |
| `target_status` | Read target health and identity | Role | Conditional | Conditional | `target.inspect`; exact target access; status can describe unavailable Kodi. |
| `addon_list` | Read installed add-on inventory | Role | Conditional | Opt-in | `target.inspect`; production inspection includes this read; development needs verified `addon_inventory` maximum. |
| `inspect_ui_state` | Read supported GUI state | Role | Conditional | Conditional | `target.inspect`; reviewed Bridge enriches observations; bounded native fallback for external Kodi. |
| `log_read` | Read bounded Kodi log output | Role | Conditional | Conditional | `target.inspect`; supported managed collector or trusted external Bridge log capability; no caller-selected file. |
| `screenshot_capture` | Capture current GUI image | Role | Opt-in | Opt-in | `target.inspect`; external **Allow screenshots**, verified capture capability, paired/reviewed Bridge and transport consent. No separate visual preset required. |
| `screenshot_retrieve` | Retrieve an exact recent capture | Role | Prohibited | Prohibited | `target.inspect`; only the existing managed capture returned by an observed remote action; never recaptures. |
| `remote_action` | Send a closed semantic GUI action | Role | Opt-in | Opt-in | Managed/development: `target.control`; development needs verified `semantic_remote` maximum. Production uses the action rules below. |

For production `remote_action`, **Allow remote navigation** permits only Up,
Down, Left, Right, Back, Home and Info with inspection access and a trusted
Bridge; no control preset or navigation scope is required. Playback/volume need
**Production Remote Kodi**, `request_transient_control` scope, exact one-shot
Web approval and native-control HTTP consent when applicable. Global emergency
blocking and target suspension override both paths. **Select and Context menu
are always prohibited on External Production Kodi**, even when `remote_action`
is listed. See [production operations](PRODUCTION-OPERATIONS.md).

## Managed lifecycle

| Tool | Purpose | Managed | Prod | Dev | Required permissions / Web controls |
| --- | --- | --- | --- | --- | --- |
| `managed_status` | Summarize installed managed versions | N/A | N/A | N/A | `managed.versions`; managed-only inventory, no target argument. Role or explicit custom-preset opt-in. |
| `managed_start` | Start an installed version | Role | Prohibited | Prohibited | `managed.versions`; role or explicit client/Kodi opt-in and target grant; missing installation needs a human. |
| `managed_stop` | Stop an installed version | Role | Prohibited | Prohibited | `managed.versions`; same authority and active-use guards. |
| `managed_reset` | Reset an idle disposable profile | Role | Prohibited | Prohibited | `managed.versions` + `managed.reset`; role or explicit opt-ins; durable Reset guards and reviewed Bridge preparation. |
| `managed_ensure_ready` | Acquire readiness for managed work | Role | Prohibited | Prohibited | `managed.lifecycle`; role or explicit custom-preset opt-in; installed target and lifecycle guards. |
| `managed_release` | Release managed lifecycle ownership | Role | Prohibited | Prohibited | `managed.lifecycle`; releases only eligible agent-owned lifecycle state. |
| `managed_selection_preview` | Preview workspace/version binding | Role | Prohibited | Prohibited | `managed.lifecycle` + `workspace.inspect`; exact owned workspace; no implicit switch. |
| `managed_select` | Bind a workspace and managed version | Role | Prohibited | Prohibited | `managed.lifecycle` + `workspace.inspect`; exact ownership and selection/lifecycle guards. |

## Target-bound development

| Tool | Purpose | Managed | Prod | Dev | Required permissions / Web controls |
| --- | --- | --- | --- | --- | --- |
| `workspace_deploy_preflight` | Explain exact deployment readiness | Role | Prohibited | Conditional | `workspace.deploy`; workspace/source ownership plus target policies/grants; reports blockers without deploying. |
| `workspace_deploy` | Deploy an exact workspace generation | Role | Prohibited | Opt-in | `workspace.deploy`; ready target; development needs reviewed `workspace_deployment` maximum and Bridge. |
| `workspace_execute` | Run a fixed development workflow | Role | Prohibited | Opt-in | `development.execute`; exact deployment; development needs `fixed_execution` maximum and reviewed Bridge support. |
| `workspace_launch` | Launch workspace with durable receipt | Role | Prohibited | Prohibited | `development.execute` + `target.control`; exact managed workspace/deployment and ownership. |
| `development_launch` | Launch the declared development entry | Role | Prohibited | Prohibited | `development.launch` + `workspace.inspect`; role or explicit opt-in; owned project or exact selected session. |
| `workspace_execution_recover` | Recover uncertain managed execution | Role | Prohibited | Prohibited | `development.execute` + `target.control`; exact retained operation and supported recovery guards. |
| `addon_settings_set` | Apply declared add-on settings | Role | Prohibited | Prohibited | `development.settings.write` + `workspace.inspect`; role or explicit opt-in; owned project or selected managed session. |
| `addon_settings_result` | Read exact settings receipt | Role | Prohibited | Prohibited | Same settings/workspace permissions; exact operation ownership. |
| `promotion_request` | Prepare evidence for human review | Conditional | Prohibited | Opt-in | `development.request_promotion`; eligible deployed workspace, target execution/deployment support; agent-owned projects excluded. Never publishes. |
| `package_install` | Deliver a retained original ZIP | Conditional | Prohibited | Prohibited | Trusted managed role (includes `addon.install`), owned package and target checks; delivery/status do not prove native GUI consumption. |
| `package_install_status` | Read or verify a ZIP-delivery receipt | Conditional | Prohibited | Prohibited | `workspace.inspect` + Trusted Managed Lab; exact owned operation binds its managed target; optional verification inspects that Kodi. |

## Target-independent projects, packages and workspaces

These tools act on Lab project state rather than a Kodi runtime. **N/A**
grants no external filesystem access. Deployment is a separate target-bound step.

| Tool | Purpose | Managed | Prod | Dev | Required permissions / Web controls |
| --- | --- | --- | --- | --- | --- |
| `project_create` | Create a private add-on project | N/A | N/A | N/A | `workspace.edit` + Trusted Managed Lab; owner-bound intake. |
| `project_transfer` | Transfer bounded source-package chunks | N/A | N/A | N/A | `workspace.edit` + Trusted Managed Lab; exact transfer identity and checksum. |
| `project_import` | Import reviewed source into a project | N/A | N/A | N/A | `workspace.edit` + Trusted Managed Lab; supported bounded package and provenance. |
| `project_status` | Inspect an owned project | N/A | N/A | N/A | `workspace.inspect` + Trusted Managed Lab; exact owner. |
| `package_import` | Retain an original installable ZIP | N/A | N/A | N/A | `workspace.edit` + Trusted Managed Lab; package intake checks; no installation. |
| `package_status` | Inspect a retained original ZIP | N/A | N/A | N/A | `workspace.inspect` + Trusted Managed Lab; owned package only. |
| `development_source_list` | List accessible source registrations | N/A | N/A | N/A | `workspace.inspect`; exact source grants; no host directory browsing. |
| `workspace_create` | Create a workspace from a source | N/A | N/A | N/A | `workspace.edit`; granted registered source; isolated agent ownership. |
| `workspace_import_preview` | Preview source import differences | N/A | N/A | N/A | `workspace.edit` + `development_source.inspect`; owned workspace and source grant. |
| `workspace_import` | Import a reviewed source generation | N/A | N/A | N/A | Same import permissions; exact preview/generation and ownership. |
| `workspace_source_scan` | Scan bounded registered source state | N/A | N/A | N/A | `development_source.inspect`; role or explicit opt-in; source grant and workspace ownership. |
| `workspace_capture` | Capture source into a workspace | N/A | N/A | N/A | `workspace.capture`; role or explicit opt-in; exact source/generation; no target capture. |
| `workspace_status` | Read workspace state and receipts | N/A | N/A | N/A | `workspace.inspect`; owned workspace and source grant. |
| `workspace_tree` | List bounded workspace files | N/A | N/A | N/A | `workspace.inspect`; present, ready workspace; no arbitrary host path. |
| `workspace_read` | Read a bounded workspace file | N/A | N/A | N/A | `workspace.inspect`; present, ready workspace and safe relative path. |
| `workspace_edit` | Edit an exact workspace generation | N/A | N/A | N/A | `workspace.edit`; present, ready workspace; generation and path checks. |
| `workspace_validate` | Run fixed structural validation | N/A | N/A | N/A | `workspace.inspect`; present, ready workspace; no arbitrary shell/test command. |

<!-- mcp-tool-matrix:end -->

## Outcomes and maintenance

Tool results retain typed success, denial, blocked, pending, unknown and uncertain
outcomes. A pending approval or durable receipt is not proof of execution or
native GUI acceptance. Original-ZIP delivery is not the supported automated source
deployment workflow; remaining original-ZIP GUI consumption qualification is
tracked separately in [#305](https://github.com/kcook98765/kodi-lab/issues/305).
Repository installation by ID remains deferred in
[#339](https://github.com/kcook98765/kodi-lab/issues/339).

The [offline inventory check](../tests/web/test_mcp_tool_documentation.py) compares
each exact tool row with the registry and authenticated modern/legacy `tools/list`
catalogs, and checks README navigation. It catches missing, renamed and duplicate
tools; permission prose still needs source review when policy changes.

For deeper contracts, see [MCP authorization](PHASE11-MCP.md),
[development tools](PHASE12-MCP-DEVELOPMENT.md),
[Trusted Managed Lab](TRUSTED-MANAGED-LAB.md),
[external production controls](EXTERNAL-PRODUCTION-UI.md) and
[external development controls](EXTERNAL-DEVELOPMENT-UI.md).

[Back to agent connection setup](../README.md#connect-your-ai-agent).
