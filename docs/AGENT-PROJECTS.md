# Agent-owned projects

A client with Trusted Managed Lab may call `project_create` with a saved
`request_id`, `addon_id`, `display_name`, optional `version` (default `0.1.0`)
and `kind` (`program` by default, or `video`). The result contains a stable
`project_id` and the existing owned workspace allocation. No Web source
registration, workspace selection or development bind mount is required.

The initial add-on is inert: a Program logs one message; a video plugin produces
an empty directory. Both declare Python 3. Use `workspace_status` to wait for
capture, then the existing `workspace_read`, `workspace_edit`, `workspace_tree`
and `workspace_validate` tools. Edits use the exact expected generation.
Deployment/launch still obey their existing technical and target prerequisites;
project creation itself neither installs nor starts Kodi.

Use `project_status` to list only your projects, or pass `project_id` for one.
It reports saved request IDs, source intake state and owned workspace IDs without
filesystem or native probes. `reserved` means intake has not published its seed;
`seeded` describes source publication, not workspace readiness or native execution.
An interrupted allocation uses the original `project_create` request. Do not mint
a new request to work around uncertainty. Unexpected seed bytes, symlinks or
ownership changes fail closed for review.

Identical replay reuses the original project/workspace and preserves edits,
including after the bootstrap template changes. Reusing a request UUID with
different arguments fails. Existing add-on IDs require explicit disambiguation;
intake never overwrites another registration. Source authority alone does not
transfer another client's private project seed. Workspace ownership remains exact.

The bootstrap is bounded to four project reservations per client and 32 globally,
and existing workspace/global-heavy quotas also apply. Records and private seeds
remain retained for replay and recovery. The private seed is only an input to the
existing workspace storage and generation/digest machinery.

This first slice supports new projects. Reviewed checkout/package transfer,
validated ZIP intake and retirement are subsequent #305 slices. Native and
browser acceptance remain tracked by #310. No host path, shell, Git clone, remote
URL fetch, Docker operation or external Kodi authority is provided by these tools.

For agent-owned projects, `workspace_edit` with `operation=create` also creates
missing safe parent directories in the private workspace. A nested resource file
and its parents publish as one generation. No-follow traversal and existing path,
text and tree bounds apply. Failed publication rolls back the known file and only
unchanged empty parents created by that operation. Unexpected bytes or replaced
parents are retained for reconciliation. Original source seeds are never edited.
Registered-source workspaces retain their existing parent-directory rule.

Private project seeds are immutable intake, so validation reports
`promotable_classification=agent_owned_project` and `promotable=false` for source
promotion. This does not prohibit managed deployment. `promotion_request` cannot
write a private seed or turn project ownership into host filesystem authority.

Trusted project owners can select an installed managed target on each
`workspace_deploy`, `development_launch`, `workspace_launch` or execution request.
The workspace identifies the project; `expected_generation` binds its exact
bytes, and `target_id` selects the prepared version returned by managed status.
These operations neither require nor change the human development selection.
Use existing `managed_stop` and `managed_start` to switch versions; absent
versions still require human installation through Managed Kodi.

The first private-project deployment creates its internal source association.
It requires a complete fresh inventory with no existing add-on of that ID;
the stopped broker copy independently refuses an existing destination without
matching copy evidence. Later copies use only the same owner's workspace and
exact runtime/container evidence. A reset profile requires a fresh deployment.
Capacity, human input, holds, pending operations and uncertain outcomes retain
their existing guards. An uncertain human selection remains a review blocker.
Fixture/source validation does not establish native cross-version acceptance.
