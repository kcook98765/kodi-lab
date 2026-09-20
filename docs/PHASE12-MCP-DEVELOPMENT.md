# Phase 12 MCP development workflows

Phase 12 is **PARTIAL**. Slice 1 established Web-approved DevelopmentSource
grants, exact MCP workspace ownership, and bounded inspection. Slice 2 added
replay-safe creation and generation-bound UTF-8 editing. Slice 3 adds only
closed exact-generation structural validation plus system-owned bounded
retention. It adds no deployment, execution, promotion request, source
registration, external/production target, shell, Git, Docker, raw filesystem,
arbitrary testing, or arbitrary command authority.

## Authority model

`workspace.inspect` and `workspace.edit` are present only in the built-in Add-on
Development and Full Lab Control presets. Read-only Inspection remains separate
from source-code mutation. Production Remote Kodi remains empty and
unassignable. Existing custom presets are immutable snapshots and do not inherit
`workspace.edit`; a new clone of either widened built-in receives its current
permission set.

Web administrators manage exact
`(mcp_client_uuid, development_source_uuid)` grants. There is no wildcard or
all-source representation, and MCP cannot register a source or change its
locator. Source registration remains the existing Web-only create-and-observe
workflow. Ordinary source discovery reads persisted registration metadata and
never traverses source contents.

`mcp_agent_workspace_bindings` gives one workspace at most one active MCP owner
in V1. Each row retains exact client, workspace and source UUIDs, the closed
`web_admin|mcp_created` origin, and creation time. Slice 1 creates only
`web_admin`; Slice 2 creation atomically creates `mcp_created`. A composite
foreign key proves the binding source is the
workspace's actual source. There is no source-delete API. A future deletion
design must preserve or explicitly resolve grants, workspaces, and bindings
rather than orphan authority.

Read access is the current intersection of a valid credential, enabled
durable client, current `workspace.inspect`, current exact source grant, exact
client-owned binding, binding/workspace source agreement, and an
operation-compatible workspace state. A grant never exposes an unbound
workspace. A binding is insufficient after grant revocation. Disable and grant
revocation apply on the next request; bindings survive disable, credential
revoke/expiry/rotation, and never cause workspace-byte deletion.

## Read-only tools

The production catalog is exactly twelve lexical tools: `addon_list`,
`development_source_list`, `remote_action`, `screenshot_capture`, `target_list`,
`target_status`, `workspace_create`, `workspace_edit`, `workspace_read`,
`workspace_status`, `workspace_tree`, and `workspace_validate`.

`development_source_list` returns only currently granted persisted metadata:
source UUID, logical name, local-directory classification, add-on ID/name/version,
extension classifications, observation time, and conservative
`registration_observed` availability. It defaults to 50 and caps at 100. It
returns no locator, root, path, repository URL, inode/device, or raw metadata.

`workspace_status` accepts one workspace UUID or lists current owned and
still-granted bindings, default 50 and maximum 100. It returns lifecycle and
byte state, generation, creation classification, safe timestamps, and current
promotion/verification classifications when authoritative. Status remains
available for reconciliation-required or cleaned workspaces and contains no
storage/source/backup path, container identity, digest, or log text.

`workspace_tree` lists one explicit directory only. It defaults to 100, caps at
200, uses opaque UTF-8-name cursors, and preserves Phase 9 exact UTF-8 byte
ordering. Results contain only UUIDs, observed generation, relative paths,
file/directory classification, and safe file size. It does not recurse.

`workspace_read` reads one explicit UTF-8 file with optional one-based
`start_line` and `max_lines` (default 200, maximum 2,000). The underlying file
remains capped at 2 MiB. It returns at most a 44 KiB text segment inside a total
48 KiB structured result, observed generation, whole-file SHA-256, line cursor,
total byte count, and truncation state. Content and hashes are never logged or
written to audit evidence. An oversized tree page or indivisible text line
fails as `response_too_large`.

## Replay-safe workspace creation

`workspace_create` accepts exactly required UUIDs `request_id` and `source_id`.
It requires an enabled client, current `workspace.edit`, an exact current source
grant, a registered `local_directory` source, and quota capacity. It requires no
target grant. MCP supplies no source locator, source path, destination path,
workspace name, storage root, host location, or container location.

Migration `0040_mcp_workspace_mutation` adds
`mcp_development_requests`. Its current closed operation kind is
`workspace_create`, unique by durable client UUID, request UUID, and operation
kind. It retains only safe UUIDs, a normalized SHA-256 parameter fingerprint,
`allocated` classification, and timestamps. Workspace row, Phase 9 creation
operation, `mcp_created` owner binding, replay mapping, and the existing
global-heavy reservation commit atomically before worker submission. There is
therefore no visible unbound MCP-created workspace and no marker-only crash
window. A crash after commit and before submission leaves one queued operation;
normal executor startup recovers that same operation.

Exact replay returns the same workspace and creation-operation UUID with their
current semantic states. A different source under the same client/request key
returns `request_id_conflict`. Unknown commit outcome returns the known resource
when the mapping is readable, otherwise `workspace_creation_ambiguous`; it never
allocates a speculative second workspace.

Present bound workspaces count while `bytes_state=present`, including creating,
ready, promoted, abandoned, and reconciliation-required lifecycle states and
both binding origins. Limits are four per client and 32 globally. At most one
creating bound workspace per client is allowed. SQLite `BEGIN IMMEDIATE` plus
transactional count-and-insert enforcement prevents concurrent pre-check bypass;
there is no eviction. Request keys retain 30 days, are opportunistically pruned,
and fail closed at 10,000 per client or 100,000 globally. These request-key
bounds are not workspace byte cleanup or retention automation.

## Generation-bound editing

`workspace_edit` is one flat strict-discriminator tool with `create`, `replace`,
or `exact_patch`. Every form requires workspace UUID, exact
`expected_generation`, and one Phase 9 relative file path. Create and replace
accept only `content`; exact patch accepts only `expected_text` and
`replacement_text`. Aggregate user text is at most 48 KiB, and the conservatively
encoded argument object is capped below the 64 KiB MCP request ceiling.

Create requires an absent file beneath an existing safe directory and creates
no parent. Replace requires an existing strict-UTF-8 single-link regular file.
Exact patch requires the complete current file to equal `expected_text`; it has
no fuzzy matching, regex, line offsets, diff parser, merge, or rebase. All three
reuse `AgentWorkspaceService` and its 2 MiB internal file limit. Success returns
workspace/source UUID, relative path, requested operation, prior and new
generation, `succeeded`, and authoritative postimage byte size, never file
content.

Only byte-present ready workspaces with no active promotion work may edit.
Source grant, exact owner binding, source consistency, client enablement, and
`workspace.edit` are rechecked on every call. A stale generation returns
`workspace_generation_conflict`; there is no force, reread, merge, or normal
edit request ID. The Phase 9 cross-process workspace lock and database CAS mean
two edits at one generation cannot both publish. One succeeds with generation
plus one; the other conflicts without interleaved bytes.

## Closed structural validation

`workspace_validate` accepts exactly `workspace_id` and
`expected_generation`. It requires an enabled client, `workspace.inspect`, the
current exact source grant, exact owner binding/source agreement, and a
byte-present ready workspace. It requires no target grant and accepts no
command, module, pytest selector, shell argument, environment, script, plugin,
package manager or caller timeout.

The Phase 9 service takes shared workspace coordination, recaptures and checks
the immutable baseline and current tree, validates every changed file as strict
UTF-8, parses baseline/current `addon.xml` with the authoritative parser, and
derives added/modified changes. It explicitly classifies add-on ID, version and
extension-type stability. Version and extension changes remain structurally
promotable; add-on ID change is valid workspace experimentation but
non-promotable. Unexpected deletion, binary mutation, type/mode transition,
unsafe object, missing/malformed descriptor and unexplained integrity drift
fail with bounded semantic errors and never repair or adopt bytes.

Success returns semantic UUIDs, exact observed generation and time, parsed
identity, counts/classifications, promotability, and at most 64 deterministic
relative changed paths with `truncated`. It contains no content, XML, digest,
locator, backing path or raw parser/OS error and remains below 48 KiB. A stale
generation is `workspace_generation_conflict`; the server does not retry at a
newer generation. Admission permits one validation/client, one/workspace and
two globally, with no durable or unbounded limiter keys. Validation is a
read-only/observational MCP effect and stores no latest-validation table.

## Bounded workspace retention

Migration `0041_mcp_workspace_validation_retention` adds the durable semantic
`mcp_clients.disabled_at` timestamp and permits content-free system retention
audit events. Existing `abandoned_at`, `promoted_at` and `cleaned_at` remain the
authoritative lifecycle times; `updated_at` is not overloaded.

MCP-owned abandoned workspaces, and promoted workspaces whose promotion and
verification are safely terminal, retain bytes for seven days before the
existing Phase 9 cleanup service is queued. Ready workspaces owned by a disabled
client quarantine for 30 days, then use the existing safe abandonment service
and retain the ordinary seven-day cleanup grace. Re-enable before abandonment
clears `disabled_at`, cancels eligibility and restores ordinary active-ready
retention. Disabling never deletes bytes.

Enabled-client ready, creating, reconciliation-required, active/ambiguous
promotion or verification, active edit/validation and failed/uncertain cleanup
states are never age-deleted. Uncertainty retains bytes for Web-admin handling.
The opportunistic sweep inspects at most 32 candidates, performs at most two
transitions and queues at most two cleanups, using deterministic UUID cursor
ordering and immediate state/coordination rechecks. It submits only to the
existing cleanup executor; it never removes a directory directly. Cleaned
bytes cease to count toward present-byte quotas while bindings and history
remain. The separate 30-day request-ID mapping policy is unchanged.

Retention is system/Web-admin policy, not MCP authority. MCP has no
`workspace_delete`, `workspace_cleanup` or `workspace_abandon`. Existing Web
UUID-specific abandonment/confirmed cleanup remains and gains only owner state,
classification, deadline and blocker visibility; no force route is added.

## Phase 9 filesystem and integrity boundary

MCP handlers never derive a workspace root or open caller paths. They call
`AgentWorkspaceService`, retaining the Phase 9 POSIX-relative parser and
descriptor-relative traversal. Absolute paths, ASCII dot/parent components,
empty file paths, backslashes, NUL/control characters, overlong paths/depth, and
invalid surrogates fail as `path_not_allowed`. Linux case and NFC/NFD
distinctions remain exact; Unicode lookalike dots are ordinary names.

Directory and file opens remain descriptor-relative and no-follow. Final file
open pre-classifies the no-follow entry, uses `O_NONBLOCK|O_NOFOLLOW`, and
verifies descriptor identity after open. Only a single-link regular file is
readable. Symlinks, hardlinks, FIFOs, sockets, devices, and unsafe directory
entries are rejected without following or blocking on them.

Status is safe for every durable lifecycle state. Tree/read require
byte-present `ready`; reconciliation-required and other unsafe states fail
before file access. Editing additionally retains Phase 9 full pre-write
recapture, same-directory atomic publication, post-write recapture, integrity
validation, conditional generation publication, exact rollback, and
reconciliation-required fallback. Phase 9 retains its in-read identity/change
checks and all no-follow path behavior.

## Privacy, admission, transports, and Web administration

Nonexistent, ungranted, other-client-bound, revoked-source, and source-mismatched
workspaces share `workspace_not_found_or_not_authorized`. Other stable errors
include `workspace_not_ready`, `workspace_bytes_cleaned`, `path_not_allowed`,
`workspace_generation_conflict`, `workspace_integrity_changed`,
`workspace_quota_exceeded`, `request_id_conflict`,
`workspace_creation_ambiguous`, `unsupported_file_type`, `file_too_large`, and
`response_too_large`. Validation additionally uses `unsupported_deletion`,
`unsupported_type_transition`, `validation_failed`, `validation_busy`, and the
existing integrity/generation errors. Raw SQLite, OS, path, and traceback text
never crosses MCP.

The development-read class is client-keyed at 10 requests/second, burst 20,
with at most 1,024 idle-retired buckets. It is inside existing authenticated
request and client/global concurrency admission. Audit contains semantic UUIDs,
tool, outcome, and timestamps, but no paths, arguments, content, hashes,
locators, tree dumps, or raw errors.

The development-edit class is client-keyed at two requests/second with burst
four and at most 1,024 idle-retired buckets. One process-active edit per
workspace is admitted; the Phase 9 file lock is the cross-process exclusion.
Creation is heavier through its durable quotas, one-creating-per-client rule,
global-heavy coordinator, and replay mapping rather than a copy queue exposed to
the caller.

Modern stateless HTTP `2026-07-28`, canonical modern stdio, and bounded
stateless HTTP `2025-11-25` share the registry, schemas, resolver, admission
classes, services, bounds, and errors. There is no legacy stdio.

Web administration can list source grants, grant/revoke an exact source, list
eligible ready/byte-present unbound workspaces, bind one existing workspace,
and inspect bindings. Mutations retain Web authentication, Trusted Host,
same-origin, and CSRF enforcement. Slice 2 adds no Web creation button or manual
file editor. MCP cannot self-grant, manually bind an existing workspace,
unbind, transfer/share, or register a source; `workspace_create` alone creates
its atomic `mcp_created` binding.

Remaining Phase 12 capabilities are internal deployment, fixed execution, and
promotion request/human-approval integration. None is implied by
`workspace.inspect`, `workspace.edit`, or Full Lab Control.
