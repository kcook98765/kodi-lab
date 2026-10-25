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
