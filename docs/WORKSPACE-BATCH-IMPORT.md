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
   rejections. An eligible preview returns `source_digest` and `preview_digest`.
3. Call `workspace_import` with those same fields and a fresh UUID `request_id`.
   All accepted bytes publish as exactly one new generation. The original
   baseline and unrelated private edits remain intact.
4. Validate the returned generation, deploy that exact generation, and perform
   runtime checks through their existing separate operations.

The initial implementation accepts at most 32 changed entries and UTF-8 files
up to 48 KiB each. Parents must already exist in the workspace. Deletions,
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
