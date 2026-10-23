# Managed add-on development: agent quick start

Use this loop for an existing authorized **managed disposable development**
target, registered source and owned workspace. Discover the tools on the
installed server first: a source checkout upgrade does not upgrade the deployed
server, target Bridge, custom presets or existing grants. Follow the
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
   Private-edit overlap rejects the batch. Preserve those edits and use the
   explicit `workspace_edit` fallback when import is unsuitable; never discard
   private edits to force import. See [batch import](WORKSPACE-BATCH-IMPORT.md).
3. **Check provenance from receipts.** Compare the returned per-file SHA-256
   hashes with the reviewed source hashes and retain the returned generation
   and tree digest. A routine complete-file readback adds no provenance when
   these receipts already establish the required byte identity. Read content
   when review, debugging or uncertainty requires it.
4. **Validate, deploy and launch the same generation.** Call
   `workspace_validate`, then `workspace_deploy` for the returned exact
   generation and authorized target. Select an eligible `launch_entries`
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
