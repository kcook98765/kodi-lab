# Authenticated Create API foundation (issue #196, milestone 3A)

This API records one explicitly approved Create intent and exposes its durable
status. It does not execute a runner, plan children, install, provision, Start,
Stop, probe readiness, configure Bridge or create viewer tickets. Approval covers
the existing `create_native_v1` scope; the eventual normal workflow remains
Create Kodi → Preparing → Ready → Open screen, without separate approvals for
each internal stage. No wizard or execution/continuation endpoint is added here.

## Authentication and request boundary

All routes use the existing single-admin Web session. An MCP bearer credential
is not a Web administrator session. POST additionally uses the existing CSRF
header and same-origin Origin/Referer checks, with existing HTTPS, trusted-host
and proxy restrictions. No presentation mode, new role, session or approval
mechanism changes authorization.

POST verifies authentication/origin/CSRF before reading approval JSON. The body
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
authorization. They can change immediately afterward. Every future typed stage
must repeat its authoritative recipe, runtime, identity, selection, lease,
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

## Subsequent integration

A separately approved bounded runner must consume this same accepted intent and
invoke the existing fixed-stage adapters with their persisted child UUIDs and
transactional dispatch checkpoints. The approval must not be split into separate
installation/registration/provision/Start consent prompts. An authenticated,
version-checked explicit continuation surface still needs implementation; status
GETs must remain passive, readiness rechecks must stay read-only and uncertainty
must never cause earlier mutation replay or replacement UUIDs.

The guided wizard still needs client UUID persistence, reviewed selection and
embedded prerequisites, honest progress/reconnect/attention handling, explicit
supported recovery actions, and independent exact-instance Open screen access.
Bridge and agent setup remain optional after native Kodi is usable. New-attempt
recovery and replacement/reset semantics are outside this milestone.
