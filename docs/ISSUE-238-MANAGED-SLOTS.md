# Issue #238: managed version slots

This document records the admission-foundation campaign. Subsequent layers are
[agent version lifecycle](MANAGED-VERSION-LIFECYCLE.md), [clean Reset](MANAGED-CLEAN-RESET.md)
and the [four-version Web interface](MANAGED-KODI-WEB.md). Automatic switching and
native/platform qualification remain open. Historical audit/findings below retain
their original phase scope.


## Current-main audit

Starting main: `71e6a8a946858e4b9738bc21d1294baca594b90c` (PR #237).
This campaign uses an isolated source worktree, fake brokers and fixture profiles.
It does not access installed runtime/profile state, merge or deploy.

Reviewed issue #238 and closed #51, #196, #12, #26, #47, #49 and #152.
The new owner direction supersedes multiple-profile and configurable-concurrency
workflow choices, while retaining their security/recovery machinery.

| Existing capability | Reuse / required change |
| --- | --- |
| `ManagedInstanceService`, `runtime/install.py`, `runtime/recipes.py`, `runtime/lab.py` | Keep exact approved runtime registration/selection. Current recipes define four canonical targets; older numeric selectors and experimental Omega remain historical identities. A future coordinator must resolve the canonical identity without adopting an old profile. |
| `ManagedRuntimeLifecycleService`, `ManagedInstanceLifecycleOperationCoordinator`, `managed_capacity.py`, `managed_start.py` | Keep durable claims, mutation leases, exact broker proofs, container preconditions and readiness. Add shared version-level admission; make the effective running cap one across ordinary and development starts. |
| `retained_development.py`, `DevelopmentSessionService`, Current Kodi | Keep profile identity, inventory and recovery checks. Retained admission currently requires a selected bound workspace/session; ordinary version Start must become independent of that development selection. |
| Create Kodi services/repository/runner | Keep durable parent/child correlation and lost-response checkpoints. Compose approved cached registration/provisioning; agent Start must not invoke installation/download/approval. Human installation can remain Advanced. |
| `ManagedAgentLifecycleService`, MCP registry/authorization/administration | Keep separately opted-in `managed.lifecycle`, exact target grants and immutable request binding. Existing ensure-ready/release operates on already provisioned target UUIDs. Add version-oriented operations only when their complete coordinator and replay contract exist. |
| `DisposableInstanceResetService`, reset operations/status, reconciliation | Keep exact stop/remove/create, durable phases and invalidation. Existing Reset can apply a fixture and briefly run Kodi; a clean version Reset needs explicit semantics, active-user admission, a distinct scoped permission and durable parent identity. Do not simply expose existing Reset through MCP. |
| Managed Bridge trust/preparation and capability prerequisites | Keep container-bound trust, reviewed build pins, invalidation and native identity verification. Native usable is separate from live Bridge capability and workspace/deployment readiness. |
| Workspace/source bindings and `mcp_workspace_development.py` | Keep exact source/workspace/client grants and profile-bound deployment lineage. Stop must preserve all bytes; Reset must invalidate profile evidence while preserving independent source/workspace/Git/evidence. |
| `/kodi`, instance details, managed resources, noVNC | Keep authentication, CSRF, exact viewing tickets, view-only ownership and recovery links. Replace ordinary registration/profile-mode/session setup with four slot entries only when all four actions are supported. Keep diagnostics, installation and reconciliation in Advanced. |

For the eventual ordinary UI, remove instance naming, retained/disposable mode
selection, manual provision stages and workspace binding as prerequisites for
Start/viewing. Compose the existing lifecycle and viewer behind the version
actions. Retain catalog approval/acquisition, exact registration/pins, development
selection, diagnostics and recovery in Advanced. Historical records are retained;
removing a workflow control does not authorize deleting its state.

## First independently reviewable slice

This change establishes broker admission prerequisites, not the complete public
four-operation workflow:

- A shared logical slot description resolves Matrix, Nexus, Omega and supported
  Piers preview to the current reviewed canonical target identities.
- Profile uniqueness is keyed by Kodi major generation, across different target
  IDs, runtime revisions, historical releases and experimental identities.
  Registrations and historical receipts without a writable runtime do not count.
- Provision admission checks observed profiles and durable provision reservations
  under the existing SQLite writer transaction. An in-flight or uncertain create
  for the same version blocks another target. Lost outcomes remain reserved.
- Start also refuses a conflicting same-version profile. Neither path removes,
  adopts, repins or chooses between pre-existing duplicates. Exact stop/removal
  and history remain available through existing administrator recovery.
- Effective running capacity is fixed at one for all broker starts, regardless
  of an older stored limit. Reading policy preserves the recorded old value;
  the settings UI/API reports the effective policy and cannot raise it.
- All existing catalog eligibility, mutation, ownership, session, Bridge and
  reconciliation checks continue to apply. No permission or preset is expanded.
- Web conflicts return 409 with actionable reasons; unverified identity remains
  unavailable. MCP preserves the exact refusal code in its replayable receipt.

Legacy singleton targets still use their exact existing lifecycle paths; they
are not converted to canonical slots. Canonical-only registration and the new
coordinator are follow-up work. No schema migration or installed-state conversion
is part of this slice.

## Remaining implementation

1. Durable version-oriented Status/Start/Stop/Reset coordinator: absent-profile
   cached preparation, exact retained reuse, safe sequential switching, immutable
   principal/request/version binding and recovery across process restarts.
2. Separate destructive `managed.reset` authority, explicit custom-preset opt-in,
   exact target/version grant checks, and active-user/mutation/reconciliation
   checks immediately before every destructive dispatch. No automatic grants.
3. Clean Reset semantics (including fixture policy), profile-bound Bridge and
   deployment invalidation, and optional restart without deleting independent
   workspaces/sources/Git or selected retained evidence.
4. Four-action MCP surface and compact four-version Web page, with human Reset
   confirmation, actionable recovery and existing secure Open Screen behavior.
5. Focused coordinator/MCP/Web tests for the complete acceptance matrix.

Issue #238 remains open. The existing agent tools are still
`managed_ensure_ready` / `managed_release`; version Start/Stop/Reset are not
advertised as available by this change.

## Qualification boundaries

Focused fake-broker and SQLite tests cover the implemented admission boundaries.
They do not establish native or graphical acceptance. After the full coordinator
lands, native Linux and Windows/WSL2 acceptance must exercise approved cached
empty-slot Start, exact Stop/Start profile retention, clean Reset/trust recovery,
safe Omega/Nexus switching, secure first-viewer rendering/cleanup and genuine
agent deployment/Bridge readiness. Expensive cross-version/native acceptance and
the eight-stage release suite are deferred by campaign authorization.

## Changed files and focused results

Implementation is limited to:

- `storage/managed_version_slots.py`: logical slot identities and read-only
  version/profile/reservation checks.
- `broker/managed_capacity.py`, `broker/operations.py`: existing atomic
  provision/start admission, exact postconditions and actionable refusal reasons.
- `storage/managed_activity.py`, `application/managed_resources.py`: effective
  one-running policy and settings validation, preserving older recorded values.
- `application/managed_agent_lifecycle.py`: preserve pre-dispatch refusal codes
  in existing MCP receipts without redispatch.
- `web/app.py`, `web/templates/managed-resources.html`: conflict HTTP status and
  fixed-limit presentation. Viewer and input ownership are unchanged.

Paths above are under `src/kodi_lab/`. README, the agent quick start, managed
resource guidance and DECISIONS explain the staged behavior and compatibility
boundary. Unknown/unclassifiable managed inventory blocks new work pending
review; no automatic cleanup is attempted.

The final `./scripts/check-focused` invocation ran each file below individually,
then focused Ruff formatting/lint and mypy over all seven changed Python source
files. **FOCUSED PASS: 135 passed across 12 test files, 83.0 seconds.** The Web
resource test also invokes the Node DOM harness. No tests were skipped.

| Test file under `tests/` | Passed |
| --- | ---: |
| `broker/test_managed_version_slots.py` | 16 |
| `broker/test_managed_capacity.py` | 11 |
| `broker/test_development_session_admission.py` | 1 |
| `application/test_managed_instance_capacity.py` | 13 |
| `application/test_managed_resources.py` | 12 |
| `application/test_development_sessions.py` | 14 |
| `application/test_retained_development.py` | 26 |
| `application/test_managed_start_selection.py` | 18 |
| `application/test_managed_agent_lifecycle.py` | 12 |
| `web/test_managed_resources.py` | 6 |
| `web/test_managed_agent_lifecycle.py` | 3 |
| `web/test_managed_slot_conflicts.py` | 3 |

Coverage includes empty provisioning and exact reuse, all-four stopped profiles
and sequential switching, saved limits of 1/2/4, simultaneous ordinary Web/MCP
lifecycle in both orderings, duplicate target/release/revision admission, pending
and uncertain create reservations across service restart, lost replies, exact
container identity, immutable refusal replay, existing permission denial and
source/workspace/session/profile preservation. Native readiness is provided by
explicit fake transports/coordinators and is not native acceptance. The new
version Reset, its destructive permission and clean-profile/trust acceptance
cannot be qualified before their implementation.

One intermediate legacy test exposed changed error precedence; admission was
ordered after existing session checks, preserving its original recovery reason.
The final focused run above passed on the corrected source.
