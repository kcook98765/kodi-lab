# Current-main dependency audit for #54

Audit baseline: `e198592b634bc5bc2cf876fd5fbded88b7cd827d`, fetched and
compared with GitHub on 2026-10-25 UTC (2026-10-24 Pacific). All #54 feature
issues and comments were read, together with the directly relevant completed
foundations #7, #12, #16, #20, #24 and #28–#30, and the remaining Windows
acceptance issue #8. Full issue snapshots accompany the local evidence named
in [the progress ledger](PROGRAM-54-LEDGER.md).

An existing, preserved branch, `feat/nexus-closed-log-retention-20261023`, has
two commits outside `main`: `09d847e` (closed Nexus logs) and `3dddbc2`
(bounded Docker output logs for newly provisioned containers). Its 22-file
change overlaps #50's runtime/log-retention foundations. It has no open PR at
audit time. Do not duplicate, amend, merge or deploy that work as part of this
program; review its exact status before a later diagnostics increment.

This is a source audit. Historical incident reports are not a reproduction or
an assertion about current installed state. The kickoff authorizes code-only
implementation and PRs, superseding the earlier review-only handoffs; it does
not authorize deployment, security-permission changes or live recovery.

## Dependency order and dispositions

| Issue | Current-main finding | Disposition and next increment |
| --- | --- | --- |
| #54 | Focused/integration/release lanes and several test optimizations already exist. | Establish new measurements, keep a ledger, optimize measured setup costs without changing test selection or assertions. |
| #17 | Managed stopped-tree recovery, Web/CSRF action, retained phase/cause, history and migration 0078 already exist. | Reuse. Remaining original-case/remote acceptance and exit-139 investigation are external evidence gaps; do not recreate the old incident. |
| #44 | Both application and broker recovery guards exclude new terminal `failed` operations. Trusted prior-copy selection excludes uncontained failures, correctly preventing silent adoption. | Independent correctness priority. Permit only a bounded, explicit stopped-tree inspection after verified copy; keep the original operation failed, retain its cause, and require fresh full-tree and runtime binding proof. A later no-write conflict must not erase the original evidence. |
| #45 | Import supports digest-bound atomic add/replace; `deletion_unsupported` and `private_edit_overlap` remain deliberate policies. | Keep separate from #44. Design explicit reviewed removals/conflict resolution and retained lineage before implementing exact-tree staging. A fresh Kodi profile cannot solve workspace conflicts. |
| #46 | Deployment admission collapses missing/stale baseline to `deployment_not_allowed`; MCP discards application error reasons. | Add precise compatible reasons first; then a shared strictly read-only exact-target assessment. Do not use a status getter that converges state or schedules readiness work. |
| #49 | Native readiness and reviewed Bridge trust are independent. `prepared` is committed before staging completes; the browser calls it installed and polls without a bound. | Independent UI correction can report uncertainty and bound polling without new grants. Shared capability projection and explicit lifecycle continuation need their own reviewed increment. |
| #50 | `log_read` uses the resident Bridge and cannot provide stopped-container crash evidence. | Plan fixed artifact/exit-metadata reads with exact container and operation binding, bounded private retention, and no wake/start. No arbitrary path, shell or fabricated crash. |
| #51 | Canonical per-version targets, exact approved cached runtimes, reset, lifecycle, resource admission, leases and agent ensure/release exist. No single-session coordinator or provisioned-container exclusivity exists. | Compose existing controls behind an opt-in, Web-owned session after recovery/preflight foundations. Never enroll or remove existing unrelated containers implicitly. |
| #47 | Internal instance creation exists; generic stable duplicate-instance Web workflow does not. | Defer per the latest issue direction. Do not close automatically; reconsider only for a concrete need beyond one session. |
| #48 | Full profile snapshot/restore exists; selective settings migration is a distinct compatibility/secret-handling feature. | Defer per the latest issue direction. Do not read private settings or restore code/trust implicitly. |
| #52 | No dedicated active-development-add-on settings operation found. Application settings RPC is a different operation. | Audit-only dependency slice initially: bounded reviewed Bridge API and exact deployed workspace identity, ordinary values only, per-key outcomes, no arbitrary add-on/Python/path input. A new grant requires separate authorization. |
| #53 | Existing native ZIP service provides reviewed artifact delivery and GUI-install observation, not a parameterized unattended native installer. | Separate feasibility research. Reuse artifact/identity/receipt protections; do not rebrand direct extraction or opening a dialog as native installation. No new installation authority. |
| #18 | Authenticated view-only PNG WebSocket and live-display limits exist; instance UI exposes still captures. Live gate is narrower than screenshot support. | Later, independent optional preview work. Managed policy/revocation design and native resource/latency acceptance remain prerequisites to broader claims; no extra services or audio work. |
| #8 | Windows/WSL installer and guide already describe the operational platform. | Keep open for fresh-account/host acceptance. Linux fixtures cannot supply native Windows evidence. Update walkthrough only when a new flow is adopted. |

## Current implementation map

Paths below are relative to `src/kodi_lab/` unless prefixed otherwise.

- Test lanes: `scripts/validation_tiers.py`, `scripts/check`; ownership/resource
  evidence: `scripts/test_scratch.py`, `tests/efficiency_plugin.py`; closed ORM
  templates: `tests/efficiency.py`; actual Alembic transitions:
  `tests/storage/test_migrations.py` and the dedicated migration modules.
- Recovery: `application/workspace_deployment_recovery.py`,
  `broker/development_sources.py::resolve_workspace_reconciliation`,
  `broker/docker_inventory.py` stopped destination inspection,
  `storage/workspace_deployments.py::prior_workspace_copy`,
  `application/reconciliation.py`, `docs/MANAGED-DEPLOYMENT-RECOVERY.md`.
- Staging: `application/workspace_imports.py`, `workspace/batch_import.py`,
  `workspace/storage.py`, `application/workspace_source_captures.py`.
  Apply already holds the workspace coordination lock, rechecks source and
  generation, stages a complete tree, and publishes one durable generation.
- Deployment: `application/mcp_workspace_development.py`,
  `mcp/authorization.py`, `mcp/tools.py`, `mcp/transport.py` and broker copy
  resolution. Authorized scope and technical prerequisites must remain distinct.
- Native/Bridge readiness: `application/runtime_status.py`,
  `application/readiness_tasks.py`, `application/managed_bridge_trust.py`,
  `application/managed_observation_bridge.py`,
  `application/managed_launch_bridge.py`, `web/static/reviewed-bridge.js`.
  Stored active trust is not fresh authenticated capability evidence. Managed
  direct copy, native remote actions and X11 screenshots have separate gates.
- Session foundations: `application/managed_instances.py`,
  `application/runtime_lifecycle.py`, `application/reset.py`,
  `application/reset_operations.py`, `application/managed_agent_lifecycle.py`,
  `application/managed_resources.py`, `broker/managed_capacity.py` and runtime
  matrix/installation services. Existing managed lifecycle authority does not
  confer create/delete/reset or reviewed-install authority.
- ZIP: `application/native_zip_installations.py`, uploaded ZIP and repository
  artifact services, fixed native observation and inventory. Workspace deployment
  remains the source-iteration path. Native package-install qualification is
  separate.

## Minimal single-session design constraints

One canonical target per generation avoids a new target pool and preserves
exact runtime pins and grants. `max_running=1` only counts non-stopped inventory
plus start reservations in `broker/managed_capacity.py`; it does not exclude a
second provisioned stopped container. It therefore cannot implement #51 alone.

An opt-in durable session must bind an explicitly selected set of development
targets, current target/container/runtime and session generation. Provision and
start admission across every entry point must share the session reservation;
checking only the new page has a race with Advanced controls, agents and reset.
Preserve unrelated targets and fail with their conflict rather than enrolling,
evicting or deleting them. Unknown inventory, retained reservations, mutation
leases and reconciliation are blockers, never evidence of an empty slot.

The Web workflow should show version, active add-on, native and agent readiness,
Resume/Stop and explicit Fresh Test/Switch actions. Reuse approved cached runtime
eligibility and existing lifecycle operations. Source edits stay in the same
profile; fresh preparation/version switching is an explicit replacement action.
Do not reacquire images or reset for every edit.

Before a switch: recheck selected evidence retention and data-loss confirmation,
current target/container/generation, all relevant leases, and recovery status.
Retire the old container only through supported, separately authorized lifecycle
actions; verify absence before provisioning a replacement. If retirement or
preparation is uncertain, preserve durable phase/evidence and block another
provision. Workspace/source/Git history and selected evidence remain external to
the disposable profile. New target generations inherit no Bridge credentials,
deployment receipts or source baseline confidence. No automatic idle deletion.

Code can be reviewed and fixture-tested here, but no existing container will be
reset, removed, switched or newly provisioned during this program without the
operator's separate authorization. Native version-switch and Windows acceptance
remain explicit later qualifications.

## Reviewable sequence and validation

1. Measured test-only setup improvement, keeping exact template checks,
   independent private clones, production-strength auth tests and actual
   migration proofs. Matched focused baseline; broader checks after settlement.
2. #44 recovery eligibility and preservation tests spanning application, broker
   and Web. Full stopped-tree/ownership proof remains authoritative. Cover later
   no-write conflict, replay after restart, changed generation/container/runtime,
   missing/partial evidence and concurrent operations. One release gate on
   settled shared recovery code is warranted; no native incident manufacture.
3. Independent #46 safe diagnosis and #49 truthful bounded UI status increments,
   followed by shared read-only assessments. Enforce unauthorized-no-probe,
   no-state-writes, no task scheduling, bounded output/deadline and race rechecks.
4. #45 reviewed removal/overlap design, then atomic implementation and
   generation/history/recovery tests. Never force-import private edits.
5. #50 bounded diagnostics before disposal; #51 session/admission persistence,
   Web orchestration and race/failure tests, then separately authorized sequential
   native qualification. Do not hide unresolved recovery behind fresh profiles.
6. Optional #18/#52/#53 work after their policy/API constraints are resolved.
   Windows/WSL and per-runtime acceptance remain separately reported.

No automatic issue closure, merge, deployment, security-grant migration,
production mutation or deletion of existing resources follows from these PRs.
