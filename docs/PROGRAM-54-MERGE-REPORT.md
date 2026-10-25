# Program 54 authorized source integration

The 2026-10-25 operator kickoff explicitly authorizes repository integration,
ordinary conflict fixes and GitHub merge commits for all 28 PRs #55–#82.
This supersedes earlier merge restrictions for these PRs only. Deployment,
production migrations, grants, existing Docker/Kodi resources, publication and
native/Windows acceptance remain outside this phase.

## Verified starting state

Local and freshly fetched remote main both equal
`e198592b634bc5bc2cf876fd5fbded88b7cd827d`. All 28 PRs are open and ready;
initial GitHub mergeability is recorded separately from combined compatibility.
The prior RELEASE PASS is independently verified at
`ba215e6d62dac996cfdf12e9df102bc1b16b6a99`, tree
`d6b76696e2eb7dbca0d115493bd0b137e4ae4862`: 5,763 passed, 38 gated skips,
34m32s. Its result is historical evidence, not this integration's gate result.

Before work, 55 existing worktrees (including three detached regression checkouts
with untracked test files), the original stash, 210 containers (four running),
volumes, networks and the unpublished log-retention branch
`3dddbc281a4796f841e056688c340cd83b276341` were inventoried. The dedicated
integration worktree is `54-merge-integration-20261025`. No existing feature
worktree is used for conflict resolution or rebasing.

## Review and dependency order

Phase 1 follows the previously reviewed order. Independent foundations then
establish exact-tree staging and single-session policy before the composed
retention/replacement/source stack. #80 remains a guide sibling over #79;
#81 follows #79 and #82 follows #81. #61 is last so it can retain this report.

| Order | PR | Original reviewed HEAD | Original base |
| --- | --- | --- | --- |
| 1 | [#56](https://github.com/kcook98765/kodi-lab/pull/56) | `bbd07688e9b243e8ce25cecc1cc8f62cadc7966e` | `main` |
| 2 | [#60](https://github.com/kcook98765/kodi-lab/pull/60) | `50975c80252eda22a7f7a5589ca0d27c7f28d229` | `main` |
| 3 | [#59](https://github.com/kcook98765/kodi-lab/pull/59) | `113111e884697a6b7f4d065a8f9408db2c31522a` | `main` |
| 4 | [#58](https://github.com/kcook98765/kodi-lab/pull/58) | `4c2f6818352f98c2d96ca529da3bcfe368418c0c` | `main` |
| 5 | [#57](https://github.com/kcook98765/kodi-lab/pull/57) | `98d382ae70cfe86c064f83489e95ac7430ce9d5f` | `main` |
| 6 | [#55](https://github.com/kcook98765/kodi-lab/pull/55) | `0c45ff3d624f921fc4ea1ba92c40a2da2720d22c` | `main` |
| 7 | [#62](https://github.com/kcook98765/kodi-lab/pull/62) | `43a332fce0182cf4de13bc1e348c965a78ad1d73` | `main` |
| 8 | [#65](https://github.com/kcook98765/kodi-lab/pull/65) | `31a01af53bcd202cba823e737da94a3d608ff6fa` | `main` |
| 9 | [#63](https://github.com/kcook98765/kodi-lab/pull/63) | `c4dc028562a8a49d9f2074a5d3aa2713455a131f` | `fix/46-managed-baseline-reasons-20261024` |
| 10 | [#64](https://github.com/kcook98765/kodi-lab/pull/64) | `f6fe7dcb54eb8551bacef20cc8cbea7dd425813a` | `fix/49-bridge-status-20261024` |
| 11 | [#66](https://github.com/kcook98765/kodi-lab/pull/66) | `2688e042d41044d44176048ab3b210a045e04575` | `feat/51-session-admission-20261024` |
| 12 | [#67](https://github.com/kcook98765/kodi-lab/pull/67) | `8833c30cbae79763310eff022fa45dd3482e9fa5` | `main` |
| 13 | [#68](https://github.com/kcook98765/kodi-lab/pull/68) | `2080f4e73b0fa895b5e48454c1939c154c763ca6` | `feat/49-prepared-continuation-20261024` |
| 14 | [#69](https://github.com/kcook98765/kodi-lab/pull/69) | `80605de41b9af6d9d45a9eca3c7464da87df27ce` | `qualification/54-capability-overlay-20261024` |
| 15 | [#70](https://github.com/kcook98765/kodi-lab/pull/70) | `782b33f56558862800428fe4a6218bb2a83ffafc` | `feat/50-retained-stopped-diagnostics-20261025` |
| 16 | [#71](https://github.com/kcook98765/kodi-lab/pull/71) | `cbc889429138df19ed11ebf09706541313545c2d` | `feat/51-replacement-review-20261025` |
| 17 | [#72](https://github.com/kcook98765/kodi-lab/pull/72) | `9067899ac68ccd32b6b7ce2aba34f0fc6ff93896` | `feat/51-scoped-retirement-receipts-20261025` |
| 18 | [#73](https://github.com/kcook98765/kodi-lab/pull/73) | `27abf6fc8e9e017ae5d4900264cc14cf78b24b45` | `feat/51-atomic-session-handoff-20261025` |
| 19 | [#74](https://github.com/kcook98765/kodi-lab/pull/74) | `79d7d2410a24bfaf3650df2d7c117a3ff2f47d52` | `feat/50-referenced-crash-metadata-20261025` |
| 20 | [#75](https://github.com/kcook98765/kodi-lab/pull/75) | `d56e30a035bd015d9dce537a6713cf9157a675ee` | `feat/51-workspace-session-admission-20261025` |
| 21 | [#76](https://github.com/kcook98765/kodi-lab/pull/76) | `2ebe7e1ff88f416d4e8b706294078237d66e93c0` | `feat/51-profile-deployment-lineage-20261025` |
| 22 | [#77](https://github.com/kcook98765/kodi-lab/pull/77) | `9ebbb28d4358682df5d430eaab74d95c2b6f0691` | `feat/51-session-preparation-checklist-20261025` |
| 23 | [#78](https://github.com/kcook98765/kodi-lab/pull/78) | `6b87f12c3048494d2610269849e851a45636fe8c` | `feat/51-stopped-source-queue-binding-20261025` |
| 24 | [#79](https://github.com/kcook98765/kodi-lab/pull/79) | `6aaa097d62a235a291274ce04bcc41c6ad5670c0` | `feat/51-guided-source-copy-admission-20261025` |
| 25 | [#80](https://github.com/kcook98765/kodi-lab/pull/80) | `945b3af798d5d5becc5851c29b3c769ad994358d` | `feat/51-guided-source-copy-web-20261025` |
| 26 | [#81](https://github.com/kcook98765/kodi-lab/pull/81) | `87c36a494569862f3464cf8dbbea85bdce019db0` | `feat/51-guided-source-copy-web-20261025` |
| 27 | [#82](https://github.com/kcook98765/kodi-lab/pull/82) | `683c2c504ec7dce9ca01feeeb3dee5d094f07e21` | `feat/51-source-enablement-session-scope-20261025` |
| 28 | [#61](https://github.com/kcook98765/kodi-lab/pull/61) | `9ec1d3aaec8dd19dd9f029e270d458fb168455f8` | `program/54-test-performance-20261024` |

## History and conflict decisions

- #66 shares the original first two #65 commits. Its base predates #65's final
  seven-line schema-test correction; merging #65 first preserves that correction
  without replaying or rewriting #66.
- #69's original parent `24c1aeb` is a qualification composition containing
  cherry-picked copies of earlier PR commits. Only #69's two feature commits
  are replayed onto the independently integrated actual feature foundations.
  #70–#82 are replayed by their exact original base-to-head ranges. Preserve
  authors, messages and PR attribution; use explicit old-SHA leases for remote
  feature-branch updates. Do not push rewritten local development branches or
  merge the qualification branch wholesale. Original objects and local refs
  remain available in the preserved historical worktrees.
- Independent appended decision records are retained in full. Numeric ordering
  and spacing match the qualified decision file; every section body was checked.
- The #67 stopped-diagnostics route and #68 capability-prerequisites routes
  conflict at the same insertion point. Both complete blocks are retained with
  separate closing parentheses. AST comparison proves all four affected route
  functions retain their original feature-head bodies and decorators.
- Other shared imports, broker operations, schema expectations, deployment
  admission and Web changes compose without a substantive design conflict.
  No assertion, check, test selection or production implementation was changed
  to make integration pass.

## Correctness and security review

Reviewed actual base-to-head histories, production diffs, migration definitions,
shared admission paths and the regression coverage for each PR group:

- #55/#64 retain explicit reviewed Bridge actions, displayed trust generation,
  lifecycle ownership, bounded status polling and uncertain-POST refusal.
  Stored preparation never implies installed or live capability readiness.
- #56 changes nine fixture constructors only; independent files, engines,
  seeds and existing assertions remain. #59 reads retained reports without
  executing tests. #60 changes only the measured default watchdog to 2400s.
- #58 keeps copied-tree uncertainty fenced and narrowly admits explicit
  historical recovery. #57 allowlists baseline reasons. #62 retains default
  import policy, quotas/grants, reviewed removals, full-tree proofs, old receipt
  compatibility and uncertain publication recovery. #63 preflight is authorized,
  read-only and cannot claim live readiness or dispatch/repair deployment.
- #65/#66 serialize all production provision/start entry points with the durable
  singleton policy. Stopped profiles occupy the slot; unknown create dispatch
  cannot be settled by absence, time or restart. Workspace history blocks cleanup.
- #67/#69/#73 use exact disposable target/runtime/container/image identity,
  stopped-state rechecks, fixed paths/artifacts, bounded reads/deadlines and
  redaction. Private captures and legacy readers survive profile retirement;
  crash metadata does not collect report bodies or infer cause. The separate
  preserved rotation/recipe/Docker log policy is not imported or duplicated.
- #70/#71/#72 bind retained-evidence holds, explicit disposal confirmation,
  snapshot hashes, typed lifecycle ownership and exact retirement receipts.
  Only confirmed retirement plus idle/empty inventory permits an atomic
  successor policy handoff. New profiles inherit no baseline, trust or grants.
- #74/#75 bind fresh workspace actions and receipt selection to the selected
  current profile while preserving historical replay/recovery. #76 reads stored
  preparation without transport or mutation. #77 pins queue-time source identity;
  #78/#79 atomically retain request attribution and typed reservations, use the
  existing executor and permit only the same unclaimed queued child to continue.
  Lost acknowledgements never reset running, terminal or ambiguous work.
- #81/#82 recheck source/session scope, positive observed CID, original target,
  typed kind/operation/token and unexpired ownership before native markers.
  Nullable historical compatibility and post-dispatch uncertainty remain.
- #80 aligns the guide with composed behavior and keeps deployment/native limits
  explicit. #61 preserves historical failed/successful pins separately from the
  current source-status checklist. Existing production vetoes and grants remain.

The exact broker catalog is 40 operations and the MCP catalog is 27 tools;
these include reviewed Program 54 additions and retain their closed-set guards.
No new settings, screenshot, automatic prompt or external-production authority
is introduced by this integration.

## Candidate equivalence and validation

All application, test, migration, script, packaging, configuration and functional
documentation blobs match the prior qualified tree exactly. Differences are the
newer ledger/review/status reports plus this integration report. This equivalence
includes every regression assertion, migration 0080–0085 and gate configuration.
No functional fix is needed beyond preserving complete route/decision blocks.

The migration graph has one head, `0085_session_source_copy_admissions`, with
linear parents 0079 → 0080 → 0081 → 0082 → 0083 → 0084 → 0085. Direct predecessor
upgrades, empty downgrade round trips, historical recovery, foreign keys,
retained populated-evidence refusal and the canonical full history remain tested
against disposable fixture databases only.

Focused route/shared-component and migration checks, followed by the complete
unchanged release gate, are recorded in the final issue #54 report. Merge-time
source comparison must match this frozen candidate before completion. Reporting
updates after the gate change Markdown only and do not transfer a PASS to changed
application/test/migration bytes.

## Remaining external phases

Source integration and GitHub merges do not deploy the installed Lab. Matched
recoverable application/broker/schema deployment, production migration, real Kodi
acceptance, Windows/WSL2 testing, release publication and optional features need
separate authority. #52 settings and #18 managed preview retain their design and
permission decisions; #53 retains its supported-native-installation evidence gap;
#47/#48 remain deferred. Program #54 stays open for those remaining phases.

The final immutable main SHA, all GitHub merge SHAs, full validation timings and
preservation result are published in issue #54 and the completed ledger PR #61.
This avoids embedding a commit's own unknown hash in its source tree.
