# Agent-owned projects

For unattended managed installation and updates, use the
[validated source-deployment loop](MANAGED-ADDON-INSTALLATION.md). Repository
`InstallAddon(id)` requires confirmation and cannot pin a reviewed package.

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
introduced. For larger editable sources, use the bounded `project_transfer` route below.
Original native ZIP intake retains its separate inline limit.
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

When Kodi discovers a newly copied owned Program or video project disabled,
managed workspace deployment enables that exact add-on before reporting success.
It verifies the copied tree, native identity and dependencies, and rechecks the
current credential and role under the original deployment lease. This does not
enable service add-ons or change external-device policy. A lost enablement
response retains the original deployment fence and requires explicit stopped-tree
inspection; replay does not send another enable request.

For a failed first deployment, an owned project has no registered-source baseline.
Advanced stopped-tree recovery can still verify the original container and the
complete requested tree from that deployment receipt. It records containment,
keeps the failed receipt, and permits a separately requested deployment. Unknown
bytes remain fenced; inspection never copies files, starts Kodi or enables an add-on.

Manifest-selected Program launch executes the exact declared library through
`RunScript(file)`. Kodi does not populate implicit add-on identity in that mode.
Scripts that need their metadata or resource path should use
`xbmcaddon.Addon("their.addon.id")`; a bare `xbmcaddon.Addon()` can fail. The inert
custom-window fixture uses its explicit ID. Launch receipt acceptance still
requires subsequent log/UI inspection to establish that the script opened.

## Larger source transfer

Use `project_transfer` to stage a reviewed ZIP, then `project_import` to publish
it as an owned project. This is the source import method for larger checkouts;
Lab never clones a repository or reads a caller-selected server path.

1. Package one add-on directory whose name equals its `addon.xml` ID. Exclude
   version-control metadata, symlinks and native binaries. Retain the reviewed
   repository commit and SHA-256 of the ZIP bytes.
2. Save a transfer request UUID. Call `project_transfer` with that `request_id`,
   `archive_sha256`, `archive_size`, `offset` and `chunk_base64`. Send ordered
   **32,768-byte decoded chunks**, with a shorter final chunk. Every call remains
   within the existing 64 KiB MCP request limit. Retain returned `next_offset`.
3. After response loss, retry the identical chunk or use `action: "status"`
   with the same UUID, size and digest. Conflicting bytes/metadata are refused.
   If status reaches the total size while still receiving after interruption,
   replay the final chunk to finish verification. A retained partial temporary
   file or changed storage is fenced for review, never overwritten.
4. When `state: "ready"`, call `project_import` with its own saved request UUID,
   `transfer_id` equal to the transfer UUID, `archive_sha256`, `intake_kind`
   and `provenance_reference`; omit `archive_base64`. Continue with the returned
   workspace's normal capture, edit, validation and deployment workflow.
5. `action: "cancel"` removes only that transfer's exact staged chunks and
   retains its tombstone. It never deletes an imported project. Imports already
   published replay after cancellation/expiry and preserve workspace edits.

Staged ZIP bounds: **4 MiB compressed, 16 MiB expanded, 1,024 entries,
2 MiB per file**. Existing inline limits remain 45 KiB/256 entries/4 MiB/1 MiB.
The same ZIP structure, CRC, path, duplicate, special-file, compression-ratio,
manifest and source-type protections apply. Entry counts are checked before
central-directory objects are allocated. Expanded validation/publication is
serialized to bound process memory; existing workspace capture remains separate.

There are four retained transfer receipts per client and 32 globally, including
cancelled/failed/expired receipts. They are bounded reservations, consistent with
retained project quotas, not reusable general upload storage. Incomplete/ready
transfers expire after 24 hours; status/write observes expiry and discards exact
chunks. Cancellation immediately releases staged bytes. At most 128 MiB of ZIP
chunks can be retained globally; intake preserves the 2 GiB free-space reserve.
Project seeds can add at most 512 MiB across the 32 project slots before existing
workspace copies; those remain subject to their independent quotas. Supported
application backup/recovery retains transfer receipts, chunks and private modes.
Source text beyond the existing 2 MiB workspace read/edit limit is refused.
