# Phase 9 Slices 1–3 — Protected Agent Workspaces

## Boundary

Slice 1 implements a durable catalog, safe asynchronous creation, a private
immutable baseline, and bounded read-only inspection. It accepts exactly one
registered `local_directory` DevelopmentSource UUID. Sources remain read-only.
Uploaded ZIPs, repository artifacts, arbitrary directories, aliases, mounts,
binds, Git repositories/worktrees/clones, runtimes, and instances are not
eligible inputs.

No Git command executes. VCS administration directories are excluded from
source capture and rejected if injected into a mutable workspace. Slice 2 adds
bounded workspace text mutation and derived changes. Slice 3 adds only reviewed
promotion of eligible UTF-8 file additions/modifications into that registered
source. There is no Kodi integration, broker call, Docker access, MCP exposure,
or cleanup API.

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

## Slice 2 text mutation

The semantic internal services and administrator integration surface support:

- create one absent UTF-8 file beneath an existing directory;
- replace one existing single-link UTF-8 regular file;
- apply an exact full-file UTF-8 preimage-to-replacement patch;
- list derived added/modified files against the immutable baseline.

Every mutation requires workspace UUID, expected generation, and a normalized
workspace-relative path. The Slice 1 path rules remain exact. A server-derived
per-workspace file lock serializes processes; stale generation fails without a
filesystem change. Only `ready` workspaces mutate. The original
DevelopmentSource and sibling workspaces are never opened by mutation.

Text fields and the resulting file are capped at 2 MiB. Patch is deliberately
full-file rather than line/byte-offset hunks: the exact current UTF-8 text must
equal `expected_text`, then the complete `replacement_text` becomes the file.
There is no fuzz, nearest context, partial hunk, merge, Unicode offset, external
patch program, shell, or diff process. Empty replacement is valid and retains
the file.

Creates use normalized mode 0644. Replace and patch retain the existing
normalized file mode (0644 plus any already captured execute bits). Callers
cannot select mode, ownership, timestamps, ACLs, xattrs, temporary names, or
other metadata. Binary files remain listable and unchanged but cannot be read
as content or mutated through this slice.

Before every edit, the complete workspace tree is safely recaptured and must
match the durable current digest. Content is validated and fsynced in a private
same-directory temporary file. Create publishes with Linux atomic no-replace
rename; replace/patch use atomic rename. Complete post-write recapture supplies
the next digest, and one conditional transaction increments generation exactly
once and appends content-free mutation activity.

At the existing 1 GiB/50,000-entry ceiling this makes mutation cost linear and
can be noticeable for a maximum-size workspace, but it remains finite and keeps
the synchronous operation free of incremental-manifest blind spots. Slice 2
accepts the bounded rescan cost to detect injected or changed objects anywhere
in the tree; no unproven incremental shortcut is used.

An existing target receives a short-lived private preimage link outside the
mutable tree. If post-write recapture or database publication fails, the service
restores that exact inode; a created target is removed only after proving it is
the exact postimage. The old full-tree digest must then recapture exactly. If
rollback or tree agreement cannot be proven, the workspace becomes
`reconciliation_required` and rejects further edits. No state is auto-adopted.

## Derived changes and identity

Change listing fully recaptures immutable baseline and current tree, validates
both durable identities, and returns only added and modified regular files.
Each change includes relative path, kind, before/current sizes, and at most
16 KiB of UTF-8 before/after preview per side with explicit truncation. Unchanged
files, including binary assets, are omitted and binary content is never exposed.
Missing baseline files, added directories, type changes, changed binary files,
unsafe objects, VCS administration directories, or external digest drift are
integrity failures rather than normal promotable changes.

A syntactically valid root `addon.xml` may change its add-on ID for workspace
experimentation. Durable source identity never changes. Change output classifies
the workspace `identity_changed`; later promotion must block it.

Migration `0030_agent_workspace_mutations` records only event/workspace UUID,
published generation, create/replace/patch kind, normalized relative path, and
time. It stores no content, preview, digest, host path, source locator, inode,
ownership, or raw error.

## Slice 3 plan and exact three-way review

A plan binds one ready workspace, its immutable baseline, exact current
generation/tree, a fresh complete source capture, immutable add-on ID, and one
administrator-selected eligible ManagedInstance. Target eligibility requires a
running/ready disposable instance, the source attachment, exact same-runtime and
same-container retained verified deployment evidence, and no reconciliation or
mutation blocker. These are future Slice 4 preconditions; planning invokes no
runtime or Kodi operation.

Only added and byte-modified strict UTF-8 regular files are candidates. A
modification is clean only when source type, bytes, and normalized executable
mode still equal baseline. An addition is clean only when absent from source and
all parents are safe existing directories. Delete, rename, mkdir, binary
mutation, type transition, mode-only transition, and whole-tree replacement
are rejected. Safe unrelated source modification/addition/deletion remains
allowed; any unsafe source object blocks the whole plan. Effective promoted
`addon.xml` must retain registered ID, while valid name/version/provider,
extension, dependency, and service metadata edits remain eligible without XML
merge.

Review retains complete before/after text privately for at most 100 changed
files, with the existing 2 MiB per-file text bound, a 4 MiB aggregate review
bound, and an 8 MiB encoded response bound. `promotion_review_too_large` leaves
the workspace valid. Plans become reviewable for 15 minutes and persist exact
pre/post identities, classifications, target preconditions, source capture,
and expiration/state. Conflicts are visible by relative path but cannot be
approved. No truncated content authorizes execution.

## Slice 3 permission, backup, apply, and rollback

The registered source is promotion-enabled only when the effective non-root
application account can actually write/search its root and all affected
parents. Readable-only sources are classified separately and fail closed.
Registration does not chmod, chown, or grant authority. There is no root helper,
broker, Git, shell, Docker, or Kodi fallback.

Execution repeats workspace and complete source validation. It accepts no
caller paths: source root and lexical affected path set come from the plan.
Before first mutation it publishes and verifies a private UUID-derived backup
containing exact modified-file preimages/modes and absent markers for additions.
The durable application marker then consumes approval. Descriptor-relative
`O_NOFOLLOW` traversal creates no parents. Adds require Linux
`renameat2(RENAME_NOREPLACE)`; modifications require `RENAME_EXCHANGE` and
exact displaced-preimage verification. Unsupported filesystems fail closed.

The durable content-free journal records not-attempted, applied, rolled-back,
or uncertain per path. Failure after mutation rolls back in reverse order only
while exact operation postimages remain. Added-file removal is confined to this
operation-bound rollback and requires prior absence plus the exact created
postimage. Complete recapture must prove original affected state. Exact rollback
is an ordinary failed operation and leaves workspace ready; uncertainty makes
workspace and operation reconciliation-required. An interrupted prior-process
operation is never retried or inferred successful from source appearance.

## Slice 3 success and evidence boundary

Before source deployment confidence is staled, success stores one private exact
handoff bound to promotion, selected target, source, runtime/container, prior
deployment operation, and prior source/destination evidence. It cannot authorize
another target or arbitrary copy/sync and is not public current-confidence
evidence. Every deployment-confidence row for the promoted source becomes
stale. Historical deployment, enablement, and smoke operations remain unchanged
but do not prove the current source tree.

The workspace becomes durably `promoted`, remains retained, and can no longer
create/replace/patch or promote again. Only ready workspaces mutate; creating,
promoted, abandoned, and reconciliation-required states reject edits. Planning
and execution use per-workspace exclusion, per-source promotion ownership, the
selected target's mutation lease, and global-heavy ownership in fixed order.
This conflicts with workspace mutation, same-source promotion, copy/sync, and
selected-target attachment/lifecycle/reassignment/deletion work.

The authenticated Web workflow creates a plan with a strict empty body, shows
the complete exact review, and accepts only immediate `{"confirmed": true}`.
It exposes logical names, safe semantic UUIDs, add-on/version metadata, relative
changed paths, complete review text, bounds, and rollback/staging notices. It
does not expose absolute/storage/backup paths, internal hashes, container IDs,
filesystem metadata, or raw exceptions.

**Kodi verification occurs in the next bounded stage and a later Kodi
validation failure will not automatically undo a successful source promotion.**

Phase 9 canonical patch-back remains incomplete until Slice 4 implements
D-0209 post-apply Kodi stopped copy/sync, start, inventory/provenance
verification, and approved diagnostics.

## Remaining Phase 9 work

Canonical read, directory inspection, bounded text write/create/replace, exact
patch editing, and derived workspace change review are implemented.
Conflict-aware reviewed promotion, exact source backup/application, promoted
state, evidence staling, and the private selected-target handoff are implemented.
Slice 4 Kodi copy/sync/reload/verification and diagnostics, MCP exposure, and
workspace cleanup/retention mutation remain later work. Delete, rename, move,
mkdir, binary write, type transition, shell, and Git remain absent by design.
