# Version-oriented managed Kodi lifecycle

Issue #238 phase 2 adds `managed_status`, `managed_start` and `managed_stop`.
The ordinary agent loop is **Status → Start(version) → develop/test → Stop(version)**.
Stop keeps the exact writable Kodi profile, settings and installed add-ons.
Separately opted-in [clean Reset](MANAGED-CLEAN-RESET.md) completes the fourth
agent operation. Automatic switching and the four-version Web interface remain
follow-ups. Discover the installed server's tools first; publishing this
source does not upgrade an installed Lab.

## Administrator prerequisites and authority

An administrator prepares an approved, eligible cached runtime using the existing
reviewed runtime installation workflow. No agent lifecycle action downloads,
approves or repins an image. Piers still requires its reviewed preview approval;
Matrix/Nexus retain the catalog's existing legacy eligibility requirements.

Create a custom preset cloned from **Add-on Development** or **Full Lab Control**
and explicitly add **`managed.versions`** under Managed version slots. Assign
that authority to the client and the exact canonical managed target's policy,
and grant that target to the client. Existing presets, `managed.lifecycle` and
existing grants acquire no new authority. Runtime installation normally creates
the canonical target; a completely absent target needs administrator preparation
before it can be granted. An authorized canonical target with no managed pin can
be registered atomically from its eligible cached installation by Start.

This permission gives no access to external Kodi, arbitrary images, Docker,
shells, screen capture/input, sources or workspaces. Source grants, workspace
ownership, deployment authority, Bridge preparation and any Current Kodi
selection remain independent. Ordinary Start/Stop leave development selection,
source, workspace and Git state untouched. The authorized status's `target_id`
is available to existing deployment tools; lifecycle actions themselves accept
only a version and request UUID.

## Calls and results

```json
{"name":"managed_status","arguments":{}}
{"name":"managed_start","arguments":{"version":"omega","request_id":"6ae79f87-9ce7-4fb5-8f06-99372174ea11"}}
{"name":"managed_stop","arguments":{"version":"omega","request_id":"e9e72b97-c6d2-4b09-a334-2639b4c638d4"}}
```

Use `matrix`, `nexus`, `omega` or `piers`. There is no caller-selected target,
container, revision, profile mode, image or Reset parameter.

| Operation | Behavior |
| --- | --- |
| Status | Returns the four supported version labels and each authorized slot's empty, stopped, starting, running or blocked state. Unauthorized entries contain no target, runtime, operation or client identity. It does not start Kodi or renew a hold. |
| Start | Resolves the exact canonical target and current grant. Reuses a valid running profile without another lifecycle start; resumes a stopped profile; otherwise registers/provisions the approved cached exact image through existing services. It never adopts another target's old profile. |
| Stop | Releases only the caller's use/hold, then stops through the existing guarded lifecycle authority if safe. An already stopped or empty slot needs no dispatch. It does not remove a container/profile or clear another client's use. |

Start/Stop return a durable parent `operation_id`, original `request_id`,
`receipt_state`, bounded `code`, typed child operation identities, current status,
and an actionable `recovery` explanation for common blockers. A completed Start
receipt means lifecycle acceptance, **not native readiness**. Wait for
`status.native_ready == true` before native use. The existing readiness worker
may still be checking or may report a native failure. `stop_confirmed == true`
requires the completed Stop intent, current exact identity/epoch and a current
stopped/absent observation; an old receipt or `managed_release` is not proof.

`bridge_readiness` reports stored prerequisites as `blocked` or `unknown` and
never claims a live Bridge handshake. `agent_readiness` is `not_assessed` because
workspace/deployment authority and development readiness are separate. Native
Kodi can be ready while Bridge or add-on development remains blocked. Use the
existing separately authorized preparation/deployment tools to resolve that.

## Replay, concurrency and recovery

Persist a fresh UUID **before** each new Start or Stop. On a lost response,
repeat the identical action/version with the **same UUID**. The principal-bound
receipt fixes its target, original pin/image and child identities. Reuse for a
different action/version is rejected. A completed old Start reports current
status after Stop and never starts Kodi again. Polling/replay never extends the
fixed 15-minute Start hold. Separately authorized existing lifecycle tools can
manage longer holds when needed; they retain their original semantics.

New requests are limited to one per client/version/action per five seconds;
original request replay is exempt. There are at most 1,024 parent receipts per
client and 32,768 globally. These mappings and referenced child history are not
age-pruned, so saturation requires administrator review rather than silent
request-ID reuse. This phase adds no receipt deletion UI.

The coordinator composes existing registration, provision, ordinary Start,
resource Stop, broker admission and reconciliation services. It uses their
cross-process target lock, SQLite claims, exact container/image ownership,
observation epochs and dispatch-time authorization. It does not introduce a
container controller or background scheduler. Broker admission enforces one
running managed Kodi globally and one writable profile per Kodi generation,
including pending/uncertain reservations.

A live operation is returned with its original identity. After a dispatched
child has an unknown outcome, the coordinator observes it and does not dispatch
it again, including after service restart. Inspect/reconcile the original child
in Advanced lifecycle history. Only existing explicit reconciliation proof can
close that uncertainty; the old intent then remains a blocked historical receipt
and a new authorized intent needs a fresh UUID. Administratively disabling an
owner can close its proven undispatched intent without deleting its profile,
clearing holds or touching a reservation.

Common recovery paths:

- **No approved cached runtime:** ask the administrator to prepare/approve it.
- **Another version running:** safely Stop that version with its own exact grant,
  then Start the requested version with a new request UUID. No automatic eviction.
- **Human viewer/input, another client's use, hold, playback or mutation:** wait
  for the owner to finish/release it. This Stop cannot force cleanup or revoke use.
- **Old/noncanonical or duplicate same-version profile:** administrator review;
  there is no silent migration, adoption or deletion.
- **Unknown create/start/stop, stale identity, lease or reconciliation:** review
  original history; neither a new request nor another version bypasses it.
- **Revoked runtime approval:** Start is blocked. Exact authorized Stop remains
  available if ordinary resource safety checks pass.

## Foundation review and compatibility

The review of PR #239 retained its canonical major-version admission, uncertain
provision reservations, atomic broker capacity claim and ordinary Start proof.
One correction removes a retained-mode post-provision rejection caused solely by
another version running while the newly provisioned profile is **stopped**.
Provisioning remains profile-unique; the correction does not permit a second
running environment. Pre-existing legacy singleton paths remain under their
existing exact authority, while the new coordinator requires the canonical slot.

Migration `0003_managed_versions` adds durable request receipts and immutable
intent guards, without backfills, policy changes, new grants or profile changes.
It preserves the prior schema revisions. Downgrade refuses once any version
receipt exists, preventing silent audit/replay loss. Normal approved deployment
must apply the migration; this work does not migrate an installed database.

The coordinator adds no new operating-system lock mechanism. It reuses existing
managed-agent locks and the Linux broker/WSL2 execution boundary. Isolated
SQLite/fake transport and authenticated MCP tests do not qualify native Linux,
Windows/WSL2, real Docker identity, live Bridge, noVNC or an actual agent client.
Those platform checks remain required before release/deployment claims.

## Follow-up boundaries

Automatic switching needs a replay-safe parent that authorizes **both** canonical
targets, proves ownership/use and any Stop outcome, and starts the new version
only after exact confirmed Stop. The present coordinator refuses instead.

Clean Reset is provided by `managed_reset` with separate destructive
`managed.reset` authority in addition to `managed.versions`. See its
[disposal, invalidation and recovery contract](MANAGED-CLEAN-RESET.md).
Ordinary lifecycle permission alone still cannot reset a profile.

The ordinary Web page still needs four clear version entries with Start, Stop,
Reset and Open Screen, human destructive confirmation and Advanced recovery.
Existing viewing/noVNC security and Current Kodi development controls remain.
Issue #238 is therefore **not fully implemented** by this phase.

## Phase 2 focused validation

Each selected test file ran individually through the focused wrapper. Final
successful selections cover **406 passed, 9 explicitly gated skips across 24
files**, plus Ruff format/lint and strict mypy on all 34 changed Python files.
The skipped candidate-transition cases require packaged root transition identity
and remain skipped in this non-root focused lane. Native acceptance is separate.

| Selection | Result |
| --- | --- |
| `tests/application/test_managed_versions.py` | 35 passed: four cached versions, retained profile bytes, reuse, empty Stop, identity changes, denied authority, concurrency, lost responses/process loss, cached-image absence, readiness, independent holds/viewers, source/workspace/Git preservation and conflicting historical profiles |
| `tests/web/test_managed_versions_mcp.py` | 9 passed: authenticated MCP, strict schemas, exact target/client/credential authority, no raw lifecycle identities or Reset input |
| `tests/storage/test_managed_version_operations_migration.py` | 11 passed: direct predecessor upgrade, preserved permissions/policy, immutable bindings, request uniqueness and nonlossy downgrade |
| Capacity and MCP foundation files | 14 + 17 passed, including retained stopped provisioning while another version runs |
| Six shared lifecycle/resource/legacy-agent files | 73 passed |
| Existing `tests/web/test_mcp.py` | 24 passed |
| Twelve affected migration/transition/package files | 223 passed, 9 gated skips |

The first registry compatibility run found a stale expected tool count (31 versus
34); it was corrected and its entire file passed. Final static checking also
caught the SQLAlchemy DDL constructor's missing typing, handled consistently with
the existing trigger declarations. No complete release suite, real runtime,
installed database, deployment or expensive native acceptance was exercised.
