# Program 54 Phase 2 source status and remaining decisions

This is the current source-scope checklist, not installed-Lab or native
acceptance. Historical milestone entries in the audit, integration review and
ledger retain their original dates and qualification boundaries.

## Qualification

- Latest verified combined **RELEASE PASS**: `c788a31`, tree `4211e3d`,
  5,703 passed / 38 gated skips / 34m09s, all eight stages, source manifest and
  owned-scratch cleanup verified. It includes source through #79.
- Settled composition `ba215e6`, tree `d6b7669`, is **RUNNING**, with no result
  yet. It includes #80's guide, #81 and final #82, plus report snapshot `5420c39`.
  All 1,817 tracked entries and 11 changed-file SHA256 values are pinned.
  The qualified functional ancestor is `6aaa097`; `c788a31` adds only two report
  files to that ancestor. Do not claim false ancestry or transfer its result.
- Failed `557dcdd` remains failed (5,617 passed / 1 historical-fixture failure /
  38 skips / 32m09s). Its later correction does not rewrite that evidence.
  Earlier `cbc8894` (5,515 / 38 / 32m12s) and Phase 1 `02c4054`
  (5,234 / 38 / 29m59s) remain separate exact successful pins.

## Kickoff requirements

| Requirement | Current authoritative evidence | Scope/status |
| --- | --- | --- |
| Verify main, worktrees, branches and PRs; preserve existing work | Live main `e198592`; initial and before/after inventory comparisons retain all 24 original worktree HEAD/status pairs, original stash, 210 container ID/name/image/state tuples and log branch `3dddbc2`. Live PR metadata is retained with the gate evidence. | Rechecked throughout; final after-gate preservation remains required. No merge or Lab operation. |
| Review #55–#60 compatibility and order | [Integration review](PROGRAM-54-PHASE2-REVIEW.md) records exact heads, shared hunks, no hard dependencies and order #56, #60, #59, #58, #57, #55. All six live heads remain unchanged and open; GitHub base mergeability is separate from combined qualification/human approval. | Source review complete; merge unauthorized. |
| Prioritize the one-disposable-environment design | #65/#66/#70–#72/#74–#82 compose a singleton policy over existing canonical targets, approved cached pins, lifecycle, typed leases and source/workspace paths. | Implemented source; latest additions await the running combined gate. No general instance manager or scheduler. |
| #45 exact-tree staging, reviewed deletion/conflict resolution | [#62](https://github.com/kcook98765/kodi-lab/pull/62), `workspace_imports.py`, `batch_import.py`; `test_workspace_exact_tree.py` checks 80→78 exact source digest, retained baseline/old tree, explicit private choices, tombstones, replay and refusal cases. | Included in c788 qualification. Existing entry/file/type limits and default overlap policy remain. |
| #46 preflight and #49 Bridge readiness foundations | #55/#57/#63/#64/#68/#76: precise refusal reasons, authorized stored preflight, independent native/capability facts, truthful prepared status, bounded polling and explicit exact-generation continuation under lifecycle ownership. | Included in c788. Read-only assessments never claim live `ready`, native prompt completion or new client authority. |
| Coordinate #50 with the preserved retention branch | The branch was inventoried/read at `3dddbc2` before diagnostics work and remains unchanged/unpublished here. #67/#69/#73 add bounded stopped exit/log/strict referenced-crash metadata and private retained captures without duplicating log rotation/recipe/Docker policy. | Included in c788. No live crash reproduced or cause inferred; native report bodies remain uncollected. |
| #51 minimal durable lifecycle, shared admission, simplified Web | #65/#66 reserve/prepare/resume/stop; #70–#72 review retained evidence and execute exact scoped retirement/atomic successor admission; #74–#79 bind current profile and guide baseline copy; #81/#82 narrow existing enable/smoke authority. [Walkthrough](DEVELOPMENT-SESSION.md#everyday-walkthrough) retains separate source/native/Bridge/grant checks. | Implemented source. #80–#82 are in pending ba215e6, not c788. Native sequential acceptance is not performed. |
| Continue independent authorized work; maintain ledger and periodic #54 milestones | Incremental PRs #61–#82, exact test logs/manifests, [ledger](PROGRAM-54-LEDGER.md), integration review and numbered issue #54 milestone comments. #52/#18/#53/#8/#47/#48 dispositions remain explicit. | Continued across multiple PR groups and qualification cycles; remaining external decisions below. |

## Single-session invariants checked in source

| Invariant | Code and evidence inspected |
| --- | --- |
| One provisioned profile, including stopped resources and concurrent entry points | `development_sessions.py::reserve`, `broker/managed_capacity.py` and production registry share writer reservations. `test_development_sessions.py` covers concurrent prepares, in-flight Advanced provision, all starts, unrelated resources and outside-dispatch adoption refusal; broker tests exercise production reservation wiring. |
| Unknown creates cannot become an empty slot | Uncertain provision reservations persist across restart; `test_uncertain_provision_survives_restart_and_is_not_freed_by_absence` checks empty inventory, repeated provision and close refusal. No elapsed-time/restart/absence settlement endpoint exists. |
| Fresh/Switch preserves evidence and admits no second profile | `session_replacements.py` binds exact retirement proof and atomically closes/reserves/switches policy. Tests cover exact disposal consent, duplicate requests, failed handoff/prepare, revoked cache, capacity, original request identity and sequential version changes with one fake profile. These use real temporary SQL plus fake lifecycle/runtime transport. |
| Source/workspace, old receipts and selected captures survive | Workspace references block cleanup; exact-tree baseline/history assertions and replacement/capture tests retain bytes/identity. New profiles inherit no trust, grants or baseline. Current-profile receipt filtering does not erase original history. |
| Source preparation remains explicit and recoverable | #78/#79 couple durable request receipt, original Source operation and both typed reservations. Tests cover same-request races, lost commit/submit acknowledgement, pruning, stale scope, stored-only GET and exactly one legal fake transport dispatch. Private workspace edits remain separate. |
| Existing native source actions obey active selection and owned authority | #81/#82 test metadata and positive CID/generation checks, exact typed kind/op/token/expiry, fixed smoke bound, malformed original target, cancellation and retained dispatch uncertainty on actual current schema. Their standalone integration results are below, pending grouped release. |
| No new broad agent or native policy | Existing preset/target/source/workspace grants and production veto remain; exact broker/MCP catalogs, schema guards, authorization suites and normal transport auditing stay in the full gate. No settings/screenshot grant or automatic native prompt acceptance was added. |

#81 passed 27 focused + 363 interactions (178.2s); its identical tests yielded
23 behavior failures / 4 compatibility passes on untouched `6aaa097`.
#82 initially passed 31 + 382 (167.9s); original-target follow-up passed
32 + 200 (99.1s), then the final explicit acquire case made 33 focused passes
(19.0s). Final identical tests yielded 25 failures / 8 compatibility passes on
untouched #81. These are synthetic boundary regressions, not reproduced live
incidents. Full ba215e6 qualification is still pending.

## Grouped decisions and unachieved external acceptance

| Item | Precise remaining decision/evidence |
| --- | --- |
| Merge/integration into main | Separately authorize review/merge scope and order for the unchanged exact heads. None was merged here. Source qualification is not maintainer approval. |
| Running-Lab deployment/schema changes | Separately authorize matched application/broker/schema deployment with required backup/export and supported recovery/login proof. No production migration or existing resource change occurred. |
| Native #51/#46/#49 acceptance | Separately authorize an isolated exact-version native sequence, initially one representative Omega, then sequential supported versions, with exact identity, baseline, enabled state, reviewed Bridge/prompt and workspace/evidence preservation proof. No simultaneous test farm or fabricated old incident. |
| Windows/WSL2 #8 | Existing working-platform guide/install path remains; fresh account/host reproduction of the adopted flow and exact versions is still missing. Linux fixtures and the source walkthrough do not supply it. |
| Settings #52 | Approve dedicated ordinary-setting allowlist/secret filtering, per-key results, exact durable deployed add-on binding and explicit permission design before implementation/new grant. Do not substitute application settings or arbitrary Python/path/RPC control. |
| Managed preview #18 | Approve managed screenshot policy/revocation and native budget/latency acceptance scope before broadening the existing narrow preview feature. No new preview implementation or benchmark is claimed. |
| Native ZIP #53 | No supported unattended exact-version installer was demonstrated by the researched upstream interfaces. Assisted dialog/artifact delivery remains distinct from install completion. A provider/native acceptance decision is required before stronger claims. |
| Optional #47/#48 | Deferred per the one-session direction. No generic second-instance manager or selective profile/settings migration has been built. |
| Original #17/#44 incident and exit 139 | Current recovery code/tests are preserved and strengthened; the historical native cause/reproduction remains unknown. Fresh profiles cannot erase unresolved provenance or serve as incident evidence. |

These limits preserve the original phase boundaries. They do not redefine the
whole modernization program as complete or authorize its operational steps.
