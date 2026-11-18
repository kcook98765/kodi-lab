# MCP tools and permissions

Use this reference to choose what an agent may do. A tool appearing in
`tools/list` is not permission to call it on every Kodi. Modern and legacy MCP
transports share this complete **46-tool** inventory and the same authorization checks.
The catalog is filtered for the credential; discovery does not grant target access.

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
| `system_info` | Read Kodi system/OS diagnostics | Role | Conditional | Conditional | `target.inspect`; exact target access and current production inspection scope; native metrics need no Bridge; optional reviewed `system_snapshot` enrichment. See [system diagnostics](SYSTEM-INFO.md). |
| `target_status` | Read target health and identity | Role | Conditional | Conditional | `target.inspect`; exact target access; status can describe unavailable Kodi. |
| `addon_list` | Read installed add-on inventory | Role | Conditional | Opt-in | `target.inspect`; production inspection includes this read; development needs verified `addon_inventory` maximum. |
| `inspect_ui_state` | Read supported GUI state | Role | Conditional | Conditional | `target.inspect`; optional trusted `ui_observation` Bridge; bounded native fallback, including managed when no trust record exists; invalid managed trust remains a denial. See [UI observation](UI-OBSERVATION.md). |
| `log_read` | Read bounded Kodi log output | Role | Conditional | Conditional | `target.inspect`; trusted `log_read` Bridge capability for every target class, including managed; no caller-selected file. See [logs](KODI-LOGS.md). |
| `log_scan` | Search and count fixed Kodi logs with resumable scans | Role | Conditional | Conditional | `target.inspect`; trusted `log_scan` Bridge capability; existing production trust, transport acknowledgement and publication reauthorization. See [Kodi log filters, counts and continuation](KODI-LOGS.md). |
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
| `managed_start` | Start an installed version | Role | Prohibited | Prohibited | `managed.versions`; role or explicit client/Kodi opt-in and target grant. Role callers need human installation; legacy callers may prepare an absent profile from an approved cached runtime. Never downloads. |
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
| `addon_settings_set` | Apply declared add-on settings | Role | Prohibited | Prohibited | `development.settings.write` + `workspace.inspect`; role or explicit opt-in; exact selected managed session, including for owned projects; **Nexus 20.5/Omega 21.3 only** with current reviewed Bridge. See [settings](ADDON-SETTINGS.md). |
| `addon_settings_result` | Read exact settings receipt | Role | Prohibited | Prohibited | Same settings/workspace permissions; exact operation ownership and selected profile; **Nexus 20.5/Omega 21.3 only**. |
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

## Inputs, effects and results

Discover the installed server with authenticated MCP `tools/list`; its
`inputSchema` and `outputSchema` include field types, enums, bounds and nested
objects for that exact version. Modern and legacy catalogs agree. The table
below lists **all required fields** and optional top-level fields. IDs are UUIDs;
`expected_generation` is the observed current generation, never the desired next
one. `version` is `matrix`, `nexus`, `omega` or `piers` for version lifecycle tools.
Unknown arguments are rejected. Conditional fields still apply: for example,
project import requires exactly one byte transport and role Reset requires its
observed profile. Consult the linked contracts before a mutation.

**Read** means read-only in the registry; it can still collect sensitive data,
retain metadata or advance scan progress. **Write** changes Lab or Kodi state;
Reset discards the writable Kodi profile. Capture/scan are not software installers.
`request_id` identifies an operation; save it before sending. Recover the same
intent with its documented receipt/status path after response loss. Settings
writes use the separate result tool; an uncertain mutation is never permission
to allocate a replacement UUID. Pagination cursors and scan cursors are distinct.
List limits are bounded; `next_cursor`/`next_line` indicate remaining results.

| Tool | Required arguments | Optional arguments | Effect and result |
| --- | --- | --- | --- |
| `target_list` | None | `cursor`, `limit` | **Read**. Paged accessible target summaries and `next_cursor`; capability hints do not prove live operation support. |
| `system_info` | `target_id` | None | **Read**. 54 native metrics plus optional seven-field enrichment, each with availability/provenance; partial is successful. [Details](SYSTEM-INFO.md). |
| `target_status` | `target_id` | None | **Read**. One semantic target summary: runtime/process/JSON-RPC/readiness states, class and capability hints; no hardware polling. |
| `addon_list` | `target_id` | `cursor`, `limit` | **Read**. Paged semantic inventory with `complete`, `total_count`, `addons`, `next_cursor` and provenance classifications; unavailable inventory is a typed error. No Bridge needed. |
| `inspect_ui_state` | `target_id` | None | **Read**. Schema-v2 window/dialog/focus/container/item/playback/UI state and semantic resolution; non-atomic, unknowns remain null. [Details](UI-OBSERVATION.md). |
| `log_read` | `target_id` | `log`, `mode`, `max_lines`, `max_bytes`, `before`, `snapshot_id` | **Read**. Metadata by default; bounded tail and snapshot-bound older bytes. `log_changed` requires a new observation. [Limits/errors](KODI-LOGS.md#existing-log_read-stats-and-tail). |
| `log_scan` | `target_id` | `logs`, `severities`, `text`, `start_time`, `end_time`, `context_records`, `max_matches`, `cursor` | **Read**. Cumulative counts/samples, per-file coverage, exactness and continuation cursor; partial zero is not an error-free scan. [Limits/errors](KODI-LOGS.md). |
| `screenshot_capture` | `target_id` | `request_id` | **Read**. PNG image content plus capture metadata; **production requires `request_id`**. No durable external pixels; completed external replay never recaptures and may report replay unavailable. |
| `screenshot_retrieve` | `target_id`, `request_id`, `capture_id` | None | **Read**. Exact retained managed action PNG plus age/runtime metadata, never a new capture; expires after two minutes or earlier replacement. |
| `remote_action` | `request_id`, `target_id`, `action` | `observe_ui` | **Write**. Durable action receipt, or production approval-required/terminal result. Managed `observe_ui=true` adds separately authorized post-action evidence. [Replay](REMOTE-ACTION-OBSERVATIONS.md). |
| `managed_status` | None | None | **Read**. Supported version slots and authorized profile identities; stored Bridge facts are separate from native readiness. |
| `managed_start` | `version`, `request_id` | None | **Write**. Exact version operation/child receipt and current status; replay never restarts a completed request after Stop. |
| `managed_stop` | `version`, `request_id` | None | **Write**. Receipt and `stop_confirmed`; blocked/retained or an old receipt is not proof of a current stop. |
| `managed_reset` | `version`, `request_id` | `restart`, `expected_profile`, `resume_bridge_version` | **Write**. Destructive exact-profile replacement receipt and separate Bridge preparation state. Role callers must supply `expected_profile` from status; default stopped. [Recovery](MANAGED-AGENT-RESET.md). |
| `managed_ensure_ready` | `request_id`, `target_id` | `hold_minutes` | **Write**. Starts only the existing provisioned instance; readiness result and optional hold (0–240 minutes, default 0). Replay never extends it. |
| `managed_release` | `request_id`, `target_id` | None | **Write**. Releases only own use/hold; returns retained while another user, playback or unresolved operation protects Kodi. |
| `managed_selection_preview` | `target_id`, `workspace_id` | None | **Read**. Current selection generation plus opaque profile/runtime identity; read-only, no reservation. |
| `managed_select` | `request_id`, `expected_generation`, `target_id`, `workspace_id`, `identity` | None | **Write**. Selection receipt; uses preview identity and latest generation, requires stopped/idle runtimes, and never starts/deletes profiles. |
| `workspace_deploy_preflight` | `workspace_id`, `expected_generation`, `target_id` | `deployment_mode` | **Read**. Durable-only blockers and next actions: blocked or unknown, never ready; external/repository readiness remains unsupported/unknown. |
| `workspace_deploy` | `request_id`, `workspace_id`, `expected_generation`, `target_id` | `deployment_mode` | **Write**. Exact deployment receipt; managed direct copy/restart or eligible `verified_existing` reuse. External repository mode can await Web approval/install/Recheck. [Workflow](MANAGED-ADDON-INSTALLATION.md). |
| `workspace_execute` | `request_id`, `workspace_id`, `expected_generation`, `target_id` | `execution_kind` | **Write**. Fixed smoke or managed negative RPC diagnostic receipt. Smoke success is bounded invocation evidence, not feature acceptance. |
| `workspace_launch` | `request_id`, `workspace_id`, `expected_generation`, `target_id`, `extension_ordinal` | None | **Write**. Manifest-entry dispatch receipt; accepted dispatch is not completion/UI acceptance. Current reviewed managed Bridge required. |
| `development_launch` | `request_id`, `workspace_id`, `expected_generation`, `target_id`, `extension_ordinal` | None | **Write**. Same manifest receipt under narrower launch permission, matching owned-project or Current Kodi authority; no general control permission implied. |
| `workspace_execution_recover` | `request_id`, `execution_operation_id` | None | **Write**. Separate recovery receipt for eligible ambiguous managed smoke; contains/stops and verifies provenance without rerunning. Definitive failure is ineligible. |
| `addon_settings_set` | `request_id`, `expected_session_generation`, `expected_workspace_generation`, `settings` | None | **Write**. Non-atomic per-key, value-free results for up to 32 declared settings on server-derived selected add-on/target; uncertain writes are not repeated. |
| `addon_settings_result` | `request_id` | None | **Read**. Original value-free receipt on the still-selected exact profile; no current-value read, native call or repeat write. |
| `promotion_request` | `request_id`, `workspace_id`, `expected_generation`, `target_id` | None | **Write**. Immutable exact-generation plan for human Web review; no approval, application or publication. |
| `package_install` | `request_id`, `target_id`, `artifact_id` | None | **Write**. Expiring, exact-target original-ZIP `source_url` and receipt; delivery is not installation. Requires configured native ZIP origin. [Limits](NATIVE-ZIP-INSTALLATION.md). |
| `package_install_status` | `operation_id` | `verify` | **Read**. Owned delivery receipt; `verify=true` performs bounded native verification and records it, without installation or Start. |
| `project_create` | `request_id`, `addon_id`, `display_name` | `version`, `kind` | **Write**. Inert private Program/video project plus owned workspace; exact replay preserves subsequent edits. |
| `project_transfer` | `request_id`, `archive_sha256`, `archive_size` | `action`, `offset`, `chunk_base64` | **Write**. Owned ordered chunk transfer status/`next_offset`; status/cancel retain request identity. 32 KiB decoded chunks, 4 MiB archive, 24-hour expiry. |
| `project_import` | `request_id`, `archive_sha256`, `intake_kind`, `provenance_reference` | `archive_base64`, `transfer_id` | **Write**. Private editable project/workspace, not native installation. Supply exactly one of inline bytes or a ready owned transfer. [Types/limits](AGENT-PROJECTS.md). |
| `project_status` | None | `project_id`, `cursor`, `limit` | **Read**. One owned project or paged records, including interrupted reserved intake; no native/filesystem probe. |
| `package_import` | `request_id`, `archive_base64`, `archive_sha256`, `provenance_reference` | None | **Write**. Retained original video-plugin/passive-repository ZIP identity, no installation. SHA-256/provenance and package security checks required. |
| `package_status` | `artifact_id` | None | **Read**. Owned original-package status; no native probe or installation. |
| `development_source_list` | None | `cursor`, `limit` | **Read**. Paged granted registration metadata; not a fresh filesystem scan or directory browser. |
| `workspace_create` | `request_id`, `source_id` | None | **Write**. Replay-safe private workspace from an exact granted source; request receipt survives response loss. |
| `workspace_import_preview` | `workspace_id`, `expected_generation` | `mode`, `resolutions` | **Read**. Source/preview digests, per-path changes/conflicts and limits; reviewed-tree resolutions require another matching preview. |
| `workspace_import` | `workspace_id`, `expected_generation`, `request_id`, `source_digest`, `preview_digest` | `mode`, `resolutions` | **Write**. Applies the exact preview as one generation and returns receipt/digests; never deploys or restarts. [Modes](WORKSPACE-BATCH-IMPORT.md). |
| `workspace_source_scan` | `workspace_id` | None | **Read**. Fresh bounded source/capture/workspace identities and private-edit status; changes no bytes. |
| `workspace_capture` | `workspace_id`, `expected_generation` | None | **Write**. Changed flag, operation, generation and digest; replaces only eligible clean workspace bytes, not Kodi. Expected generation is current, not next. |
| `workspace_status` | None | `workspace_id`, `cursor`, `limit` | **Read**. One owned workspace or paged summaries, generations, retention and receipts; not a Kodi liveness probe. |
| `workspace_tree` | `workspace_id` | `relative_path`, `cursor`, `limit` | **Read**. Paged non-recursive directory entries and generation; safe relative path only, no host browsing. |
| `workspace_read` | `workspace_id`, `relative_path` | `start_line`, `max_lines` | **Read**. Bounded UTF-8 segment, generation, file digest and `next_line`; follow it before replacing a whole file. |
| `workspace_edit` | `workspace_id`, `expected_generation`, `operation`, `relative_path` | `content`, `expected_text`, `replacement_text` | **Write**. New generation/digest. `create`/`replace` require complete `content`; `exact_patch` requires complete `expected_text` and `replacement_text`. Other-mode text fields are forbidden. |
| `workspace_validate` | `workspace_id`, `expected_generation` | None | **Read**. Closed structural validation of the exact generation; no caller command, arbitrary tests or Kodi execution. |

Native status, add-on inventory and the 54 base `system_info` metrics need no
Bridge. UI observation can use native fallback; `log_read` and `log_scan` require
trusted Bridge capabilities on every target class. External screenshots require
Bridge; managed X11 screenshots and native remote input have independent paths.
Manifest launch/settings require the exact current managed reviewed build and
private runtime binding. `fixed_execution` is a native development operation,
not an advertised Bridge capability. Diagnostics never install a Bridge. There
is no standalone MCP Bridge installer;
role-authorized `managed_reset` separately prepares the reviewed Bridge as part
of profile replacement. No tool grants itself permissions. See the
[Bridge matrix](BRIDGE.md#version-and-capability-matrix).

## Outcomes and maintenance

Tool results retain typed success, denial, blocked, pending, unknown and uncertain
outcomes. A pending approval or durable receipt is not proof of execution or
native GUI acceptance. Agent-owned source development and direct managed deployment are supported;
[#303](https://github.com/kcook98765/kodi-lab/issues/303) is Completed.
Native original-ZIP GUI consumer installation remains unverified and is not
currently pursued ([#305, Not planned](https://github.com/kcook98765/kodi-lab/issues/305)).
Unattended repository install-by-ID is unsupported and is not currently pursued
([#339, Not planned](https://github.com/kcook98765/kodi-lab/issues/339)). Existing
repository workflows, ZIP intake/delivery protections and historical results
are unchanged. System diagnostics [#380](https://github.com/kcook98765/kodi-lab/issues/380)
is Completed; this does not imply deployment to an installed server or Bridge.

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
