# Program 54 authorized source integration

## Source integration completion — 2026-10-25

All application/documentation feature PRs #55–#60 and #62–#82 have merged through
normal GitHub merge commits in the reviewed dependency order. This final ledger
PR #61 completes the 28-PR integration. The source-complete main before this
ledger-only merge is `24b89559b0b2e0ab3edbc98e12227b4ee0981c4c`. The final immutable main SHA including
#61 is recorded in the [issue #54 final merge report](https://github.com/kcook98765/kodi-lab/issues/54)
and the [completed ledger PR #61](https://github.com/kcook98765/kodi-lab/pull/61).
A commit cannot contain its own resulting hash; those two GitHub records provide
that final identity without changing qualified application bytes.

**RELEASE PASS:** 5,763 passed, 38 unchanged gated skips, 5,801 collected,
34m20s complete gate (2059.79s wrapper), all eight stages zero.
Frozen reviewed candidate `133c0dd0f01603efff4940a6d63ed666a7fce0d0`, tree `e724dc6e6e2770a040aada7c06c7703afe326fcc`. Full test selection
and exact skip identities match prior qualified ba215e6. All 1,819 tracked entries
were frozen; executable source, tests/assertions, migrations, gate scripts,
packaging and configuration remain byte-identical through the final merges.
Final follow-up edits are limited to the four program-report Markdown files.

**FOCUSED PASS:** 39 cases across nine explicitly selected route/component and
migration modules, Ruff and strict mypy, 91.7s. Strict full-gate mypy checked
752 files; all six early exact broker/schema guards passed. Migrations 0080–0085
retain one linear head and their direct upgrade, integrity/FK, empty downgrade,
full-history and populated-evidence downgrade-refusal proofs. No production
migration ran and no check or assertion was weakened.

History adjustments replay only #69's actual feature commits and the later
feature stack, retaining original authors/messages and stable feature patch IDs.
Original local branches/worktrees remain unchanged. Ordinary resolution retained
all independent decision records and all four stopped-diagnostics/capability
route functions. No production code or regression assertion required an
integration fix. The final executable tree also matches ba215e6 exactly.

Preservation checks before/during/after qualification match all 55 original
worktrees, stash, three historical untracked test files, all 210 original
containers (including all four running containers), volumes/networks and retained
log branch 3dddbc2. The final main worktree receives only its authorized fast-forward;
existing development worktrees are preserved. Final post-merge verification is
published in issue #54 with the exact final main SHA.

**Completed:** source integration, normal GitHub merges and source-level release
qualification. **Not performed:** deployment, production migration, changes to
grants/credentials or existing Docker/Kodi resources, release publication, real
Kodi acceptance or Windows testing. Optional #52/#18/#53 work and #47/#48 remain
separate. The program issue remains open for the separately authorized phases.

## Final merge inventory

| Order | PR | Final reviewed PR HEAD | GitHub merge commit |
| --- | --- | --- | --- |
| 1 | [#56](https://github.com/kcook98765/kodi-lab/pull/56) | `bbd07688e9b243e8ce25cecc1cc8f62cadc7966e` | `d5fc7148d230a9f015c707a09034bff6c31c5b53` |
| 2 | [#60](https://github.com/kcook98765/kodi-lab/pull/60) | `50975c80252eda22a7f7a5589ca0d27c7f28d229` | `02248bbde3a04db46f41277ff82e0a938efbb3cd` |
| 3 | [#59](https://github.com/kcook98765/kodi-lab/pull/59) | `113111e884697a6b7f4d065a8f9408db2c31522a` | `792bac237bb7b65a53975c1b1044191643ea7fde` |
| 4 | [#58](https://github.com/kcook98765/kodi-lab/pull/58) | `4c2f6818352f98c2d96ca529da3bcfe368418c0c` | `f5f3bb38a200705c3ce9beadc566f9ea72838466` |
| 5 | [#57](https://github.com/kcook98765/kodi-lab/pull/57) | `98d382ae70cfe86c064f83489e95ac7430ce9d5f` | `aad8ffc3b304c51e6330159f5116dd232abf7c46` |
| 6 | [#55](https://github.com/kcook98765/kodi-lab/pull/55) | `0c45ff3d624f921fc4ea1ba92c40a2da2720d22c` | `0d5c43f41d8cf75c7b0d7d90ce3a8fda154edeba` |
| 7 | [#62](https://github.com/kcook98765/kodi-lab/pull/62) | `43a332fce0182cf4de13bc1e348c965a78ad1d73` | `2ae0c04cb17aaa4945b9792c9fec2e1393e32838` |
| 8 | [#65](https://github.com/kcook98765/kodi-lab/pull/65) | `c5e3aff26241b47e09914243cfb018fbbd256ad4` | `964cc196d686dc78b07472b64d308dc62bcc33e3` |
| 9 | [#63](https://github.com/kcook98765/kodi-lab/pull/63) | `8efa079f33d17999c55a8dc2d9dfa27978de1d18` | `03c675c30d5ff1a359bfbfb83baa903d567d117b` |
| 10 | [#64](https://github.com/kcook98765/kodi-lab/pull/64) | `cabab311384de9f1337336eda479b0d8dfb34523` | `888a2a64ce86b78ed4dd374c51ae6c46d6228fea` |
| 11 | [#66](https://github.com/kcook98765/kodi-lab/pull/66) | `2688e042d41044d44176048ab3b210a045e04575` | `073c86b9a95975592de759c19ab87577e9e6a663` |
| 12 | [#67](https://github.com/kcook98765/kodi-lab/pull/67) | `2bfe471a74f1c16448b638de8ca7d91c25de5551` | `8528ee0fb40c459138ec9a7cbe63dfce63d458df` |
| 13 | [#68](https://github.com/kcook98765/kodi-lab/pull/68) | `06f29a1a4f187ffa3d6a4819913ece10cab39f4f` | `d2dc0a7ac0b004f2c84d033e702de17d5f3ab002` |
| 14 | [#69](https://github.com/kcook98765/kodi-lab/pull/69) | `20114c31ad1e6338020eb015b8785b475021e216` | `4865d8e30fc6b17a60695f832dbcb0e34cd1f942` |
| 15 | [#70](https://github.com/kcook98765/kodi-lab/pull/70) | `c4c37033d795d3805241f01b8fd84343f08611f4` | `f9792ecb07bb7102240a72e36bec32dd3437349f` |
| 16 | [#71](https://github.com/kcook98765/kodi-lab/pull/71) | `73b9bc2a5f2720873cb23f306c82416649697808` | `be6a108da2e35586ceee4598349d3c0a9f58ebf4` |
| 17 | [#72](https://github.com/kcook98765/kodi-lab/pull/72) | `8c6a8c953076a7589b4c98733785a027f12d700c` | `05fddb6b4c7d3c6d01f7796e1d9d403db47d3d2b` |
| 18 | [#73](https://github.com/kcook98765/kodi-lab/pull/73) | `9aa79353f20161f36451db6b70b3277ee7933754` | `4eb3195fccc8142e905422d3f40238d12ab6f184` |
| 19 | [#74](https://github.com/kcook98765/kodi-lab/pull/74) | `46f233b8e4cc5f8aababa8af2156b54c12215a88` | `6b32f81c1008d55354557eb2484eb0470e37074d` |
| 20 | [#75](https://github.com/kcook98765/kodi-lab/pull/75) | `674fb984f5bcfb9f41c339fd9781738d759c754d` | `087273f969ef01aad65ce8b9b2f8b87bd5772602` |
| 21 | [#76](https://github.com/kcook98765/kodi-lab/pull/76) | `bba1ef1d7ff933bc123cdde7280005e12583dd20` | `1ac26de0870ca84ce78c2660525b5eff3ceb735b` |
| 22 | [#77](https://github.com/kcook98765/kodi-lab/pull/77) | `cff69854b90b33b0752e73f2e68dfa2b7173e572` | `c9b7f6110bd2511ee3cd38a07d171ff9ee9a7c8c` |
| 23 | [#78](https://github.com/kcook98765/kodi-lab/pull/78) | `5801a3a3711aa9c300204179ecf49e891aff5ba3` | `dd3f773a83aac479a456b14f8f0cdb94e3031b4d` |
| 24 | [#79](https://github.com/kcook98765/kodi-lab/pull/79) | `cf70b6905ed66663d6ddfa216da155aa06b140fe` | `fd6a6c75b6f2d5c834201d8185d6ee61fecc01f5` |
| 25 | [#80](https://github.com/kcook98765/kodi-lab/pull/80) | `ff4fbfcb3c38b612344edad85e5f946ffefa6d16` | `710c3441c96357a3a1b3e0708a79468e9f1e7ffc` |
| 26 | [#81](https://github.com/kcook98765/kodi-lab/pull/81) | `4744e0502b8ca6819ad8e52bc4ce1323d2219471` | `452479931b03e55a3972fb72ff9c981f3a54724a` |
| 27 | [#82](https://github.com/kcook98765/kodi-lab/pull/82) | `5e3d803920a000a00e3c5ffd48bc7e5ce2dae825` | `24b89559b0b2e0ab3edbc98e12227b4ee0981c4c` |
| 28 | [#61](https://github.com/kcook98765/kodi-lab/pull/61) | Ledger completion in this PR | [Final merge identity](https://github.com/kcook98765/kodi-lab/pull/61) |

## Full release result

| Release stage | Seconds | Exit |
| --- | ---: | ---: |
| Ruff format | 1 | 0 |
| Ruff lint | 0 | 0 |
| mypy | 22 | 0 |
| contract guards | 9 | 0 |
| pytest | 2021 | 0 |
| package build | 5 | 0 |
| Alembic history | 1 | 0 |
| package/config import | 1 | 0 |

Peak pytest RSS: 3,233,536 KiB. Minimum sampled scratch free space: 5,780,467,712 bytes. All three exact run-owned scratch roots were removed by the normal harness. Private logs, JUnit/JSON, stage timings, stable patch IDs and before/after inventories are retained under `/home/kyle/kodi-lab-integration/54-20261025/evidence/`.


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
