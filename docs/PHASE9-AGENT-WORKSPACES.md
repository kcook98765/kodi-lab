# Phase 9 Slice 1 — Protected Agent Workspaces

## Boundary

Slice 1 implements a durable catalog, safe asynchronous creation, a private
immutable baseline, and bounded read-only inspection. It accepts exactly one
registered `local_directory` DevelopmentSource UUID. Sources remain read-only.
Uploaded ZIPs, repository artifacts, arbitrary directories, aliases, mounts,
binds, Git repositories/worktrees/clones, runtimes, and instances are not
eligible inputs.

No Git command executes. VCS administration directories are excluded by the
shared capture policy. There is no file mutation, diff/promotion, source
mutation, Kodi integration, broker call, Docker access, MCP exposure, or
cleanup API.

## Durable model and layout

`AgentWorkspace` stores only logical identity and provenance: workspace/source
UUID, immutable captured source identity snapshots, state, baseline artifact
UUID and digest, generation, current-tree digest, and timestamps. It stores no
absolute path. States are `creating`, `ready`, `abandoned`, and
`reconciliation_required`; generation begins at zero.

The server derives this private layout:

```text
KODI_LAB_DATA_ROOT/agent-workspaces/<workspace-uuid>/
  baseline/
    manifest.json
    tree/
  tree/
```

Temporary creation staging is a UUID-derived sibling and is never public.
Every workspace, including two from the same source, has separate bytes and a
separate baseline identity. The invoking non-root application user owns them.

## Capture and interruption

Creation reuses the Phase 8 descriptor-relative `O_NOFOLLOW` capture, normalized
modes, VCS exclusions, two-pass drift detection, and canonical bounds. The
captured root `addon.xml` must parse and retain the registered add-on ID. The
first normalized capture is published as baseline and is the sole source of the
mutable generation-0 tree. Recapture proves exact baseline/tree topology,
bytes, modes, and absence of extras before the database becomes ready.

A durable typed operation owns a global-heavy lease. Before baseline
publication, exact partial staging/workspace bytes are removed and state becomes
failed/abandoned. After baseline publication, uncertainty is never adopted or
silently deleted: workspace and operation become `reconciliation_required`.

## Read and inspect policy

The internal service and authenticated administrator API accept only workspace
UUID plus a normalized POSIX-style relative path. Empty path is accepted only
for root-directory inspection. Absolute paths, dot/parent traversal, backslash,
NUL/control characters, overlong components/paths, and excessive depth are
rejected. Every component is opened descriptor-relatively without following
links. Reads re-check regular-file identity; directory inspection rejects any
link, hardlink, socket, device, or FIFO observed after creation.

Directory results contain at most 2,000 entries and only relative name,
file/directory type, and regular-file size. File content is strict UTF-8 text
only and at most 2 MiB per request. Binary content reads are deferred; binary
files can still be listed by safe metadata. DTOs and errors contain no host
paths, source locator, data root, baseline path/digest, manifest, inode,
ownership, timestamps, link target, or raw exception.

The generation-zero tree digest is creation evidence, not a permanent trust
claim. Read/inspect always traverses current bytes safely, and every future
diff or promotion must perform a complete fresh bounded recapture.

## Remaining Phase 9 work

Canonical read and directory inspection inside a Lab-managed working copy are
implemented. Bounded write/create/replace and patch editing remain unimplemented.
Conflict-aware reviewed promotion to the original DevelopmentSource remains
unimplemented. Kodi copy/sync/reload/verification, diagnostics, MCP exposure,
and cleanup mutation remain later work. Git remains absent by design.
