# Physical source-capture acceptance checkpoint

On 2026-10-01 UTC, the reviewed source-free candidate completed the physical
v2 source-capture workflow on the real external-development Nexus `mini`.
**Result: PASS through exactly verified MCP execution.** This is acceptance of
that exact workflow and candidate, not a release or managed-workspace promotion
result. No credential value is part of this record.

## Accepted evidence

| Evidence | Exact identity and result |
| --- | --- |
| Candidate source commit | `301b75f71dc0038947c9d8b631ab5210e308c9e5` |
| Candidate BUNDLE | `4f251f4d3cdf43624dd73a023f9d4c4fc6d73e22d019ad16857c71589ba7eff7` |
| Installed database head | `0059_workspace_validation_digest` |
| DevelopmentSource | `bf64851c-c721-412c-91e6-d1bc749cb864` |
| Workspace | `515d8c96-661a-48f6-8d46-d2295ec364e0` |
| Physical target | `mini`, `931888f1-dd4a-456f-851c-d17004a3b488`, Kodi 20.5 Nexus, bridge 1.1.13 |
| Add-on | `plugin.video.kodi-lab-physical-acceptance` |
| Creation baseline, generation 0 / v1 | `sha256:cd578732ac24c129ca57735efe3d4acce92a873301172f58a03e53534d7b6128` |
| Active capture, generation 1 / v2 / version 1.0.1 | `sha256:40c1cfadba086cc0d7ec38843a79ce411119133e5f3d35ff4aa93cfb8ce82a8a` |
| Capture operation | `5b50b4a0-17f6-4a27-9e74-09ecca1d6c76`, succeeded, generation 0 to 1 |
| Generation-1 validation | `8ff8e2b3-57f7-41e3-8766-aa9fb40a6265`, passed at the generation-1 digest |
| Repository deployment | `5fd69f6e-d047-4076-b015-f725d040ff05`, succeeded after exact Web Recheck |
| Publication | `7191856d-625c-4de5-b8a9-b48a7f1463a9`, version 1.0.1; ZIP `sha256:19d8fd0a680668852b067e0723ebf18e48c92ff21f841f634fdcec65fe92f4c7` |
| Exact installed state | Web Recheck `repository_verified` against the generation-1 tree digest |
| MCP execution | `9a79c8ef-a9b9-41e4-9b4d-34031e92883f`, succeeded, `smoke_passed` |

Historical v1 evidence remains retained: validation
`5b82a232-3a27-4e03-93b2-be9fa570c0af`, publication
`8440f210-e369-4abf-b7a1-68cdf3e34ed7`, succeeded deployment
`50535574-3f2f-44d4-bd97-b5da99d5ee7b`, and execution
`2a16e001-2c16-47a1-b0e2-28df62881a48`.

The operator edited the external source to v2, ran authenticated Web Scan and
Capture, approved the exact prepared publication in Web, manually updated Kodi,
and observed `Kodi Lab Physical Acceptance v2` on mini. The served development
feed advanced to 1.0.1. Authenticated MCP performed generation-1 validation,
repository deployment preparation and execution. Web Recheck proved the exact
installed generation-1 tree before MCP execution. The creation baseline and
historical generation-0 validation, publication, deployment, installation and
execution evidence remained retained.

## Observed workflow contract

1. Scan evidence is response-scoped and read-only. An ordinary later page load
   does not continuously monitor the external source and reports it as not
   freshly scanned.
2. Capture re-reads the whole external source. The changed v2 source advanced
   the existing workspace exactly once, from generation 0 to 1, while retaining
   the v1 creation baseline. The old generation-0 validation became stale.
3. `workspace_validate` is authenticated MCP validation of an exact workspace
   generation and tree digest. There is currently no equivalent Web validation
   action.
4. Repository-mode `workspace_deploy` reads target preconditions through native
   inventory and paired-bridge inspection. It prepares an unpublished 1.0.1
   publication and target-bound deployment before reaching `awaiting_approval`.
5. Human Web approval makes that exact publication live in the served
   development feed and advances deployment to `awaiting_kodi_update`. Kodi
   update/install remains an explicit human action.
6. Authenticated Web **Recheck installation** compares native identity with the
   authenticated bridge's complete installed tree. The exact match completed
   deployment as `repository_verified`. Only then did authenticated MCP
   `workspace_execute` run the fixed smoke action against that verified
   generation-1 deployment, producing `smoke_passed`.

## Separate deferred managed-target state defect

During acceptance review, recovered managed Omega was live, running and usable
on historical runtime revision 1, while persisted
`managed_instances.runtime_state=stopped` and
`targets.availability=unavailable`, `targets.health=warning`,
`targets.connection_state=disconnected` remained stale. Normal managed Start and
status observation did not reconcile these durable fields; the Web's live
running/responsive status came from broker/runtime observation. Ordinary managed
lifecycle operations use live broker state and were not shown compromised.

Managed-workspace promotion eligibility does consult the persisted target
available/healthy/connected fields and can reject a healthy managed target.
The discrepancy existed before this source-capture candidate's Gate 4 upgrade;
the upgrade did not cause it. It did not invalidate the external-development
mini source-capture flow. Review and fix it separately before
managed-workspace promotion acceptance. This checkpoint makes no Omega state
change and no claim that promotion has passed.

Source follow-up: migration `0063_managed_runtime_projection` and D-0402 add
ordered managed current-state convergence. This historical checkpoint remains
open for a separate development-candidate and live Nexus projection acceptance;
the source change alone does not prove live repair or promotion eligibility.
