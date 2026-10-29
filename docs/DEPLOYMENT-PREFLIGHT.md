# Read durable deployment prerequisites

`workspace_deploy_preflight` reads one exact workspace, expected generation and
target before any deployment request. It uses the existing `workspace.deploy`
permission in both client and target presets, the workspace binding, source grant,
target grant, current credential and production veto. It rechecks authority before
returning. No preset or existing grant is changed. The authenticated Web endpoint
uses the same application service:

```text
GET /api/agent-workspaces/{workspace_id}/deployment-preflight
    ?target_id={target_id}&expected_generation={generation}&deployment_mode=auto
```

This first increment reports **durable prerequisites**. It separately identifies
workspace state/generation, validation generation/digest, source identity and
attachment, baseline confidence/runtime, instance/global leases and typed
reconciliation. Unknown validation digest stays unknown. A usable prior workspace
receipt is distinguished from a stale source baseline, and still needs exact
container proof. A verified-at-last-operation baseline is historical evidence,
not proof of currently installed bytes. Existing stopped-tree verification and
mutation admission remain authoritative.

The verdict is `blocked` or `unknown`; this service never returns `ready`.
Fresh native state, enabled add-on state and live authenticated Bridge readiness
are explicitly unobserved. Stored Bridge trust is projected independently and is
never presented as authenticated liveness. Bridge absence is not a managed direct
deployment blocker. External/repository assessment is unsupported/unknown rather
than inheriting direct managed semantics. Future bounded observations can extend
this foundation without calling the converging runtime-status getter.

Next actions state agent versus Web administrator authority and running/stopped
preconditions. Baseline preparation points to the existing stopped copy workflow;
busy states retain their reservations and direct the administrator to target
history. Global blocker IDs and another client's operation identifiers are not
exposed. A status read never retries, copies, enables, validates, repairs, installs,
starts, stops or schedules tasks. It creates no request/operation/reservation or
runtime projection. Normal MCP transport security auditing remains in force.

Preflight is not a reservation or a guarantee of later success. Mutation paths
retain every existing recheck and lease. A preflight disagreement such as a stale
validation digest is diagnostic here; this increment does not silently change the
existing deployment predicate. Reconciliation must be resolved through supported
recovery, independently of any fresh-profile development workflow.

An uncontained terminal inventory failure after a verified workspace copy is
reported as `deployment_provenance_review_required` when it is the latest retained
completed copy for this workspace/source/runtime and has no newer source baseline.
Copy chronology uses completion time; a tied uncontained failure still requires
review. The source-baseline check remains separate, preserving its own confidence
and runtime reasons.
A later no-write replacement conflict does not hide that evidence. The next action
is Web administrator review of retained history, rather than ordinary source-copy
advice. This remains an `unknown` prerequisite: preflight neither proves which
bytes are installed nor enrolls the failed operation for recovery. Exact historical
admission and stopped-tree inspection remain separate administrator actions.


When a disposable development session is selected, managed direct preflight also
checks its stored bound target/runtime/workspace/source and pending provision or
start reservations. A mismatch returns `session: blocked` plus an administrator
`review_session` link. `stored_session_scope_matches` is a database scope check;
it does not observe the current container, native Kodi, Bridge or enabled add-on.
Mutation-time admission and exact observed-container checks remain mandatory.
External-development and inactive-session behavior retain their separate scope.

Web administrators can open a ready workspace's **Deployment prerequisites**
section, choose a managed disposable Kodi and explicitly request the same stored
assessment. The matching Current Kodi selection is preselected when present;
choosing another target does not change that session. The page explains blockers
and unknowns and links to existing target history, setup or Current Kodi controls.
It never reports a ready verdict or performs an agent permission check. Reads
remain separate from stopped source copy, recovery and fresh deployment admission.
After editing or capturing, reload the page to assess its new generation.
