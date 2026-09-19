# Phase 9 Slices 1–5 — Protected Agent Workspaces

## Boundary

Slice 1 implements a durable catalog, safe asynchronous creation, a private
immutable baseline, and bounded read-only inspection. It accepts exactly one
registered `local_directory` DevelopmentSource UUID. Registration and ordinary
workspace use grant read trust only; promotion is the separately permissioned
exception described below.
Uploaded ZIPs, repository artifacts, arbitrary directories, aliases, mounts,
binds, Git repositories/worktrees/clones, runtimes, and instances are not
eligible inputs.

No Git command executes. VCS administration directories are excluded from
source capture and rejected if injected into a mutable workspace. Slice 2 adds
bounded workspace text mutation and derived changes. Slice 3 adds only reviewed
promotion of eligible UTF-8 file additions/modifications into that registered
source. Slices 1–4 add no cleanup API; Slice 5 adds only UUID-specific
application cleanup. Cleanup has no broker, Docker, Kodi, Git, root, shell, or
MCP authority.

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

Any DevelopmentSource directory provisioned writable for promotion becomes part
of the Kodi Lab application trusted-computing base. Promotion requires separate
administrator OS permission provisioning; Kodi Lab does not `chmod` or `chown`
source trees. Compromise of the application service account could modify every
promotion-enabled source writable by that account. Administrators must use
least-privilege ownership or ACLs and enable promotion only where intended.

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

## Slice 4 promotion-bound Kodi verification

Source terminal success, handoff publication, verification-child creation, and
reservation of its one selected-target handoff to the exact verification UUID
commit in one database transaction. Fallible drift and handoff preflight begins
only after that durable publication. The promotion service requires its
publication collaborator at construction, and verification dispatch can only
execute or recover an existing durable child; it cannot repair success by
creating one later. The handoff remains
unconsumed through preflight, cursor, stop, and sync. Only exact sync readback
and fresh ordinary deployment evidence atomically set both consumption markers;
deterministic earlier failure and ambiguity remain reserved and non-reusable. A
durable verification operation captures a fixed Kodi-log cursor, stops the
exact container, reuses the low-level changed/new-only sync core, publishes new
verified deployment evidence after exact readback, starts the same container,
waits for readiness, checks promoted ID/version/type and enabled preservation,
then classifies only a bounded post-cursor log window. No second broad approval,
generic target selector, recreate, enable repair, shell, generic JSON-RPC, or
caller path/offset/limit exists.

Sync, reload, inventory, and log results are distinct. Target contradiction or
target-correlated startup errors fail; correlation/observation gaps are
incomplete; mutation uncertainty is reconciliation-required. All outcomes
retain the already succeeded promotion and immutable promoted workspace.
Web status explicitly separates Source promotion from Kodi verification and
returns only logical identity, versions, enabled Booleans, classifications,
counts, and bounded sanitized snippets. The complete attributable buffer is
bounded to 1 MiB, decoded, and subjected to deterministic obvious-secret
redaction before control normalization, the 40 KiB retained-evidence cut, and
snippet truncation. It covers
Bearer credentials, common token/password assignments, credential-like URL
userinfo, and obvious common token prefixes; it is not a general DLP guarantee.
Raw log bytes remain transient inside the fixed private broker boundary and are
not persisted. Only redacted snippets enter evidence, DTOs, or Web history.

## Slice 5 abandonment, cleanup, and conservative retention

Canonical read, directory inspection, bounded text write/create/replace, exact
patch editing, and derived workspace change review are implemented.
Conflict-aware reviewed promotion, exact source backup/application, promoted
state, evidence staling, and the private selected-target handoff are implemented.
Promotion-bound Kodi copy/sync/reload/verification and bounded diagnostics are
implemented, so canonical patch-back is complete.

A ready workspace may be explicitly abandoned only with no active promotion
plan or source operation and no reconciliation blocker. Abandonment is
metadata-only: it makes the workspace immutable and retains its baseline and
tree until a separate confirmed cleanup. Promoted workspaces are already
immutable and cannot be abandoned. Lifecycle is independent from byte state, so
successful cleanup leaves the lifecycle `abandoned` or `promoted` and changes
only durable byte state from `present` to `cleaned`.

Cleanup accepts one workspace UUID and `{"confirmed": true}`. It requires an
abandoned workspace, or a promoted workspace with terminal source promotion and
terminal verification. Creating, ready, active-plan, queued/running promotion,
queued/running verification, and every reconciliation-required condition block.
A typed global-heavy operation records queued/running/terminal state and
validating/deleting/verifying/finalize phase.

The server derives the exact UUID subtree and persists its no-follow device and
inode before the destructive marker. Descriptor-relative deletion never follows
links and never crosses a device boundary. Because the subtree is wholly
Lab-owned, injected symlinks, hardlinks, sockets, FIFOs, and devices are unlinked
as directory entries; their targets are never opened. Baseline, editable tree,
manifest, and workspace-private transient entries are removed together. The
storage parent, sibling workspaces, DevelopmentSource, managed Kodi instances,
deployment evidence, and canonical source are untouched.

Failure before the deletion marker is ordinary. Once marked, only the same
operation may resume the idempotent goal of exact subtree absence. Missing bytes
alone are not authoritative without that operation evidence (except a proven
failed pre-publication creation). Identity contradiction or uncertain partial
state becomes reconciliation-required. A completed cleanup request is
idempotent and never recreates or deletes again.

List/detail retain logical provenance, add-on identity, creation/final
generation, lifecycle, byte state, and promoted/abandoned/cleaned timestamps.
Mutation and lifecycle activity, promotion plan/source operation/journal,
handoff, verification, private source backup, and bounded log evidence remain.
Tree inspection, reads, edits, diffs, and promotion after cleanup return
`workspace_bytes_cleaned`; no host path is exposed.

V1 cleanup is manual and one UUID at a time. There is no TTL, timer, background
worker, disk-pressure policy, automatic post-promotion/abandonment cleanup,
cleanup-all, age/source/status selector, or promotion-backup/log-evidence
pruning. A later artifact-retention decision must authorize any such policy.

The second formal Phase 9 completion review found two remaining structural
blockers: an optional verification-publication service dependency and redaction
after the retained-window cut. Both are now remediated in code and direct
regression coverage. Phase 9 is not yet formally COMPLETE: one final completion
review must be rerun against this remediation. MCP exposure remains later Phase
11/12 work. Delete, rename, move, mkdir, binary write, type transition, shell,
and Git remain absent by design.
