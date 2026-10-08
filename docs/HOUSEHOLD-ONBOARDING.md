# Household Add Kodi backend

This administrator-only coordinator adds a fresh `external_production` target
using existing native verification/designation, reviewed Bridge builds, `otp-1`,
encrypted credentials, production inspection and screenshot preference policy.
It does not authorize MCP clients. Managed lifecycle trust and the development
`onboard-otp` routes remain separate.

## API

All three POST routes require the normal authenticated Web administrator session,
same-origin checks and CSRF token. Bodies reject unknown fields. Responses use
HTTP 200 with `state`, bounded `code` and `message`, `operation_id`, and applicable
`target_id`, `name` and `transport`. FastAPI schema errors return content-free 422
responses; ordinary session/CSRF errors keep their existing status codes.

1. `POST /api/household-onboarding`

   Supply a caller-generated UUID `operation_id`, `name`, private IP `address`,
   four-character `code`, optional `kodi_port` (8080), `bridge_port` (8765), and
   optional Kodi `username`/`password`. Retain the operation ID before dispatch.
   The initial request validates the OTP format but does not consume it. It
   registers/verifies/designates the target and returns `requires_action` with
   the exact HTTP transport disclosure. OTPs/passwords are not retained in the
   operation row, response or browser storage by this backend.

   If protected native credential storage fails before registration, return
   `incomplete` with `secret_unavailable` and storage repair instructions. The
   operation stays `reserved` and retains its endpoint reservation; no target
   record or public target ID is created. After fixing the key/storage, resubmit
   Connect with the original operation ID and Kodi credentials, then use the
   normal consent/resume flow. A plain resume first returns
   `registration_retry_required`, because unpersisted credentials are not retained.
   The original OTP remains unused; registration failure does not require its
   replacement. Ordinary SQL/filesystem persistence failures remain retryable
   `persistence_failed` results.

2. `POST /api/household-onboarding/{operation_id}/resume`

   Supply `code` and
   `transport_decision: {disclosure_token, accept_http_risk: true}` after displaying
   the returned disclosure and receiving an explicit informed decision. The
   disclosure binds the operation, target, endpoint/ports, confirmed native
   observation digest, identity generation, policy revision, Bridge trust revision
   and transport policy. A stale or unaccepted disclosure cannot consume OTP.
   The operation's native binding is persisted after safe policy initialization.
   Consent acknowledgement checks the expected binding and inserts the existing
   audit record in one SQLite writer transaction. A changed endpoint, native
   generation/observation or policy revision cannot be rebound by fresh consent;
   the operation returns an actionable conflict and remains closed to agents.
   Resume after bootstrap/pairing persistence needs neither OTP nor repeated consent;
   existing exact acknowledgement checks remain mandatory. A response lost before
   registration completes may require resubmitting the original start request with
   its same operation ID and Kodi credentials.

3. `POST /api/household-onboarding/{operation_id}/abandon` with `{}`

   Explicitly retire an incomplete operation's local Bridge authority and deactivate
   its target through existing services. Preserve operation/target audit history
   and release its endpoint reservation. This performs no remote reset. Reset
   pairing explicitly in Kodi before a new Connect operation. Connected operations
   require ordinary target administration instead of this incomplete-flow cleanup.
   If deactivation succeeded but saving abandonment failed, retry accepts that
   completed step and uses existing production native-trust invalidation to finish
   local Bridge cleanup; ordinary Bridge revocation deliberately rejects retired
   targets. Shared revocation and native credential deactivation journal opaque
   pending-deletion references in the same SQLite transaction that retires authority.
   File deletion precedes removal of each journal entry; failure or process loss at
   either boundary leaves a retryable obligation. Final and interrupted temporary
   files share one opaque reference. Already-missing files are confirmed with a
   directory fsync before acknowledgement. Cleanup holds the SQLite writer lock and
   rechecks each obligation so a stale snapshot cannot delete a new live owner. Deactivation is followed by residual Bridge invalidation,
   closing the revoke/deactivate gap. A SQL guard prevents abandonment publication
   while any deletion obligation remains. Failed cleanup keeps the reservation; repeated successful abandonment
   does not reactivate or restore authority.

`connected` means fresh native/Bridge verification and final policy checks passed.
`incomplete` means resume the same operation after a transient or persistence error.
`requires_action` identifies consent, current OTP, independent build review, device
identity review or explicit pairing reset/abandonment. `failed` covers invalid initial
input, an unknown operation or request/endpoint/name conflict. Transport disclosure
is an action field; ordinary results omit internal trust stages and credentials.

## Durable transitions

| Internal checkpoint | Durable meaning and recovery |
| --- | --- |
| `reserved` | Request identity, endpoint reservation and target UUID precede registration. Retry can find the exact target even if registration's response was lost. |
| `native_verified` | Exact observation confirmed and production designated. Native read-only enablement, mutations suspended and safe target maximum initialized before pinning endpoint, observation, generation and policy revision. Agent admission remains blocked. Consent and an OTP can be supplied explicitly. |
| `bootstrap_started` | Persisted before one bootstrap attempt. A recoverable pending/paired credential resumes commit. Missing credential requires reset/abandonment, never another automatic bootstrap. Only a Bridge-service classification proving a pre-dispatch failure or definite bootstrap rejection may return to `native_verified`; a subsequent identity/protocol error cannot reopen dispatch. Unsigned discovery metadata cannot override this checkpoint. |
| `paired` | Existing authenticated commit/provenance confirmation completed. Final target policy and fresh checks still precede agent admission. |
| `connected` | SQLite writer reservation binds final publication to fresh native/Bridge snapshots, the original native binding, accepted disclosure token and read-only target preset. No client grant or scope was created. |
| `blocked` | Identity/administration conflict cannot be silently rebound; target is disabled. |
| `abandoned` | Local trust retired, target deactivated, endpoint reservation released; historical evidence remains. |

Native defaults contain only `target_inspect`, `addon_inventory` and `screenshot`.
The target receives the existing Production Visual Inspection maximum; native
navigation/playback, execution, development and deployment remain excluded.
This maximum is assigned before transport disclosure so its policy revision change
is included in the original binding. The incomplete-operation admission gate still
blocks every intermediate target from MCP access.
Production mutation suspension remains set. The independent screenshot preference
initializes enabled from the configured maximum. Retries preserve a later explicit
administrator preference. Structured UI observation remains subject to the existing
reviewed-build support and ordinary authorization checks.

Endpoint reservation creation serializes with registration in SQLite. SQL guards
also reject independent target registration/endpoint edits into another operation's
reservation. Matching names/IPs/Kodi versions never adopt an existing active target.
A new UUID at an occupied endpoint conflicts; the same operation ID cannot change
its nonsecret request identity. Per-operation POSIX locks serialize Web workers and
release on process exit; lock files are retained to avoid unlink/recreate races.

Reviewed bootstrap metadata is only an independent-record candidate lookup.
Unknown/retired/incompatible candidates and a missing Lab credential encryption
key are rejected before OTP consumption. The
existing authenticated identity, version/protocol, complete capability-set and
reviewed provenance checks still precede pairing confirmation. No approval records
are created automatically. HTTP disclosure explicitly covers pairing credentials
as well as Kodi credentials, metadata and pixels; authentication does not provide
HTTP confidentiality.

## Bounded limitations and qualification

- Every newly materialized external credential has a committed opaque reference
  in the existing retirement journal before its temporary or final file is written.
  Publication rechecks lifecycle state under a SQLite writer reservation, writes the
  file, and atomically transfers its reference to durable ownership while removing
  the obligation. Failed/interrupted publication retains its obligation; replacement
  journals displaced credentials in the same ownership-transfer transaction.
  Historical references lost before this repair cannot be reconstructed. No orphan
  sweep is introduced; migration preserves existing live references.
- A crash, storage failure or prolonged network loss after OTP consumption but
  before Lab credential persistence cannot be recovered automatically by the
  existing service. The durable checkpoint fails closed and instructs explicit
  abandonment and local Bridge reset. Even a crash immediately before dispatch
  can require that conservative action. Credential journaling before dispatch is
  a separate future improvement; no second pairing protocol was added here.
- Once registration persists Kodi login credentials, retry does not replace them.
  Abandon the partial operation and start again if those credentials were wrong.
- All workers accessing a database must share the same data-root lock directory on
  a filesystem with working POSIX advisory locks. Cross-host replicas with separate
  lock roots are unsupported. SQLite endpoint reservations still prevent separate
  operation IDs from creating duplicate active targets.
- Native bindings are never automatically refreshed after administrator policy or
  identity changes, including when resuming a previously connected operation.
  The independent screenshot preference remains outside this binding and ordinary
  preference changes continue to survive retries. An operation missing its native
  binding fails closed; this is no authority to adopt earlier partial state.
- Independently approved records must already exist in the Lab. Approved repository
  artifact distribution/installation and actual remote binary attestation are
  outside this backend change; a self-reported fingerprint is never an approval.
- Existing production Bridge compatibility requires the current supported add-on
  version and reviewed screenshot-capable builds. Unsupported builds require
  administrator review/update; this coordinator does not weaken that contract.
- IP-literal private endpoints and configured port allowlists are unchanged.
  Friendly hostnames, HTTPS/Bridge transport redesign and remote-control consent
  are outside this implementation.
- Device reset and live-target verification were not performed. Focused local
  validation is separate from repository RELEASE qualification and live acceptance.

The next implementation task is a minimal Add Kodi Web flow that retains the
operation UUID before Connect, displays the exact transport disclosure, accepts
explicit consent, clears OTP/password inputs and handles resume/action results.

## Implementation verification

Starting repository: `/home/kyle/kodi-lab`, branch `main`, HEAD
`6ca4b53a8fc6cf06484881c31441c6ada5149455`, clean worktree, two local commits ahead
of `origin/main`. HEAD remains unchanged; all implementation changes are local and
uncommitted. No push, deployment, service restart or live Kodi modification occurred.

Files changed:

- Application: `src/kodi_lab/application/household_onboarding.py`,
  `external_targets.py`, `external_bridges.py`, `production_inspection.py`.
- Persistence: `src/kodi_lab/storage/models.py`, `household_onboarding_sql.py`,
  `migrations/versions/0071_household_onboarding.py`, and the shared retirement
  helpers in `src/kodi_lab/storage/secrets.py`.
- Administrator API: `src/kodi_lab/web/app.py`.
- Tests: `tests/application/test_household_onboarding.py`,
  `tests/web/test_household_onboarding.py`,
  `tests/storage/test_household_onboarding_migration.py`,
  `tests/storage/test_migrations.py`.
- Documentation: `docs/DECISIONS.md` and this file.

Initial coordinator **FOCUSED PASS**: 26 application, six Web and three new migration tests.
Coverage includes safe defaults, no MCP grants/control/deployment, screenshot
preference, reviewed/unknown/modified builds, explicit consent, OTP single use,
response/commit loss, duplicate/overlapping requests, endpoint conflicts, changed
identity, interrupted persistence, stale credentials, secret redaction, explicit
administrator disable/deactivation, concurrent final policy change, abandonment
and missing encryption key before OTP consumption.

Existing focused regression selections: 217 passing tests across development
onboarding, external target administration, production OTP/target/inspection/Bridge
services, structured observation, managed lifecycle Bridge trust, screenshot
preferences, read-only authorization, MCP external targets, external Web routes
and screenshot-preference migration. The complete migration file reported
111 passing tests and one pre-existing failure:
`test_empty_database_upgrades_to_runtime_catalog_head` omits
`managed_runtime_reconstruction_operations` from its expected inventory. The
mismatch was independently confirmed using the starting HEAD's test inventory
against a disposable database upgraded only through migration 0070. The new
household table and head expectation were added; the unrelated baseline omission
was left unchanged.

Repository Ruff format/lint, relevant strict mypy, `git diff --check`, package
build/import and Alembic head/history checks passed. The wheel contains both the
coordinator and migration. No complete repository suite or live acceptance was
run. Broader validation remains **PARTIAL**; **RELEASE PASS** is not established.

## Targeted defect repair verification

Final review reproduced two defects: consent validation and acknowledgement used
different target snapshots, and abandonment retried administration services that
reject already deactivated targets. Both are repaired locally and remain uncommitted.

The coordinator now stores `native_binding_digest` in its existing uncommitted 0071
migration and ORM model. The binding includes the reserved target/endpoint, exact
confirmed observation digest, native generation and policy revision. Safe target
preset assignment precedes binding/disclosure because its existing SQL guards advance
that revision. The shared HTTP acknowledgement service accepts an optional expected
binding and validates it while writing acknowledgement under `BEGIN IMMEDIATE`.
Final publication independently verifies the original native binding and disclosure
token, even when supplied snapshots match a subsequently reverified target.

Abandonment accepts completed deactivation and uses existing production native-trust
invalidation to retire any residual local Bridge authority. SQL, filesystem and
credential-store cleanup errors retain the operation/reservation for retry. Successful
repetition preserves historical events and does not restore credentials or authority.
No new lock infrastructure or committed historical migration was changed.

Settled repair validation achieved **FOCUSED PASS**:

| Test file/group | Passing tests |
| --- | ---: |
| Household application, including 18 added regression cases | 44 |
| Household administrator API | 6 |
| Household migration/persistence, including durable binding across service restart | 3 |
| Production OTP pairing | 7 |
| Production Bridge/HTTP consent | 18 |
| Production target administration | 14 |
| External target administration | 7 |
| Production inspection | 20 |
| Existing development onboarding | 25 |
| Managed lifecycle Bridge trust | 13 |
| Total repository tests selected | 157 |

Both original standalone fault injections also pass. Regression cases cover edits
before acknowledgement, edits after acknowledgement and before publication,
pending reverification, stale policy/generation, unchanged valid consent, concurrent
administrator writes, cleanup interruption/retry, reservation retention/release,
repeated/concurrent abandonment and absence of credential/authorization restoration.
The migrated-schema test verifies that native binding survives coordinator recreation.

Repository Ruff formatting/lint (613 files), focused strict mypy, `git diff --check`
and the single Alembic 0071 head passed. The known migration inventory baseline
failure was not rerun or repaired. No complete repository suite, live Kodi testing,
deployment, service restart, commit or publication occurred. **RELEASE PASS** remains
unestablished. The next task is independent final backend review before any Web slice.

## Recovery defect repair verification

The subsequent independent review found two further recovery defects: revocation
forgot secret references before failed file deletion, and a protocol error after
OTP consumption was incorrectly classified as safe to bootstrap again.

Migration 0071 now also creates `external_secret_retirements`, containing only the
store kind, opaque reference UUID, target UUID and timestamp. Existing shared Bridge
revocation/native invalidation and native credential deactivation commit deletion
obligations with authority retirement. Retries drain them without restoring live
references, credentials, scopes or grants. Obligations are removed only after the
existing no-follow secret-store deletion succeeds; a failed DB acknowledgement
therefore retries an already-missing file safely. Populated pending obligations
prevent downgrade and a SQL guard prevents reservation release through abandonment.
No historical committed migration was changed.

The shared OTP service now classifies pre-dispatch errors and definite bootstrap
rejections separately from post-dispatch identity/transport errors. The coordinator
requires that classification before returning to `native_verified`. Missing durable
credentials after an uncertain outcome retain `bootstrap_started` and require
reset/abandonment; available credentials still recover lost bootstrap/commit responses.
Replay of unsigned discovery information cannot reopen a consumed operation.

New regression cases cover repeated unlink failure, coordinator/service recreation,
Bridge tokens and request credentials, native credentials, residual trust in the
revoke/deactivate gap, deletion followed by DB acknowledgement failure, atomic journal
insertion with revocation, migrated-schema restart, downgrade refusal, protocol errors
after consumed/lost bootstrap responses, replayed discovery, definite pre-dispatch
failures and ordinary persisted-credential commit recovery. Original review artifacts
remain unchanged; the copied protocol probe changes its obsolete `native_verified`
assertion to the repaired `bootstrap_started` expectation.

This repair's final focused selections passed 190 repository tests: 58 household
application cases, five household migration cases, two selected history checks,
112 service/API/secret-store regressions, and 13 managed Bridge trust cases.
The four copied independent review probes pass; the original raw protocol probe
stops at its obsolete assertion requiring the broken `native_verified` state.
Its preserved replacement expects `bootstrap_started` and confirms one bootstrap
POST across retries with replayed discovery. Ruff and focused strict mypy passed
across 22 selected Python paths, together with diff and Alembic head/history checks.
No repository-wide suite or live acceptance was run. **FOCUSED PASS** is achieved;
**RELEASE PASS** remains unestablished. All changes remain uncommitted for independent
final review.
