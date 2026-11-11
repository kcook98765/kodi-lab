# Native Create readiness (issue #196, milestone 2D)

`CreateKodiReadinessService` explicitly advances a verified Startup / Confirmed
parent to Readiness / Accepted, then inspects native Kodi through the existing
`KodiReadinessCoordinator` and fixed-method `KodiJsonRpcReadinessAdapter`. It is
an internal application service, with no runner, startup hook or HTTP endpoint.

## Verification and authority

Admission, each native observation context and the final completion transaction
verify the original successful PROVISION and START receipts, canonical target,
reviewed recipe, eligible sealed runtime, registration/pin, exact image/container
and saved post-Start projection. The original ordinary Start authorization must
match that binding, have been consumed between issuance and child completion,
and retain the owned projection exactly one below the parent's post-release
projection. None of this authorizes another Start or recreates a proof.

The saved projection must still match the live registration. Running lifecycle
work, mutation leases (including expired/unresolved leases), capacity reservations,
global heavy-operation ownership, pending/ambiguous target agent lifecycle work
and reconciliation requirements prevent completion. These are read-only checks;
the service never reconciles or clears a blocker. No other managed Kodi may be
running, unknown or transitional. Every inventory read requires the original
container/revision/image to be uniquely present and running.
The administrator’s running limit remains authoritative at Create Start admission.
Other exact, known running instances may coexist within that limit. Valid
retained mode supplies that limit without changing its recorded resource policy;
an administrator's incompatible later policy change requires explicit correction.

A read-only broker facade fences each process observation to the original target,
container and revision before the native coordinator sees it. The existing
coordinator checks trusted native process health, `JSONRPC.Ping`, and
`Application.GetProperties` against the exact reviewed execution profile's Kodi
name, release, channel and prerelease ordinal. Container RUNNING or a stale
process-local readiness result cannot complete Create. The service also validates
all successful result layers and repeats authoritative binding/recovery/inventory
checks under the final SQLite writer transaction.

Bridge, MCP registration/grants, development selection/workspaces, screen capture,
noVNC and input ownership are not readiness capabilities. There is no Bridge
preparation, lifecycle call, download or repair in this adapter. Screen access
continues through the independent exact-instance viewer controls.

## Durable transitions and explicit rechecks

The fixed readiness correlation remains `uuid5(parent, "create-kodi:readiness:v1")`.
No child table, attempt UUID, new broker authority or migration is introduced.

| Explicit action | Required state | Durable result |
| --- | --- | --- |
| Advance | Startup / Confirmed with original Start evidence | Readiness / Accepted |
| Inspect (first call) | Readiness / Accepted | Existing Plan, then a verified Readiness / Dispatched claim |
| Inspect (continue planned) | Readiness / Planned | Verified Readiness / Dispatched claim |
| Inspect (recheck) | Readiness / Dispatched, current version | Another read-only probe claim; version/timestamp advance, same correlation and lifecycle IDs |
| Fresh usable result | Same claimed version, exact final fences | Readiness / Complete and success/completion time in one transaction |
| Not usable, timeout, malformed JSON-RPC or wrong application | Claimed read-only probe | Remains Dispatched; return the native observation, no terminal failure or retry |
| Probe/response/process lost | Planned or Dispatched | Original state/key retained; explicit fresh inspection can continue |
| Inspect completed history | Readiness / Complete, current version | Same historical parent; no probe or runtime validation |

Readiness alone permits an explicit repeated Dispatched claim, because its only
external effects are observations. The repository checks the stage before this
exception; it cannot repeat installation, registration, provisioning or Start.
Terminal Needs attention states are not reset or reopened. No timer or status
read issues a probe. A passive parent read remains SELECT-only.

Every claim and completion uses the existing cross-process `BEGIN IMMEDIATE`
and parent version. Duplicate requests for the same version cannot both probe.
An explicitly requested newer inspection can supersede an interrupted/in-flight
read-only observer; the old version can never commit completion. Cancellation
checks and context fences stop stale observers. A process-wide bound of four
explicit probes matches the existing registry's per-process worker bound; a busy
request is refused without changing the parent. No database writer or lifecycle
lease is held across the bounded JSON-RPC wait. Existing reviewed profile timeouts,
poll intervals, HTTP deadlines and response-size limits remain unchanged.

The process-local registry is neither consulted nor populated by this service.
Reopening the database or restarting the application requires fresh native
observations on an explicit call. Completion is a historical point-in-time fact;
a later manual Stop, deletion or pin change cannot make it restart Kodi or erase
the completed parent. Existing parent references continue to protect provision,
Start and authorization records from normal pruning.

## Remaining integration

An incompatible identity, changed projection or unresolved mutation requires
inspection through existing controls; this milestone does not adopt a replacement,
relax a projection fence, retry lifecycle work or add recovery/new attempts.
Unsuccessful native observations are returned to the explicit caller rather than
persisting another redundant result/attempt table.

Later authenticated accept/status/continuation endpoints must enforce ordinary
Web administrator, CSRF/origin and request/version controls. They must route
readiness to this explicit bounded inspection, translate nonusable observations
into a clear Check again action, and leave GET/polling passive. A future runner
must distinguish this read-only recheck from uncertain mutation dispatch. The
guided wizard must keep native completion, screen availability and optional agent
readiness separate. None of those endpoints, runner or wizard is implemented here.

## Automatic Bridge preparation

For new Web requests, native Complete is a native evidence checkpoint. Web Create
then continues a separate exact-target Bridge maintenance receipt and reports
Ready only after reviewed installation, authentication and the requested final
stopped/running state are verified. This adapter remains observation-only;
`ManagedBridgePreparationService` owns the subsequent lifecycle mutations.
Historical native-only receipts are never reopened. See
[managed Bridge preparation](MANAGED-BRIDGE-PREPARATION.md).
