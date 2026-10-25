# Program 54 Phase 2 source status and remaining decisions

## Integration completed — 2026-10-25

All 28 PRs #55–#82 are accounted for by the final ledger merge #61. Source-level **RELEASE PASS**: 5,763 passed, 38 unchanged gated skips, 34m20s; all eight stages passed. Source-complete main before the ledger-only merge: `24b89559b0b2e0ab3edbc98e12227b4ee0981c4c`. The final immutable main SHA and preservation proof are recorded in [issue #54](https://github.com/kcook98765/kodi-lab/issues/54) and [the complete integration report](PROGRAM-54-MERGE-REPORT.md). Deployment, production migrations and native/Windows acceptance remain separately authorized phases. Historical entries below retain their earlier status and pins.


## Current authorized source integration

The 2026-10-25 operator kickoff authorizes review and merge of all PRs #55–#82.
See [the integration report](PROGRAM-54-MERGE-REPORT.md) for the exact inventory,
dependency order, security review, history adjustments and final validation.
Earlier pending/unauthorized statements below describe their historical phase.
Deployment, production migrations and native/platform acceptance remain separate.

This is the current source-scope checklist, not installed-Lab or native
acceptance. Historical milestone entries in the audit, integration review and
ledger retain their original dates and qualification boundaries.

## Qualification

- Latest independently verified combined **RELEASE PASS**: `ba215e6d62dac996cfdf12e9df102bc1b16b6a99`, tree `d6b76696e2eb7dbca0d115493bd0b137e4ae4862`:
  **5,763 passed / 38 gated skips / 34m32s**, all eight stages, source manifest and all three owned-scratch
  removals verified. It composes #80, #81 and final #82 with report snapshot
  `5420c39`; all 1,817 tracked entries and 11 changed-file hashes remain exact.
- Prior `c788a31`, tree `4211e3d`, remains a separate successful pin:
  5,703 / 38 / 34m09s through #79. Its functional source `6aaa097` is an ancestor
  of the new candidate; c788 itself is a sibling report-snapshot commit, not an
  ancestor. No release result was inherited or fabricated.
- Failed `557dcdd` remains failed (5,617 passed / 1 historical-fixture failure /
  38 skips / 32m09s). Its later correction does not rewrite that evidence.
  Earlier `cbc8894` (5,515 / 38 / 32m12s) and Phase 1 `02c4054`
  (5,234 / 38 / 29m59s) remain separate exact successful pins.

## Kickoff requirements

| Requirement | Current authoritative evidence | Scope/status |
| --- | --- | --- |
| Verify main, worktrees, branches and PRs; preserve existing work | Live main `e198592`; initial and before/after inventory comparisons retain all 24 original worktree HEAD/status pairs, original stash, 210 container ID/name/image/state tuples and log branch `3dddbc2`. Live PR metadata is retained with the gate evidence. | Rechecked before and after the successful gate; all original evidence matched. No merge or Lab operation. |
| Review #55–#60 compatibility and order | [Integration review](PROGRAM-54-PHASE2-REVIEW.md) records exact heads, shared hunks, no hard dependencies and order #56, #60, #59, #58, #57, #55. All six live heads remain unchanged and open; GitHub base mergeability is separate from combined qualification/human approval. | Source review complete; merge unauthorized. |
| Prioritize the one-disposable-environment design | #65/#66/#70–#72/#74–#82 compose a singleton policy over existing canonical targets, approved cached pins, lifecycle, typed leases and source/workspace paths. | Implemented source; latest additions included in the verified ba215e6 gate. No general instance manager or scheduler. |
| #45 exact-tree staging, reviewed deletion/conflict resolution | [#62](https://github.com/kcook98765/kodi-lab/pull/62), `workspace_imports.py`, `batch_import.py`; `test_workspace_exact_tree.py` checks 80→78 exact source digest, retained baseline/old tree, explicit private choices, tombstones, replay and refusal cases. | Included in c788 qualification. Existing entry/file/type limits and default overlap policy remain. |
| #46 preflight and #49 Bridge readiness foundations | #55/#57/#63/#64/#68/#76: precise refusal reasons, authorized stored preflight, independent native/capability facts, truthful prepared status, bounded polling and explicit exact-generation continuation under lifecycle ownership. | Included in c788. Read-only assessments never claim live `ready`, native prompt completion or new client authority. |
| Coordinate #50 with the preserved retention branch | The branch was inventoried/read at `3dddbc2` before diagnostics work and remains unchanged/unpublished here. #67/#69/#73 add bounded stopped exit/log/strict referenced-crash metadata and private retained captures without duplicating log rotation/recipe/Docker policy. | Included in c788. No live crash reproduced or cause inferred; native report bodies remain uncollected. |
| #51 minimal durable lifecycle, shared admission, simplified Web | #65/#66 reserve/prepare/resume/stop; #70–#72 review retained evidence and execute exact scoped retirement/atomic successor admission; #74–#79 bind current profile and guide baseline copy; #81/#82 narrow existing enable/smoke authority. [Qualified-source walkthrough](https://github.com/kcook98765/kodi-lab/blob/ba215e6d62dac996cfdf12e9df102bc1b16b6a99/docs/DEVELOPMENT-SESSION.md#everyday-walkthrough) retains separate source/native/Bridge/grant checks. | Implemented source. #80–#82 are now included in verified ba215e6; they remain outside c788. Native sequential acceptance is not performed. |
| Continue independent authorized work; maintain ledger and periodic #54 milestones | Incremental PRs #61–#82, exact test logs/manifests, [ledger](PROGRAM-54-LEDGER.md), integration review and numbered issue #54 milestone comments. #52/#18/#53/#8/#47/#48 dispositions remain explicit. | Continued across multiple PR groups and qualification cycles; remaining external decisions below. |

## Single-session invariants checked in source

| Invariant | Code and evidence inspected |
| --- | --- |
| One provisioned profile, including stopped resources and concurrent entry points | `development_sessions.py::reserve`, `broker/managed_capacity.py` and production registry share writer reservations. `test_development_sessions.py` covers concurrent prepares, in-flight Advanced provision, all starts, unrelated resources and outside-dispatch adoption refusal; broker tests exercise production reservation wiring. |
| Unknown creates cannot become an empty slot | Uncertain provision reservations persist across restart; `test_uncertain_provision_survives_restart_and_is_not_freed_by_absence` checks empty inventory, repeated provision and close refusal. No elapsed-time/restart/absence settlement endpoint exists. |
| Fresh/Switch preserves evidence and admits no second profile | `session_replacements.py` binds exact retirement proof and atomically closes/reserves/switches policy. Tests cover exact disposal consent, duplicate requests, failed handoff/prepare, revoked cache, capacity, original request identity and sequential version changes with one fake profile. These use real temporary SQL plus fake lifecycle/runtime transport. |
| Source/workspace, old receipts and selected captures survive | Workspace references block cleanup; exact-tree baseline/history assertions and replacement/capture tests retain bytes/identity. New profiles inherit no trust, grants or baseline. Current-profile receipt filtering does not erase original history. |
| Source preparation remains explicit and recoverable | #78/#79 couple durable request receipt, original Source operation and both typed reservations. Tests cover same-request races, lost commit/submit acknowledgement, pruning, stale scope, stored-only GET and exactly one legal fake transport dispatch. Private workspace edits remain separate. |
| Existing native source actions obey active selection and owned authority | #81/#82 test metadata and positive CID/generation checks, exact typed kind/op/token/expiry, fixed smoke bound, malformed original target, cancellation and retained dispatch uncertainty on actual current schema. Their individual integration results are below; the settled composition is now release-qualified. |
| No new broad agent or native policy | Existing preset/target/source/workspace grants and production veto remain; exact broker/MCP catalogs, schema guards, authorization suites and normal transport auditing stay in the full gate. No settings/screenshot grant or automatic native prompt acceptance was added. |

#81 passed 27 focused + 363 interactions (178.2s); its identical tests yielded
23 behavior failures / 4 compatibility passes on untouched `6aaa097`.
#82 initially passed 31 + 382 (167.9s); original-target follow-up passed
32 + 200 (99.1s), then the final explicit acquire case made 33 focused passes
(19.0s). Final identical tests yielded 25 failures / 8 compatibility passes on
untouched #81. These are synthetic boundary regressions, not reproduced live
incidents. The settled ba215e6 composition now has RELEASE PASS (5,763 passed / 38 gated skips / 34m32s); individual PR heads were not separately full-qualified.

## Grouped decisions and unachieved external acceptance

The source is published as incremental PRs rather than a proposal to merge the
qualification branch. For review, retain these dependency groups:

- Phase 1: #56, #60, #59, #58, #57, #55; these have no hard code dependency on
  one another. #61 is the continuing documentation ledger over #56.
- Independent foundations: #62 (exact tree), #65 then #66 (sessions), #63 over
  #57 (preflight), #64 then #68 over #55 (Bridge), and #67 (exit diagnostics).
- The saved composition underlying #69 includes those foundations. From #69,
  #70 through #79 form the sequential evidence/replacement/admission/Web stack.
- #80 is a documentation sibling over #79; #81 follows #79 and #82 follows
  #81. The frozen candidate composes both siblings and preserves their complete
  appended session documentation.

The new migration chain is linear: 0080 (session admission), 0081 (retained
diagnostics), 0082 (replacement review), 0083 (retirement receipts), 0084
(successor handoff), and 0085 (source-copy request admission). These are source
migrations tested against temporary databases, not production migrations run
during this phase. Existing populated-data downgrade refusal remains mandatory.
Any rebase, conflict resolution or source edit creates a new candidate requiring
appropriate exact-tree validation; the pinned result cannot certify changed
bytes. GitHub's mergeable flag supplies neither that validation nor merge
authorization.

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
