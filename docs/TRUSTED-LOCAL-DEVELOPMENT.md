# Trusted local development agent

Use a separate, explicitly approved agent for one selected local add-on and its
managed Kodi. It can inspect, Start/Stop, edit, deploy and launch a reviewed
manifest entry without general Kodi control. Navigation remains a separate slice.

## One-time setup

In **Agents & Permissions → Advanced administration → Create custom preset**,
choose **Add-on Development** as the permission ceiling. Give the new preset a
name such as **Local add-on testing**. Select only these six permissions:

| Web permission choice | Permission |
| --- | --- |
| Inspect Kodi health, add-ons and UI state | `target.inspect` |
| Read and validate workspaces | `workspace.inspect` |
| Edit workspace files | `workspace.edit` |
| Deploy workspace builds | `workspace.deploy` |
| Status, prepare, Start and Stop approved managed versions | `managed.versions` |
| Launch a deployed manifest entry of the selected managed development project | `development.launch` |

The ceiling exposes choices; it does not select them. Saving a custom preset
freezes that exact set. Existing built-in and custom presets gain nothing.

Create or choose a separate local development client using that preset. Apply the
same preset to only the intended managed Kodi targets and grant that client each
exact target. Start/Stop still require `managed.versions` on both policies.
Do not grant external or production targets. Use the existing localhost MCP
connection instructions; keep its token private.

In **Development → Current Managed Kodi**, choose the project and this client,
then choose Kodi. Automatic onboarding supplies the exact source approval and
client-owned workspace. It does not select another session implicitly. Complete
any existing native/Bridge/add-on setup shown by **Check add-on testing setup**.
An administrator must explicitly select the intended project/profile; an absent,
reserved, uncertain or different selection cannot use the new launch tool.

## Develop and test

1. Use `managed_status`, `managed_start` and `managed_stop` for the approved version.
   Stop retains its profile. Each mutation uses a fresh saved request UUID; after a
   lost reply, inspect that same request rather than choosing another UUID.
2. Discover the exact owned workspace and authorized target with the existing
   workspace/target tools. Edit through `workspace_edit`, validate the current
   generation, then deploy that exact generation through `workspace_deploy`.
3. Choose an eligible ordinal from validation's `launch_entries`, then call
   `development_launch` with `request_id`, `workspace_id`, `target_id`,
   `expected_generation` and `extension_ordinal`. There is no path, add-on ID,
   command or argument input. Program scripts use their declared manifest entry.
4. An accepted receipt proves dispatch only. Use `inspect_ui_state` and
   `screenshot_capture` to assess the resulting screen. Managed screenshots need
   `target.inspect`; the legacy `target.visual` permission is not needed.

The new tool invokes the existing launch coordinator. It verifies the latest
exact installed workspace tree, add-on ID/version/enabled state, selected source,
runtime revision, container, Bridge binding, native prerequisites and operation
coordination. It retains existing rates, use tracking and original receipts.
Same-request replay can query the original resident receipt without executing
again. An uncertain launch blocks a fresh launch; review its saved evidence.

`workspace_execution_recover` retains its separate existing permission and exact
smoke-operation evidence requirements. The launch opt-in does not authorize that
recovery, broad remote control, navigation, Reset, promotion, settings writes,
external control or DVR backend API calls. Launch executes approved add-on code
with the target's existing privileges and network access; it is not an add-on
sandbox. This campaign does not run live add-on code or change backend data.

Navigation remains deferred under #285. This preset deliberately cannot call
`remote_action`, including its general navigation actions. UI inspection and
screenshots are read-only observations, not navigation acceptance.

## Keep local edits and the approved project in sync

For a checkout outside the configured development root, the least disruptive
option is an independently cloned, approved working tree beneath the existing
root. Keep all existing project directories and registered relative locations.
Do not switch the root to an unrelated directory, insert a symlink, or add a
nested bind mount. Match an existing registration's location if the add-on ID
already exists; onboarding refuses a conflicting second location.

Choose a reviewed commit and keep repository metadata private. Give the Web
account (`10001:10001` in the supported bundle) only the required read/traverse
access to the project and installable add-on. Keep the development root itself
non-writable to that account. The existing Web read/write mount and broker
read-only mount remain unchanged; patch-back writes are a separate OS permission
and human-reviewed workflow.

**Open that approved working tree as the OpenCode project.** Subsequent source
edits then happen in the directory Kodi Lab actually reads. The original checkout
stays untouched; a one-time copy is not an automatic synchronization mechanism.
If development continues in another checkout, explicitly transfer reviewed
commits into the approved tree and check its commit/diff before capture. Do not
silently overwrite local changes or treat an old copy as current source.

Host source edits and the protected workspace are also separate. For the existing
bounded import workflow, explicitly add **Scan development sources**
(`development_source.inspect`) to the new preset as needed. Use
`workspace_import_preview`, review the exact paths/digests and any private-edit
conflicts, then `workspace_import` with that preview and current generation.
Validate and deploy the returned generation. Project selection alone never
refreshes or overwrites workspace edits. See [workspace batch import](WORKSPACE-BATCH-IMPORT.md).

No working tree, permissions, root mount, production configuration or original
Hub checkout is changed by this proposal. Native acceptance must separately prove
Program launch, intended UI, inspection/screenshots and retained Start/Stop on a
reviewed runtime. Navigation acceptance belongs to the later navigation slice.

## Bounded implementation assessment

Based on main `590a86f5c74545fae284c8b79a2bcd73e5f3264d` after merging #286.

The existing workspace launch coordinator already verifies the current deployed
workspace tree, selected manifest entry, native inventory, enabled add-on,
Bridge/runtime/container binding and typed mutation lease. It owns rate limits,
request receipts, status-only replay and uncertainty fencing. Reuse that complete
coordinator through a narrow opt-in tool; retain the legacy tool and recovery
permissions unchanged. No database or built-in preset change is required.

This PR delivers the launch slice of #285. Navigation is deferred: its current
remote-action receipt binds client, target and action, but not selected workspace,
deployed generation and verified installation. Extending narrow authority must
bind that evidence and recheck it at the existing coordinated dispatch point.
It must retain remote-action replay, observation, rates and use coordination.
Do not substitute a temporary `target.control` grant for that work.


## Focused validation

FOCUSED PASS; no full suite or live Kodi was run:

| Selected test file | Passed |
| --- | ---: |
| `tests/application/test_development_launch.py` | 25 |
| `tests/web/test_development_launch.py` | 3 |
| `tests/web/test_managed_versions_mcp.py` | 10 |
| `tests/application/test_workspace_launch.py` | 19 |
| `tests/web/test_workspace_launch.py` | 4 |
| `tests/application/test_mcp_permission_subsets.py` | 3 |
| `tests/application/test_mcp_foundation.py` | 17 |
| `tests/web/test_home_agents.py` | 14 |
| **Distinct focused cases** | **95** |

Every new/materially changed test file was run individually. The existing launch,
permission-subset and Agents checks cover the affected shared paths. Ruff format,
lint and strict mypy passed for all 14 selected Python files; JavaScript syntax
and diff checks passed. Existing Starlette deprecation warnings remain.

The new application tests use a real captured Program manifest, generation-bound
validation/deployment and existing launch coordinator with fixture broker/Bridge.
They cover successful launch, same-request replay, rate limits, uncertainty and
post-claim revocation, exact grants/ownership, session selection, runtime/container,
stale generation/deployment and missing native Bridge binding. The wire fixtures
exercise closed schemas, retained Start/Stop, existing deployment authority, actual
UI observation collection and screenshot routes, and refusal of protected tools.
These are isolated fixtures, not native or production acceptance.

`tests/web/development_launch_browser.cjs` passed in Chromium at 390/1440px with
keyboard-only permission selection, no automatic opt-in, refusal under the
read-only ceiling, zero scoped Axe violations, no overflow, page errors or
administration writes. Every browser request was intercepted.

To reproduce the new focused files:

```sh
./scripts/check-focused tests/application/test_development_launch.py \
  tests/web/test_development_launch.py \
  --source src/kodi_lab/application/mcp_administration.py \
  src/kodi_lab/mcp/registry.py src/kodi_lab/mcp/authorization.py \
  src/kodi_lab/mcp/tools.py src/kodi_lab/mcp/transport.py src/kodi_lab/web/app.py
```

Set `KODI_LAB_LAUNCH_UI_EVIDENCE` to a private directory while running the Web
file to export its disposable Agents HTML. With the same evidence directory and
`KODI_LAB_BROWSER_TOOLS` pointing to existing Playwright and Axe node modules, run
`node tests/web/development_launch_browser.cjs`.
