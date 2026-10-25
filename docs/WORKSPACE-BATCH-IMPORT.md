# Import a reviewed registered-source batch

Use this workflow when a project's registered source mirror contains reviewed
changes that should become private workspace edits. Update that mirror through
the project's normal local workflow first. Lab never reads an arbitrary path on
the agent's or Windows user's computer.

The MCP client needs both `workspace.edit` and the opt-in
`development_source.inspect` permission, the exact registered-source grant and
the workspace binding. Existing presets gain no permissions automatically.

1. Read the current workspace generation using `workspace_status`.
2. Call `workspace_import_preview` with `workspace_id` and that
   `expected_generation`. Review its file paths, sizes, SHA256 values and policy
   rejections. `max_file_bytes` and `max_changed_files` advertise the enforced
   limits, including on ineligible previews. An eligible preview returns `source_digest` and `preview_digest`.
3. Call `workspace_import` with those same fields and a fresh UUID `request_id`.
   All accepted bytes publish as exactly one new generation. The original
   baseline and unrelated private edits remain intact.
4. Validate the returned generation, deploy that exact generation, and perform
   runtime checks through their existing separate operations.

The importer accepts at most 32 changed entries and UTF-8 files
up to 256 KiB (262,144 bytes) each. Parents must already exist in the workspace. In default `changes` mode, deletions,
directory/file transitions, executable-mode changes, changed add-on identity,
binary files and overlaps with private edits are rejected. A rejected batch
does not partly apply. A changed source or workspace requires a fresh preview.
Import neither deploys bytes nor starts, stops or restarts Kodi.

Later imports can update previously imported files while their workspace bytes
still match the exact prior import receipt. Private edits to those files cause
an overlap rejection when the source also changes. The promotion baseline stays
unchanged throughout these imports.

Repeat the identical `workspace_import` request after a lost response to read or
recover its receipt. Reusing a request ID with different parameters fails.
Recovery checks the retained old/new trees against the durable journal; it does
not capture newer source bytes or dispatch the import again. Unprovable outcomes
remain `reconciliation_required`, with the workspace unavailable for ordinary
development until administrator reconciliation. A failed receipt remains failed;
review a new preview and use a new request ID for a new attempt.

Previous trees and import receipts are retained. The initial implementation
permits 64 import receipts per workspace and requires 2 GiB free space beyond
its source/workspace staging estimate. Capacity errors require administrator
storage review. Whole-workspace cleanup refuses retained import history until
reference-aware cleanup is available; it never silently removes recovery data.


## Reviewed removals and private conflicts

To stage the registered source as a reviewed tree, pass `mode: "reviewed_tree"`
to both preview and apply. The preview includes source removals and private-only
files as well as additions/replacements. Each file includes the current private
preimage hash, size and mode; the full workspace digest also binds these bytes.
Removals count toward the same 32-entry cap. Manifest removal/identity changes,
new/removed directories, directory/file transitions and changed file modes remain
unsupported. An empty leftover directory therefore blocks an exact-tree claim.

Private differences require an explicit per-path resolution. First read the
unresolved preview, then preview again with, for example:

```json
{
  "workspace_id": "<workspace UUID>",
  "expected_generation": 196,
  "mode": "reviewed_tree",
  "resolutions": [
    {"path": "old_artwork.py", "choice": "take_source"},
    {"path": "private_notes.py", "choice": "keep_private"}
  ]
}
```

`take_source` accepts that exact incoming file or its removal. `keep_private`
retains the reviewed private preimage (including an already absent file), and is
reported as `retained_private`. No wildcard/force resolution exists. Resolutions
must be unique and refer to actual private conflicts. Apply repeats the mode,
resolutions and digests from the **resolved** preview with a fresh request UUID.
Changing a resolution requires another preview. Repeat the identical request
for replay, including options and resolution order.

`matches_source: true` means the eligible reviewed-tree plan has no retained
private differences. Its staged result must match the complete source digest
before publication. Keeping any private difference yields `matches_source: false`:
this is a reviewed merge. Default changes mode also reports false because it does
not assess unrelated private files. A preview is a read, not a reservation.

Deletion occurs only in the private staged copy, using descriptor-bound ordinary
file traversal. The complete expected result is checked before the atomic tree
exchange. The original baseline, prior tree and operation receipt remain intact;
versioned receipts record deletion tombstones and private choices. Kept private
bytes do not acquire imported-source provenance. Original list-form receipts
remain readable; older application code cannot interpret the new versioned
receipts, so rolling back application bytes after use needs a reviewed recovery
plan. No schema migration or source-baseline rewrite is needed. Existing
populated-receipt downgrade refusal remains in force.

Staging remains separate from validation, deployment, promotion and native
acceptance. Existing promotion/deployment capability limits still apply; an
import receipt does not widen those operations or modify a Kodi profile.

Publication readback also covers cancellation just after a database commit. A
proved committed receipt keeps the published tree. A proved unpublished ordinary
failure may roll back; unknown publication retains both trees and fences the
workspace, including when its generation already advanced. Replay then proves
the complete retained trees and acknowledges that same generation once, without
advancing it a second time or capturing newer source bytes.
