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

`project_import` accepts an agent-reviewed checkout packaged as a source ZIP, or
an existing source ZIP. Send base64 archive bytes, their SHA-256, `intake_kind`
(`reviewed_checkout` or `source_zip`), a provenance reference (for example a
repository commit), and a saved request UUID. The reference records the agent's
review claim; Lab validates the transferred bytes and does not fetch that reference.
`project_status` retains the package digest, reference, kind, file count and bytes.
Provenance is immutable and protected from a lossy schema downgrade.

Inline packages are bounded to 45 KiB compressed, 256 entries, 4 MiB expanded,
and 1 MiB per file. They must contain one add-on ID directory, one eligible Python
Program/plugin entry, UTF-8 source text and supported image/font resources.
Extra dependencies, service/binary/repository add-ons and unsupported member types
are refused with explicit codes. ZIP structure, paths, duplicates, symlinks, CRC
and compressed stream validation reuse the existing ZIP validator. Source seeds
publish with private read-only files and directories; workspace capture supplies
the editable copy. Empty archive directories and executable permission bits are
not imported as source files.

Editable source import does not perform native ZIP installation. For separate
test-mode intake, use `package_import` with original ZIP bytes, SHA-256, provenance
and a saved request UUID, then `package_status`. It accepts bounded Python video
plugins or passive reviewed repository descriptors through the existing immutable
artifact validator. Python test packages have the same dependency/file-type
restrictions as editable imports; Program source import remains supported through
`project_import`, while Program native ZIP testing is unsupported. Intake has
four reservations per agent and 32 globally, including failed/interrupted records.
An interrupted `reserved` package requires inspection of the original request;
replay reports it without replacing bytes or allocating another artifact.

`package_install` requests the existing native ZIP delivery for your accepted
artifact and an installed managed target. This requires the administrator's
explicit native delivery origin configuration; it neither starts Kodi nor performs
installation. Kodi's GUI consumes its exact expiring `source_url` using Install
from zip. `package_install_status` reads the owned receipt; `verify=true` checks
exact native payload, version/type and enabled state. A URL, download or GUI
notification alone is not installation success. See
[native ZIP testing](NATIVE-ZIP-INSTALLATION.md) for the reviewed route and its
operator-assisted limitations. No generic builtin or direct-path installer is
introduced. Larger/chunked transfers remain unsupported by this bounded inline
protocol.
Native and browser acceptance remain tracked by #310. No host path, shell, Git
clone, remote URL fetch, Docker operation or external Kodi authority is provided.

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
