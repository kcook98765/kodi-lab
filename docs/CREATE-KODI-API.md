# Authenticated Create API (issue #196, milestones 3A and 3B2)

Acceptance records one explicitly approved Create intent; status/reconnect remain
passive. Only the explicit protected continuation POST invokes the existing
bounded runner. Approval covers
the existing `create_native_v1` scope; the eventual normal workflow remains
Create Kodi → Preparing → Ready → Open screen, without separate approvals for
each internal stage. No wizard is included.

## Authentication and request boundary

All routes use the existing single-admin Web session. An MCP bearer credential
is not a Web administrator session. POST additionally uses the existing CSRF
header and same-origin Origin/Referer checks, with existing HTTPS, trusted-host
and proxy restrictions. No presentation mode, new role, session or approval
mechanism changes authorization.

POST verifies authentication/origin/CSRF before reading Create JSON. The body
is limited to 4096 bytes and a five-second read deadline, then validated with
Pydantic strict types and forbidden extra fields. Untrusted body content and
validation exceptions are not echoed. The existing Web security headers apply;
Create responses, including errors, use `Cache-Control: no-store`.

## Read the reviewed selection

`GET /api/create-kodi/prerequisites?generation=omega` retains its existing read-only
checks and component. It now also returns `recipe_id`, `recipe_revision`,
`recipe_digest` and `target_id` from the reviewed wheel recipe, alongside the
existing generation/version. This machine metadata binds an approval to what
the client presented and provides a canonical reconnect key. It is not runtime
or broker authority. Normal UI rendering still uses the existing check labels.

## Accept approval

`POST /api/create-kodi/operations`

JSON fields (all required, no extras):

| Field | Meaning |
| --- | --- |
| `request_id` | Client-generated UUID, persisted before the first submission and reused unchanged after lost responses |
| `generation` | `matrix`, `nexus` or `omega`; preview/other generations are rejected |
| `recipe_id` | Reviewed ID from the selection snapshot |
| `recipe_revision` | Positive integer from the same snapshot |
| `recipe_digest` | Exact reviewed SHA-256 digest from that snapshot |
| `owner_approved` | JSON boolean `true`, explicitly supplied |

A new request must match the current reviewed recipe tuple. The server derives
the canonical target; callers cannot supply target/container/runtime identities,
child IDs, force flags, leases, proofs, paths or expected stage transitions.

New intent refreshes the existing non-reconciling prerequisite observations. An
unavailable installation/capacity observation returns 503; known setup/policy/
capacity/storage blockers or unrelated active installation return 409. Unknown
Docker storage, physical Windows disk, VM memory or filesystem measurements do
not become invented thresholds or automatic blockers. Recorded administrator
settings remain unchanged. Pending mutation/heavy leases/reservations, existing
recovery requirements and an already-present container for this canonical Kodi
are reported without adoption or repair.

These are snapshots before acceptance, not reservations or installation
authorization. They can change immediately afterward. Every typed stage repeats
its authoritative recipe, runtime, identity, selection, lease,
capacity and broker admission checks. This API does not duplicate those stages'
complete admission policies or issue their authority.

The existing `CreateKodiService.accept()` and SQLite writer/unique constraints
perform durable admission. New approval returns 202, the status representation
below, `replayed: false`, and a relative `Location` matching `status_url`. The
parent remains Installation / Accepted at version zero, without child IDs.
The message explicitly says that Create is approved and setup has not started.

Identical request replay returns 200 with `replayed: true` and the same parent,
timestamps, version and original approved recipe. It does not depend on current
prerequisites or current recipe publication remaining available. Different
inputs for the same request return 409 `create_request_conflict`. Another UUID
for the same canonical target returns 409 `create_target_conflict`, including
the saved `operation_id` and `status_url`; it is not accepted as a new alias.
These rules include terminal/completed history. Both lookup/acceptance races
and independent processes preserve the original identity.

Stale selected recipe identity returns 409 `create_recipe_changed`. Missing or
invalid input returns 422 `create_invalid_request`; an explicit false approval
returns 422 `create_approval_required`. Wrong content type is 415, oversized
approval 413, and body-read timeout 408. Domain errors use the existing
`error: {code, message}` convention, with fixed safe messages. Existing Web
authentication/origin failures retain their established 401/403 shape.

## Passive status and reconnect

- `GET /api/create-kodi/operations/{operation_id}` reads the existing UUID.
- `GET /api/create-kodi/operations/by-target/{target_id}` locates the existing
  canonical parent after refresh, another tab, session renewal or restart.
- Missing records return 404 `create_operation_not_found`; malformed UUIDs are
  rejected. Lookup never creates a record or submits an approval.

Both return the same strict, small representation:

| Field | Meaning |
| --- | --- |
| `operation_id`, `status_url` | Stable parent identity and relative status resource |
| `generation`, `kodi_version` | Original requested version resolved from its recorded reviewed recipe; unavailable archive details are null |
| `version`, `stage`, `checkpoint` | Fixed parent synchronization metadata; not child receipts or execution authority |
| `phase`, `status`, `message` | Browser labels: Preparing/Setting up/Starting/Checking Kodi, Ready or Needs attention; waiting/preparing/attention/complete |
| `reason_code` | Fixed public diagnostic, never an echoed internal reason |
| `complete` | Historical parent completion, independent of subsequent runtime state |
| `next_action` | Snapshot guidance: `continue`, `wait`, `check_again`, `needs_attention` or `complete`; never execution authority |
| `instance` | Public target UUID, current availability label/message and existing details URL where registered |

Normal UI should render phase/message, rather than requiring users to understand
the machine stage/checkpoint. There is no fabricated percentage or running task
promise. Dispatched work without confirmed outcome is shown as waiting for
confirmation; terminal refused/failed/uncertain outcomes require review and never
invite an unsafe automatic retry.

Exact-instance availability compares the saved container/runtime/image binding
with read-only inventory and the current pin. It can report running, stopped,
unavailable, changed, unknown or review required, without returning those raw
internal identities. A running container is not a fresh native-readiness or screen
availability result. Historical completion remains true after a manual Stop,
replacement or deletion; the separate current availability explains the change.
No current projection is rewritten or adopted to make the display look healthy.

Parent status reads are SELECT-only. Current availability uses database reads,
the existing read-only reconciliation inspector without broker/readiness/recovery
executors, and inventory only. It never calls stateful runtime status, installation
reconciliation, child stage execution, native observation/probes or viewer services.
No raw reasons, child UUIDs, proofs, lease digests, credentials, image/container IDs,
internal filesystem paths or broker endpoints appear in the normal response.

## Explicit bounded continuation

`POST /api/create-kodi/operations/{operation_id}/continue`

The only body field is `{"version": 0}`, using the exact nonnegative integer
version last read from durable status. Strings, booleans, floats, unknown fields
and query parameters are rejected. Clients cannot supply stage, child UUID,
container, proof, force or retry parameters. The same administrator session,
CSRF, Origin/Referer, trusted-host and HTTPS protections apply. An authenticated
read request or MCP bearer credential alone does not authorize continuation.

One POST invokes `CreateKodiRunner.continue_once()` once. The entire synchronous
action and status presentation run in the existing Web thread pool, including
broker calls and bounded native probes; they do not block the application event
loop. No queue, scheduler, request retry or independent execution authority is
added. Original child IDs, dispatch checkpoints, leases and Start proof admission
stay in their existing services. The original Create approval covers the normal
fixed sequence without separate per-stage consent.

Success returns 200 with the same `CreateStatus`, `Location: <status_url>` and
`Cache-Control: no-store`. Unknown parent is 404. Stale version is 409
`create_stale_version` with the saved status URL; it never executes using the
newer version. A competing claim after initial validation may return the current
durable winner through the runner's existing version fence. Other transition
refusals return 409 `create_continuation_blocked`; unexpected response/service
failure returns 503 `create_continuation_unavailable`. Errors use fixed safe
messages and reference the original parent/status resource, never raw exceptions.

After a timeout or missing response, first GET the saved status URL (or reconnect
by canonical target) and retain the original operation identity. A failed HTTP
response does not establish that a child failed or was never dispatched. Never
replace its child UUID or replay earlier mutation work. The endpoint itself
does not retry, reset, repin, replace or Stop anything.

Presentation guidance comes only from the parent snapshot and read-only current
availability. `continue` means the next explicit call can plan/advance or ask the
existing adapter to admit its action. `wait` means a dispatched mutation has no
confirmed parent result; it is not a promise that GET polling will advance it.
A later explicit POST inspects the original installation receipt or lifecycle
ownership without redispatch. Matching active ownership returns waiting;
missing, terminal-inconsistent or expired lifecycle ownership becomes Needs
attention. Original uncertain mutation outcomes do not authorize automatic retry.

`check_again` at native readiness invokes only the existing bounded read-only
recheck (or exact local registration confirmation at that stage). `needs_attention`
requires review; supported original installation/registration observation remains
separate from mutation retry. `complete` is historical even after manual Stop.
All metadata is informational: every continuation still checks the parent version
and its existing authoritative stage admission. Passive GETs never claim a
probe, confirm a child, change a stage or create viewer tickets.

The prerequisite runtime-file observation also recognizes the legacy
`setup_incomplete` view: that view is emitted only after eligibility and exact
image presence pass, when canonical registration is absent. It is verified
runtime reuse, not a download blocker. Registration remains a separately admitted
Create stage, and missing images or unverified runtimes still require attention.

## Guided interface

Milestone 4A adds [the guided Create Kodi page](CREATE-KODI-WIZARD.md) using these
unchanged APIs. It persists the original approval before acceptance, reconnects
before offering Create, and paces version-fenced foreground continuation without
a server scheduler. Unknown responses pause and retrieve status before Resume.

It includes reviewed stable selection, embedded prerequisites, progress/attention
handling and independent exact-instance Open screen links. Existing recovery
surfaces remain separate from new attempts or replacement/reset behavior.
Bridge and agent setup remain optional after native Kodi is usable. New-attempt
recovery and replacement/reset semantics are outside this milestone.
