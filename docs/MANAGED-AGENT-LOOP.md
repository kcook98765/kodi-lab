# Managed add-on development: agent quick start

Use this loop for an existing authorized **managed disposable development**
target, registered source and owned workspace. Discover the tools on the
installed server first: a source checkout upgrade does not upgrade the deployed
server, target Bridge, custom presets or existing grants.
The Web administrator can use the selected-session preparation checklist to copy
the explicitly attached registered-source baseline into stopped Kodi. Saved copy
requests recover lost replies. This does not deploy private workspace edits,
enable/start the add-on, or give an agent new permissions. Follow the
[development authority model](PHASE12-MCP-DEVELOPMENT.md#authority-model) and
[manifest launch prerequisites](MANIFEST-ENTRY-LAUNCH.md).

1. **Resume the current checkpoint.** Use `target_status` and `workspace_status`
   to confirm the exact target/source/workspace and current generation. Recover
   any unresolved original request before making another mutation. Reuse
   still-valid validation and deployment evidence for unchanged bytes; check
   its generation and target/runtime identity rather than assuming it is current.
   Starting an existing provisioned target through
   [managed ensure-ready](MANAGED-KODI-RESOURCES.md) requires its separate opt-in
   authority. Release a bounded managed-use hold when finished.
2. **Stage reviewed changes once.** Synchronize reviewed local changes into the
   registered source mirror through the project's normal workflow. Call
   `workspace_import_preview` with the current `expected_generation`; review all
   paths, byte sizes, hashes, advertised limits and rejections. Then call
   `workspace_import` with that exact generation, `source_digest`,
   `preview_digest` and a fresh request UUID. Import needs opt-in
   `development_source.inspect` plus `workspace.edit`, the exact source grant
   and workspace binding; existing presets do not gain access automatically.
   Default changes mode rejects private-edit overlap. For explicit removals or
   conflicts, use the installed server's `reviewed_tree` preview/apply mode with
   exact per-path choices, preimages and resolved digests. `matches_source: true`
   establishes an exact source tree; retained private choices are a reviewed
   merge. Preserve private edits and use `workspace_edit` when import is
   unsuitable. See [batch import](WORKSPACE-BATCH-IMPORT.md).
3. **Check provenance from receipts.** Compare the returned per-file SHA-256
   hashes with the reviewed source hashes and retain the returned generation
   and tree digest. A routine complete-file readback adds no provenance when
   these receipts already establish the required byte identity. Read content
   when review, debugging or uncertainty requires it.
4. **Validate, deploy and launch the same generation.** Call
   `workspace_validate`, then `workspace_deploy_preflight` for the current exact
   generation and authorized target. Resolve stored blockers through the stated
   agent or administrator path. Preflight remains `blocked` or `unknown`; live
   native/Bridge/add-on readiness and exact container proof remain separate.
   `workspace_deploy` performs its mandatory mutation-time checks for that same
   generation and target. A `verified_existing` result with
   `deployment_work_skipped: true` is a fresh verification of an existing
   deployment, with zero copy/restart work; it does not execute code. Retain
   its generation and reused deployment identity. See
   [verified no-op eligibility and receipts](VERIFIED-NOOP-DEPLOYMENT.md).
   Select an eligible `launch_entries`
   ordinal and use `workspace_launch` when the installed server and reviewed
   Bridge support it. Launch acceptance establishes dispatch, not the intended
   window, completed execution or functional acceptance: inspect the UI and
   test the feature. `workspace_execute` remains a bounded video-plugin smoke
   test, not a generic script or mixed-manifest launcher. See
   [entry-point launch and uncertainty](MANIFEST-ENTRY-LAUNCH.md).
5. **Observe navigation without duplicating it.** When available and authorized,
   use `remote_action(observe_ui=true)` to receive the action's retained UI
   observation. For visual evidence, retrieve an available exact action capture
   with `screenshot_retrieve` using its original request and capture IDs, instead
   of asking for another capture. Inspection and visual access each require
   their own current permissions and exact grant. Capture reuse covers managed
   development only; images expire after two minutes and may be replaced sooner,
   and old capture IDs never select newer pixels. Retrieve does not recapture.
   See [action observations and capture reuse](REMOTE-ACTION-OBSERVATIONS.md).
6. **Keep a small checkpoint and full evidence.** Retain complete receipts and
   warnings in the normal evidence store. Carry only the current target, source,
   workspace and request IDs; current and confirmed deployed generations;
   relevant validation/deployment/launch/capture receipts; unresolved outcomes;
   verified launch route; and the next functional or visual test. After a lost
   response, recover the original request with its original UUID and parameters.
   Do not replay an uncertain mutation under a new UUID. A receipt that remains
   ambiguous needs its documented reconciliation path, not a guessed success.


## With the single disposable development session

The Web administrator selects the persistent workspace and an approved cached
Kodi version, then completes the preparation checklist and existing reviewed
source/native/Bridge setup. The source grant, workspace binding and exact target
grant must already belong to the acting client; session selection creates none
of them. The checklist explicitly does not assess an agent's authority. Discover
the installed server's tools and schemas before using the loop above.

When that policy is active, fresh managed workspace deploy/execute/GUI launch
must match the selected bound workspace/source/runtime/target and observed
container. A reserved, uncertain or pending runtime needs review. Offline edits
and exact historical receipt replay remain available through their ordinary
authorization paths. An old receipt does not establish the successor profile's
baseline, trust, installed bytes or live readiness.

Use existing separately granted `managed_ensure_ready` and `managed_release` only
for the exact authorized target. Release ends the bounded use hold; it is not a
Fresh Test or version switch and does not remove a profile. Fresh Test/Switch is
an explicit Web administrator review with selected retained diagnostics and final
data-loss consent. Unknown create/removal remains fenced; do not choose another
version or a fresh request UUID to escape an uncertain original operation.
