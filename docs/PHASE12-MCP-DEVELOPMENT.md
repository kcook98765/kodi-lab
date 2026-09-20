# Phase 12 MCP development workflows

Phase 12 is **PARTIAL**. Slice 1 establishes only Web-approved
DevelopmentSource grants, exact MCP workspace ownership, and bounded read-only
inspection. It adds no workspace creation, editing, validation, deployment,
execution, promotion request, cleanup/retention mutation, source registration,
external/production target, shell, Git, Docker, raw filesystem, or arbitrary
command authority.

## Authority model

`workspace.inspect` is present only in the built-in Add-on Development and Full
Lab Control presets. Read-only Inspection remains separate from source-code
access. Production Remote Kodi remains empty and unassignable. Existing custom
presets are immutable snapshots and do not inherit the new permission; a new
clone of either widened built-in receives its current permission set.

Web administrators manage exact
`(mcp_client_uuid, development_source_uuid)` grants. There is no wildcard or
all-source representation, and MCP cannot register a source or change its
locator. Source registration remains the existing Web-only create-and-observe
workflow. Ordinary source discovery reads persisted registration metadata and
never traverses source contents.

`mcp_agent_workspace_bindings` gives one workspace at most one active MCP owner
in V1. Each row retains exact client, workspace and source UUIDs, the closed
`web_admin|mcp_created` origin, and creation time. Slice 1 creates only
`web_admin`. A composite foreign key proves the binding source is the
workspace's actual source. There is no source-delete API. A future deletion
design must preserve or explicitly resolve grants, workspaces, and bindings
rather than orphan authority.

Workspace access is the current intersection of a valid credential, enabled
durable client, current `workspace.inspect`, current exact source grant, exact
client-owned binding, binding/workspace source agreement, and an
operation-compatible workspace state. A grant never exposes an unbound
workspace. A binding is insufficient after grant revocation. Disable and grant
revocation apply on the next request; bindings survive disable, credential
revoke/expiry/rotation, and never cause workspace-byte deletion.

## Read-only tools

The production catalog is exactly nine lexical tools: `addon_list`,
`development_source_list`, `remote_action`, `screenshot_capture`, `target_list`,
`target_status`, `workspace_read`, `workspace_status`, and `workspace_tree`.

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
before file access. Phase 9 retains its in-read identity/change checks.

## Privacy, admission, transports, and Web administration

Nonexistent, ungranted, other-client-bound, revoked-source, and source-mismatched
workspaces share `workspace_not_found_or_not_authorized`. Other stable errors
include `workspace_not_ready`, `workspace_bytes_cleaned`, `path_not_allowed`,
`unsupported_file_type`, `file_too_large`, `response_too_large`, and
`workspace_integrity_changed`. Raw SQLite, OS, path, and traceback text never
crosses MCP.

The development-read class is client-keyed at 10 requests/second, burst 20,
with at most 1,024 idle-retired buckets. It is inside existing authenticated
request and client/global concurrency admission. Audit contains semantic UUIDs,
tool, outcome, and timestamps, but no paths, arguments, content, hashes,
locators, tree dumps, or raw errors.

Modern stateless HTTP `2026-07-28`, canonical modern stdio, and bounded
stateless HTTP `2025-11-25` share the registry, schemas, resolver, rate class,
services, bounds, and errors. There is no legacy stdio.

Web administration can list source grants, grant/revoke an exact source, list
eligible ready/byte-present unbound workspaces, bind one existing workspace,
and inspect bindings. Mutations retain Web authentication, Trusted Host,
same-origin, and CSRF enforcement. There is no MCP self-service, unbind,
transfer/share, creation, or source registration in Slice 1.

Remaining Phase 12 capabilities are replay-safe workspace creation,
generation-bound create/replace/patch editing and CAS, validation, quotas and
retention, deployment, execution, and promotion request. None is implied by
`workspace.inspect` or Full Lab Control.
