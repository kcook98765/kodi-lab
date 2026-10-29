# Select a retained Kodi version

Requires the separately installed retained-profile policy and schema
`0089_retained_development`. Existing installations keep the disposable workflow
until an administrator explicitly opts in. This does not acquire Piers RC1 or
waive its exact reviewed prerequisite/acknowledgement gates.

In Web, open **Current Kodi → More options → Retained Kodi versions**. Stop the
current Kodi, release active use holds, and close/resolve viewers and unfinished
operations. Choose which reviewed Omega identity to retain and enable retained
versions. Existing selection, profile, workspace, trust and grants stay intact.
Prepare missing approved instances through the ordinary administrator lifecycle;
retained mode permits four provisioned stopped profiles but starts only the
selected exact instance. No profile is reset or retired during selection.

Choose a ready add-on workspace and Kodi version, review the selection, then
confirm. The HTML workflow needs no JavaScript. The preview binds exact target,
pinned runtime and container-or-absence identity and latest session generation.
A stale preview refuses; inspect current state before making a new request. An
absent selected profile uses ordinary Prepare; a retained stopped profile uses
Resume. Each target needs its own native readiness, Bridge trust and source
baseline/deployment receipts. Selection does not copy that evidence or grants.

For an agent, an administrator must first select its owned workspace. Selection
requires explicit `managed.lifecycle` **and** `workspace.inspect` in the existing
client/target capability intersection, plus the exact target grant, source grant,
and owned workspace binding. No built-in preset gains lifecycle permission.
The agent can switch versions within that currently selected workspace. Changing
the workspace or initializing selection remains an administrator action. A local
OpenCode tool allowlist must also admit the new tools after application upgrade;
tool visibility alone does not grant authority.

1. Discover the granted target/workspace IDs through existing inspection tools.
2. Finish the current test. Call `managed_release` with its genuine request UUID;
   release records relinquished use and may retain Kodi while playback/another
   hold/idle policy protects it. Verify stopped status before switching. Use Web
   **Stop** if needed; MCP release is not an unconditional stop command.
3. Call `managed_selection_preview(target_id, workspace_id)`.
4. Call `managed_select` with a new request UUID, exact target/workspace, preview
   `identity`, and integer `expected_generation=latest_generation`. Selection
   keeps all profiles and never starts Kodi. Replaying the same request returns
   its historical receipt, possibly closed, without restoring selection. Recheck
   status; a historical result with `selected=false` is not current admission.
5. Call `managed_ensure_ready` for the selected provisioned target. Inspect exact
   version/native readiness, then validate/deploy the actual workspace generation,
   launch through its supported route, and inspect behavior/UI/logs/screenshots.
   Static validation and dispatch do not prove execution. Reuse the same container
   for edits; deployment remains the supported stopped-copy/restart workflow.
6. Release the use hold, verify stopped state and repeat for another version.

API administration uses authenticated GET/POST `/api/development-session/policy`,
GET `/api/development-session/selection-preview`, and POST
`/api/development-session/select`. Mutations retain Origin/CSRF enforcement and
closed inputs. MCP returns an opaque preview identity and target/workspace/source
selection receipt; it does not disclose host paths, container IDs or image IDs.
Revocation is checked on previews, mutations and historical replay.

Pending or uncertain operations, start/provision reservations, input-worker
ownership, unexpired use, stale identities or reconciliation requirements refuse
selection. Preserve original operation/request IDs and use supported explicit
recovery; do not create new IDs to hide unknown results. Selection has a bounded
five-second per-actor interval and the existing 256-generation history cap. It
never clears uncertainty or prunes protected history to make room.

Returning to the disposable workflow refuses inventory that violates its original
single-profile policy. That action never deletes retained profiles. Do not disable
retained mode as ordinary cleanup. Profile retirement remains a separate explicit
administrator decision.
