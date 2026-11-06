# Managed add-on development: agent quick start

The intended version-slot workflow is **Status → Start(version) → develop/test →
Reset(version) or Stop(version)**. **Stop keeps the exact Kodi profile; Reset
discards its writable settings/add-ons and prepares a clean replacement.** Source,
workspace and Git state outside Kodi are independent. Reset must have separate,
explicit destructive authority; ordinary lifecycle permission does not grant it.

Human administrators use the [four-version Kodi page](MANAGED-KODI-WEB.md) for
Start, screen viewing, retained Stop and confirmed clean Reset. These actions do
not grant agent or workspace authority. After Reset, use **Set up for add-on
testing** to establish fresh Bridge and deployment evidence.

Issue #238 is [being delivered in stages](MANAGED-VERSION-LIFECYCLE.md). The
version-oriented MCP loop is now available after explicit administrator opt-in:

1. Call `managed_status({})` to discover the supported versions and authorized
   slot state. No raw target/container/revision is needed for lifecycle actions.
2. Persist a fresh request UUID, then call
   `managed_start({version: "omega", request_id: "<UUID>"})`. It prepares an empty
   approved cached slot or resumes the same stopped profile. Poll with the
   original UUID after a lost response or while readiness is checking. Continue
   native work only when `status.native_ready` is true. Bridge and workspace
   readiness are separate; Start grants neither.
3. Develop/test through the separately authorized workspace loop below.
4. Persist another UUID and call
   `managed_stop({version: "omega", request_id: "<UUID>"})`. Confirm
   `stop_confirmed`; a retained profile keeps settings and installed add-ons.
   Another user's viewer, input, hold, active use or mutation blocks Stop.

Use `matrix`, `nexus`, `omega` or `piers`. Both the custom client preset and exact
canonical target policy need the new `managed.versions` permission plus the
client's exact target grant. Existing presets and `managed.lifecycle` do not
automatically opt in. The administrator must prepare an approved cached runtime;
Start never downloads, approves, repins or silently adopts an old profile.

To switch versions, safely Stop the current version first with its own grant.
Automatic switching remains a separate phase. For a clean Kodi environment,
explicitly authorized agents can call
`managed_reset({version: "omega", request_id: "<new UUID>", restart: false})`.
This deliberately replaces only Kodi's writable profile from its existing
approved cached image, preserving independent source/workspace/Git/evidence.
It also requires separate `managed.reset` authority in both client and target
policies. Default Reset leaves the new profile stopped; `restart: true` requests
native Start. Bridge pairing, profile-bound deployment evidence and any disposed
Current Kodi selection are invalidated; complete their existing preparation
before fresh deployment. See [clean Reset and recovery](MANAGED-CLEAN-RESET.md).
Reset of the selected Current Kodi closes that selection. The existing MCP
selection tools cannot bootstrap it again: an administrator must select the
replacement/workspace while all Kodi are stopped, copy a fresh source baseline,
prepare reviewed Bridge trust and enable the add-on. The administrator closes
viewers and releases their own Web keep-awake/recent use (zero), or Stops before
the agent begins; this does not clear an agent hold or prove Stop by itself.
Existing grants survive but
do not supply these preparation powers. Then resume the authorized workspace
validate/deploy/launch loop. See the [exact post-Reset sequence and reasons](ISSUE-238-INTEGRATED-REVIEW.md#development-after-reset).

Never replace an uncertain request with a new UUID.
See [version lifecycle recovery and limits](MANAGED-VERSION-LIFECYCLE.md).

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
   authority. Use `managed_stop` when explicitly granted actual Stop authority; legacy
   `managed_release` clears use but does not prove Stop.
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
