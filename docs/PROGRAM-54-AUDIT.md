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

## Additional single-session implementation findings

`runtime/lab.py::canonical_id()` derives curated targets with UUID5 and reviewed
recipe targets with `recipe_target_id()`. They are distinct exact identities;
`ManagedInstanceService._validate_selection()` rejects an incompatible release.
Use a logical session pointer over those identities instead of repinning a
single target across Kodi releases. `runtime_lifecycle.py::_deprovision()`
requires stopped state and invalidates source/ZIP evidence and Bridge trust.
`reset.py::_reset_locked()` already sequences stop, invalidation, deprovision,
absence verification and exact-pin provision; it does not provide cross-target
exclusivity. Reusing its phases requires durable operation ownership, not simply
calling reset from a new page.

The smallest proposed coordinator persists one logical session generation and
its exact target/runtime/container, selected source/workspace and current typed
operation/phase. An opt-in policy must be checked by provision/reset and every
start path, including Advanced Web controls, agent lifecycle, deployment restart
and the broker admission boundary. A page-level inventory check has a race with
those paths. Existing unrelated provisioned containers should cause a clear
conflict, preserving them until separate operator action; their stopped state
does not make them disposable by this session.

Suggested durable sequence: review prerequisites/evidence and data loss → claim
the session generation → stop/verify the old exact container → retain selected
evidence → retire through supported lifecycle → prove absence → provision the
chosen approved cached exact runtime → establish native and reviewed Bridge
readiness independently → perform stopped source baseline copy → start/enable →
deploy the validated workspace. Each irreversible dispatch needs a persisted
phase first. An uncertain dispatch remains blocked for inspection; never
provision another container on an assumed timeout. Stop/resume alone retains the
same profile. Only explicit Fresh Test/Switch advances to a replacement profile.

No target/source/workspace grant is inherited by selecting a version. Existing
`managed.lifecycle` only resumes/releases an already provisioned granted target;
it does not become create/reset/delete authority. Reuse Web administration for
preparation. Tests must race two prepares, Advanced provision vs prepare, agent
start vs switch, restart after each dispatch marker, changed identity and
uncertain retirement, and prove that workspace/history/evidence survive. New
durable vocabulary needs its own migration and migration-transition proofs.

## Convenience API feasibility review

This additional review is read-only. No settings/ZIP capability, grant, Bridge
build, target configuration or installation was changed.

For #52, `ManagedLaunchBridgeService.binding()/call()` and managed Bridge trust
already supply reviewed identity/generation rechecks; `narrow_bridge.py` has a
closed operation set and no add-on-settings setter. A future operation should
derive the active deployment's add-on and target from a durable session binding,
not guess the latest deployment when a workspace has several version histories.
It needs an explicitly reviewed runtime-settings permission; workspace file-edit
authority alone is not that permission. A first slice can accept a bounded set
of artifact-reviewed ordinary setting IDs/types, use a fixed Kodi API call,
return per-key application/readback outcomes without batch-atomicity claims,
exclude secrets and avoid values in audits/replay caches. Do not install,
enable, restart or navigate as a side effect.

Matrix already exposes typed add-on settings accessors; Nexus introduces the
`getSettings()` wrapper and deprecates older typed accessors. This supports a
small version-aware adapter proposal, not a runtime compatibility claim.
Primary sources:
[19.5 Addon.h](https://github.com/xbmc/xbmc/blob/19.5-Matrix/xbmc/interfaces/legacy/Addon.h),
[20.5 Addon.h](https://github.com/xbmc/xbmc/blob/20.5-Nexus/xbmc/interfaces/legacy/Addon.h).
Qualification still needs each exact supported Bridge/runtime pin, denied/stale
scope, partial failure, per-key readback and secret-exclusion tests.

For #53, `InstallFromZip` ignores its parameter vector and enters the Add-on
Browser dialog in the checked official source tags:
[19.5](https://github.com/xbmc/xbmc/blob/19.5-Matrix/xbmc/interfaces/builtins/AddonBuiltins.cpp),
[20.5](https://github.com/xbmc/xbmc/blob/20.5-Nexus/xbmc/interfaces/builtins/AddonBuiltins.cpp),
[21.3](https://github.com/xbmc/xbmc/blob/21.3-Omega/xbmc/interfaces/builtins/AddonBuiltins.cpp),
[22.0rc1](https://github.com/xbmc/xbmc/blob/22.0rc1-Piers/xbmc/interfaces/builtins/AddonBuiltins.cpp).
The documented Addons JSON-RPC namespaces provide enumeration, execution and
enablement, not a ZIP-install method:
[Matrix](https://kodi.wiki/view/JSON-RPC_API/v12#Addons),
[Nexus](https://kodi.wiki/view/JSON-RPC_API/v13#Addons),
[Omega](https://kodi.wiki/view/JSON-RPC_API/v13.5#Addons),
[Piers](https://kodi.wiki/view/JSON-RPC_API/v13.11#Addons).

Inference: one unattended native ZIP install cannot be built by adding a path
argument to that builtin or wrapping the existing JSON-RPC add-on methods.
Current Lab native delivery is narrower still: `NativeZipInstallationService`
validates passive repository artifacts, requires an absent destination and
independent `addon.install` authority, and observes operator-driven installation.
It does not upgrade an arbitrary active workspace add-on. Preserve that path as
the supported assisted alternative. A proposed one-call workflow would need a
separately reviewed bounded GUI route or an upstream supported API, artifact/
workspace lineage and upgrade semantics, and real prompt/dependency/version
qualification. Opening the picker, downloading a ZIP or extracting its files
does not establish native completion. No such unattended route was implemented.

## Next exact-tree staging slice (#45)

Keep the current add/replace contract as the default. A deliberately reviewed
deletion mode should bind each removal to its current preimage, the source
snapshot, workspace generation and preview digest. Start with tracked ordinary
files; preserve manifest identity, type/mode limits, quotas and grants. Deletions
count toward the bounded batch. Unsupported directory transitions must remain
explicit instead of leaving empty directories while claiming an exact tree.

`WorkspaceImportService._changes()` currently compares tracked/source paths and
rejects removals before overlap handling. An exact-tree assessment must also
include private-only paths. Default overlaps remain rejections. Any explicit
keep-private/take-source resolution must be included in the reviewed digest and
rechecked against the private preimage at apply time; a kept private path means
the result is a reviewed merge, not an exact source tree.

`workspace/batch_import.py::switch_import_tree()` currently accepts only byte
replacements. Extend its internal staged operations with bounded removals, using
the same descriptor-bound parent traversal and atomic full-tree exchange.
Delete only in the private staged copy, retain the old tree and original
baseline, and prove complete expected results before publication. An uncertain
database publication retains both trees and reconciliation rather than guessing
rollback. Existing import-history reconstruction assumes every postimage is a
file; removals need explicit tombstone handling and versioned lineage instead of
constructing a fictitious file entry with a null hash.

Acceptance should reproduce the synthetic 80→78 shape (69 unchanged, 7 modified,
2 added, 4 removed) with exact hashes and retained prior history, plus explicit
private conflicts, stale source/generation/preimage, concurrent import/edit,
unsafe path/type/mode, interrupted exchange/publication and replay. Reuse
`tests/application/test_workspace_imports.py`, `test_workspace_import_limits.py`,
`tests/web/test_workspace_import_mcp.py` and
`tests/storage/test_workspace_import_migration.py`. This is the next proposed
implementation slice, not an implemented API or native acceptance claim.
