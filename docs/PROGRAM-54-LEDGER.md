# Kodi Lab development program #54

Coordination: <https://github.com/kcook98765/kodi-lab/issues/54>.
This ledger records source qualification, not deployment or native acceptance.

Current requirement-by-requirement [Phase 2 source status and remaining decisions](PROGRAM-54-PHASE2-STATUS.md) distinguish the current state from historical milestones.

## Current Phase 2 checkpoint (2026-10-25 UTC)

- Settled shared-native-boundary candidate **RUNNING**: `ba215e6`, tree
  `d6b7669`, starting11:55UTC. It composes source82 `683c2c5`, guide80 and report
  snapshot5420c39;1,817tracked entries/11changedSHA. No result yet. c788 remains
  the latest verified RELEASE PASS; later81/82 results are integration only.

- Latest completed combined qualification: **RELEASE PASS**, commit
  `c788a3166002ea171074d08f8363a827fb1e3da0`, tree
  `4211e3d7bda1b32b38e2044111e45d19959248a2`: **5,703 passed / 38 gated skips /
  34m09s**. All eight stages exited zero, all three owned scratch roots removed,
  all 1,815 tracked entries and 61 changed-file SHA values reverified. This is
  #79 source `6aaa097` plus the two report files at snapshot `96e8c5d`; it includes
  #74's fixture correction and #75–#79. No native/platform acceptance implied.
- Prior successful combined `cbc8894`: 5,515 / 38 / 32m12s. Phase 1 remains
  separately pinned `02c4054`: 5,234 / 38 / 29m59s. Frozen `557dcdd` remains a
  **failed** historical attempt: 5,617 / 1 fixture failure / 38 / 32m09s; its final
  three stages did not run. Later correction/success does not rewrite that result.
- [#78](https://github.com/kcook98765/kodi-lab/pull/78) adds durable exact source-
  copy admission; [#79](https://github.com/kcook98765/kodi-lab/pull/79) adds the
  guided stopped baseline-copy Web flow and Source queue/worker/broker policy.
  Their standalone focused/integration results remain below; their settled
  composition is included in c788. Review order #74→#75→#76→#77→#78→#79.
- [Draft #80](https://github.com/kcook98765/kodi-lab/pull/80), `945b3af`, is a
  Markdown walkthrough correction with static review only, **outside c788**.
  [Draft #81](https://github.com/kcook98765/kodi-lab/pull/81), `87c36a4`,
  narrows synchronous source enablement and typed marker ownership:27 focused
  +363 interactions passed178.2s, outside c788 with no release inheritance.
- [Draft #82](https://github.com/kcook98765/kodi-lab/pull/82), `683c2c5`,
  retains existing queued smoke profile/typed ownership with no migration.
  Initial31 focused+382 interactions passed167.9s; target-consistency follow-up
  passed32+200 in99.1s, then final33 focused19.0s. Outside c788; no inheritance.
- At 11:21:47 UTC, main `e198592`, all 24 original worktree HEAD/status pairs,
  stash, 210 container ID/name/image/state records and preserved log branch
  `3dddbc2` were again verified unchanged. No merge, deployment, grant/resource
  operation or production migration. The full qualification branch stays frozen.
- Grouped external decisions remain in the [Phase 2 review](PROGRAM-54-PHASE2-REVIEW.md):
  settings/screenshot authority/design, native ZIP limits, native/platform
  acceptance and separately authorized operational integration.

## Authority and preservation

The 2026-10-24 kickoff authorizes reversible code, tests, documentation, commits,
feature-branch pushes and PR creation. Merging, deployment, changing security
permissions, modifying production Kodi and deleting existing resources require
separate authorization. Work occurs in isolated worktrees. Existing targets,
profiles, images, containers, credentials, worktrees and unrelated changes stay
preserved. Test wrappers may reclaim only the scratch they create.

## Initial audit

- Local and remote `main`: `e198592b634bc5bc2cf876fd5fbded88b7cd827d`,
  verified by `git ls-remote` and fetch on 2026-10-25 UTC (2026-10-24 Pacific).
  Primary checkout clean. No open PRs at audit time.
- Sixteen pre-existing additional worktrees preserved. Their identities and
  dirty states, container inventory and issue snapshots are recorded outside
  the source checkout in `/home/kyle/kodi-lab-program-54/20261024/`.
- Initial branch: `program/54-test-performance-20261024`; worktree:
  `/home/kyle/kodi-lab-worktrees/54-test-performance-20261024`.
- Read `AGENTS.md`, `CONTRIBUTING.md` and `docs/TEST-PERFORMANCE.md` before
  choosing gates. No parallel agents or live acceptance environments started.
- Existing acceleration includes guarded tmpfs, owned-scratch reclamation,
  phase/resource JSON, migration templates, a Web/ZIP current-schema template
  pilot, early contract guards, focused/integration runners and a full release
  gate. Preserve assertions and migration-transition coverage.

## Dependency review

Completed initial issue review: #17, #18, #44–#54, directly relevant completed
#7/#12/#16/#20/#24/#28–#30 and open Windows acceptance #8, including comments.
See [current-main dependency audit](PROGRAM-54-AUDIT.md) for code paths,
dispositions, session design constraints and incremental qualification plan.

Findings: #17 implementation already exists; #44 terminal failures need explicit
recovery admission; #46 must separate scope authorization and setup evidence;
#49 prepared trust is not installation proof; max-running=1 does not enforce one
provisioned container for #51. Keep #47/#48 deferred, #8 native acceptance open.

## Validation and timings

Release baseline **FAILED by timeout**, exit 124, on unmodified `e198592`
application/tests. Only the two program documents differed in that worktree. Log:
`/home/kyle/kodi-lab-program-54/20261024/main-release-baseline.log`.
Private stage/contract evidence was copied from
`/tmp/kodi-lab-check-results.m4DPsZHH` to the durable evidence directory's
`main-release-baseline-evidence/`. No other tests ran concurrently.
Node v22.23.3, uv 0.11.7,
CPython 3.12.3; non-root UID 1000. Initial free space: approximately 8.17 GB
tmpfs, 70.94 GB disk; available memory approximately 13.34 GB. Dependencies
installed with `uv sync --locked --dev` in this worktree's own virtualenv.

| Main baseline stage | Time / result |
| --- | --- |
| Ruff format / lint | Both passed, under one second each. |
| Full strict mypy | Passed, 691 files, 35 seconds. |
| Exact contract guards | 6 passed, 7.64 seconds pytest / 11 seconds stage wall. |
| Full pytest | 5,243 collected; watchdog terminated at 900 seconds, about 71% progress; status 124. No assertion failure appeared before timeout. This is not a suite pass. |
| Later build / migration history / import | Not reached. |

The terminated pytest did not publish complete XML/JSON, so phase/resource
totals and the full-suite completion time remain unknown. Runner-owned scratch
cleanup succeeded. Repository-transition tests consumed substantial observed
wall time; the complete candidate report below now provides exact phase data.
The first four increments left the runner/default unchanged and used the
documented 2,400-second override. A separate, subsequently qualified increment
(PR #60) makes that successfully exercised bound the default. Do not rerun
unchanged main merely to obtain a pass.

Matched focused/integration baseline and candidate both passed through the actual
integration runner; selection/command, per-stage timing and phase JSON are retained as
`schema-before.log`, `schema-before.json` and `schema-pilot-selection.json` in
the durable evidence directory. Production schema/auth semantics and actual
Alembic transitions remain untouched by the planned fixture-only pilot.

| Matched selection | Before | After |
| --- | --- | --- |
| Focused managed-instance module | 22 passed | 22 passed |
| Interaction selection | 247 passed, one existing warning | 247 passed, same warning |
| Interaction pytest | 38.30s | 18.21s (52.5% lower) |
| Interaction setup phases | 29.24s | 8.22s |
| Complete integration lane | 48.2s | 25.7s |
| Peak pytest process RSS, interaction portion | 372,272 KiB | 271,916 KiB |

Nine explicit application fixtures now clone the already checked, unseeded,
closed current-schema template into independent case databases. Seeds, engines,
sessions, foreign-key behavior and assertions remain per-case and unchanged.
The four storage modules in the matched selection remain unchanged controls;
`tests/storage/conftest.py` was not modified. These single matched runs establish
selection-specific savings only; full-suite savings remain unknown.

The first instrumented integration attempt failed during CLI parsing because a
custom plugin option in `PYTEST_ADDOPTS` was not preloaded for multiple paths;
its 22-test focused portion passed. The external benchmark command was corrected
to explicitly preload `tests.efficiency_plugin` for both matched runs. The failed
instrumentation log is retained separately; no product behavior was changed for
it. The complete matched command and phase aggregates are in
`schema-comparison.json` and `run-fixture-lane.py` in the durable evidence store.

## Completed increments and PRs

Increment status (full qualification and later focused additions are pinned below):

| Increment | Isolated branch/worktree | Status |
| --- | --- | --- |
| #54 fixture performance | `program/54-test-performance-20261024`, `/home/kyle/kodi-lab-worktrees/54-test-performance-20261024` | Code commit `c156e79`; [PR #56](https://github.com/kcook98765/kodi-lab/pull/56). INTEGRATION PASS for matched 269-case lane before/after; FOCUSED PASS for all nine changed modules individually (235 cases) plus 15 existing template/cleanup proofs, Ruff/mypy, 43.2s. Non-fixture behavior ASTs match main in all nine modules. |
| #44 failed-copy recovery | `fix/44-fence-post-copy-failure-20261024`, `/home/kyle/kodi-lab-worktrees/44-post-copy-failure-20261024` | Commit `4c2f681`; [PR #58](https://github.com/kcook98765/kodi-lab/pull/58). INTEGRATION PASS: 31 application and 5 Web cases individually, then 121 workspace/broker interactions, Ruff/mypy, 58.9s. Retains the inventory-failure fence and uses explicit historical enrollment plus the existing full-tree proof. |
| #49 Bridge status | `fix/49-bridge-status-20261024`, `/home/kyle/kodi-lab-worktrees/49-bridge-status-20261024` | Commits `a77607f`, `0c45ff3`; [PR #55](https://github.com/kcook98765/kodi-lab/pull/55). Latest FOCUSED PASS: 2 browser/route and 6 existing control cases individually, Ruff/mypy and JS syntax, 14.4s. Follow-up requires fresh status after POST success while hidden. The frozen full candidate contains `a77607f`; the UI-only follow-up is qualified separately. |
| #46 baseline diagnosis | `fix/46-managed-baseline-reasons-20261024`, `/home/kyle/kodi-lab-worktrees/46-baseline-reasons-20261024` | Commit `98d382a`; [PR #57](https://github.com/kcook98765/kodi-lab/pull/57). FOCUSED PASS: 5 application and 3 Web/actual modern-and-legacy MCP cases individually, Ruff/mypy, 10.0s. Admission predicates unchanged; the complete observation-only preflight remains later. |
| #54 retained-report utility | `perf/54-report-summary-20261024`, `/home/kyle/kodi-lab-worktrees/54-report-summary-20261024` | Commit `113111e`; [PR #59](https://github.com/kcook98765/kodi-lab/pull/59). FOCUSED PASS: 19 cases, Ruff/mypy, 2.1s. Real before/after 247-case JSON reports summarized without rerunning tests. Independent of the frozen combined candidate. |
| #54 release watchdog | `fix/54-release-timeout-20261024`, `/home/kyle/kodi-lab-worktrees/54-release-timeout-20261024` | Commit `50975c8`; [PR #60](https://github.com/kcook98765/kodi-lab/pull/60). FOCUSED PASS: 3 wrapper cases, Ruff/mypy, 6.4s; Bash syntax passed. Defaults to the same 2,400-second value used in the real full gate below. Unit stubs check arguments, invalid input and owned cleanup; they are not release evidence. |

No merges or running-installation changes authorized. Each increment remains
isolated; main's timed baseline ended before the matched measurements.

Combined qualification completed **RELEASE PASS** at exact commit
`02c4054616598df41edb89cda955a4aa6095157f`, tree
`1cdb95bff744d012faace9f05417191a42a375cc`, in
`/home/kyle/kodi-lab-worktrees/54-batch-qualification-20261024`, branch
`qualification/54-batch-20261024`. The four source increments were copied into
that isolated candidate with ordinary cherry-picks; no PR or main merge occurred.
The candidate remained unchanged during its full gate. The documented timeout
override is 2,400 seconds for pytest, justified by main's 900-second timeout.
Log: `candidate-release.log`; exact source/tree and changed-file SHA256 manifest:
`qualification-source-manifest.json` in the durable evidence directory.

| Combined release stage | Time / result |
| --- | --- |
| Ruff format / lint | Passed, 1s / under 1s. |
| Full strict mypy | Passed, 33s. |
| Exact contract guards | 6 passed, 11s stage. |
| Full pytest | 5,272 collected: 5,234 passed, 38 gated skips, 3 warnings, no failures/errors; 1,709.27s pytest / 1,747s stage. |
| Package build / Alembic history / package-config import | All passed, 6s / 1s / under 1s. |
| Complete release gate | Exit 0, 1,799s (29m59s). |

All 1,745 tracked Git blobs/modes and 29 changed-file SHA256s matched the frozen
source after completion; the candidate was clean. Durable evidence:
`candidate-release-evidence/`, `candidate-release-summary.json` and
`candidate-release-ranked.json`. Peak pytest RSS: 2,741,352 KiB; minimum sampled
scratch free space: 7,072,378,880 bytes. Cleanup succeeded. The reported
16,291,782,097 logical bytes reclaimed is cumulative, not peak scratch usage.

Retained phase sums total 1,689.19s (setup 468.28s, call 1,130.98s, teardown
89.93s), distinct from wall time. Highest file totals identify the next profiling
targets without rerunning the gate:

| File | Cases | Summed phases |
| --- | --- | --- |
| `tests/repository/test_candidate_transition.py` | 47 | 237.07s |
| `tests/repository/test_origin_migration.py` | 21 | 108.40s |
| `tests/storage/test_remote_lifecycle.py` | 27 | 76.79s |
| `tests/storage/test_migrations.py` | 112 | 56.62s |
| `tests/application/test_repository_existing_adoption.py` | 74 | 52.37s |
| `tests/application/test_household_onboarding.py` | 61 | 49.95s, including 27.22s teardown |

The UI follow-up was then composed at
`2b33e1c45e2f2ef5017918732b3b1346318e8b25`, tree
`1cc1ab5fcd8a06fdfbcb69b68267909cd935d084`, branch
`qualification/54-final-ui-20261024`. Only the Bridge script/harness differ from
the release-qualified parent, and both match PR #55. **FOCUSED PASS**: 2 browser/
route plus 6 control cases, files individually, Ruff/mypy, 13.1s. Evidence:
`composed-ui-summary.json`, `composed-ui-focused.log`. This child does not claim
a repeated full gate. Report-tool and watchdog additions have their independent
focused qualification above. No live Kodi/DinD/acquisition acceptance was run.

All six increments compose without conflicts at
`cf28e9f5694a5a98e064f688c474ddcce02bb9de`, tree
`14852cc30c6600d3172216950c8fd79953cdfd06`, branch
`qualification/54-review-batch-20261024`. The additional report/watchdog modules
passed individually (19 + 3 cases), Ruff/mypy and Bash syntax, **FOCUSED PASS**
in 4.6s. Evidence: `composed-review-summary.json`,
`composed-tooling-focused.log`. The real release gate belongs to ancestor
`02c4054`; no full-gate claim is made for this later composed tree. The reviewed
default is the effective bound already exercised by that real gate. Wrapper
unit tests deliberately stub costly stages and cannot establish release success.

Final preservation check on 2026-10-25 04:45 UTC confirmed all 17 original
worktree HEADs/dirty states unchanged and all 210 original container identities,
names, images, states and health unchanged, with zero added/missing containers.
Main remains clean at `e198592` locally/remotely. Evidence:
`preservation-final.json`, `containers-final.txt`. Source PR branches and
qualification pins are pushed; no merge, deployment, production Kodi action,
security permission change or existing-resource deletion occurred.

Read-only follow-up audits completed during qualification: exact canonical/
recipe identity and admission gaps for #51; Kodi 19.5/20.5/21.3/22.0rc1 source
inspection of native ZIP builtin behavior; settings API/permission dependencies
for #52; and the next bounded deletion/lineage/resolution slice for #45. Primary
source links and proposal/evidence limits are in `PROGRAM-54-AUDIT.md`. None of
these later features was implemented or granted by the audit.

Existing #50-related log work is preserved on
`feat/nexus-closed-log-retention-20261023` (`09d847e`, `3dddbc2`), outside current
main. Coordinate with that branch before changing runtime/log retention.

During #44 focused validation, one new malformed-evidence fixture violated the
existing ordered-marker database constraint before reaching the service. Its
false `sync_completed` case now also clears the later `target_started` marker;
the production constraint remains unchanged. The original failure log is retained
as `recovery-fixture-marker-failure.log`. A following run passed all cases but
found fixture-only typing errors; explicit test-double casting and non-null
digest narrowing corrected those without production changes. Its log is retained
as `recovery-fixture-type-failure.log`. The settled focused lane passed.

## Blockers and next steps

1. Review the six independent PRs using the exact qualification pins above.
   Broader core validation and focused follow-up validation are complete; merging
   and deployment remain separately authorized phases. Keep their achieved
   levels distinct. Main's timed baseline is recorded; do not repeat it.
2. Use the retained full report and PR #59 to choose the next measured test-cost
   reduction. Preserve real migration/repository-transition proofs, assertions,
   private isolation and the complete release gate. No full-suite percentage
   improvement can be inferred from the incomplete main run.
3. Implement #45 next using the reviewed removal/preimage/lineage design in the
   audit. Private overlaps remain rejected until explicit resolution is supported.
   No force import, empty compatibility modules, baseline rewrite or workspace
   reset is a substitute for the reviewed tree.
4. Complete #46/#49's shared strictly read-only prerequisite/capability assessment
   and lifecycle-owned prepared-Bridge continuation. The delivered reason/UI
   slices do not implement those complete services. Preserve current grants and
   independent native, screenshot, Bridge and deployment eligibility.
5. Coordinate #50 with the preserved existing log-retention branch before adding
   bounded stopped-container evidence access. Original native crash cause remains
   unknown; do not manufacture a crash or infer one from warnings/exit metadata.
6. Implement #51's durable session reservation and admission across every relevant
   entry point, then Web composition over canonical exact targets/cached images.
   Follow the audit's failure/evidence-retention sequence; never evict or delete
   existing resources as an implicit migration into the new workflow.
7. #52 needs an active-deployment binding, reviewed settings operation and explicit
   permission design. #53 has no demonstrated unattended native installation
   route; retain the honest assisted alternative. #18 remains optional pending
   its managed-policy/protocol and resource qualification. No new grants or live
   installation/benchmark operations are authorized by the current work.
8. Keep #47/#48 deferred. Keep #8's fresh Windows/WSL account/host acceptance and
   #17's remaining native/remote evidence open. Native qualification, existing
   resource replacement, deployment and merging require their separate authority.

## Phase 2 continuation

The 2026-10-24 Phase 2 kickoff renews reversible code-only implementation,
commits, feature pushes and PR authority, and authorizes milestone comments on
#54. Main and all six PR heads were freshly verified; existing resources and
worktrees were inventoried without mutation. See
[Phase 2 integration review](PROGRAM-54-PHASE2-REVIEW.md) for exact heads, shared
files, review order, dependencies, qualification limits and next increments.

The progress ledger continues in isolated branch
`program/54-phase2-ledger-20261024`, based on PR #56, preserving the original
Phase 1 worktree. Phase 2 implementation starts with #45 in an independent
main-based worktree. No full gate is repeated for unchanged Phase 1 bytes.

### Phase 2 milestone: reviewed exact-tree staging

[PR #62](https://github.com/kcook98765/kodi-lab/pull/62), main-based branch
`feat/45-reviewed-exact-tree-20261024`, commit `0e1d913`: opt-in reviewed-tree
staging, explicit private choices, deletion preimages/tombstone lineage, complete
staged-result proof, retained old tree/baseline and replay of new/legacy receipts.
Default changes mode and existing grants/limits remain. Unsupported directory,
type and mode transitions remain explicit. A kept private difference reports a
reviewed merge, not source equality. Older code cannot consume new receipts;
the guide records that recovery/rollback limit. No Kodi profile was modified.

**INTEGRATION PASS**, 78.8s: each materially changed/new test module individually
(22 exact-tree, 17 original imports, 6 limits, 3 actual migrated-schema, 1 MCP wire)
plus Ruff/mypy, then 179 affected capture/edit/deployment/storage interactions.
Evidence: `phase2-20261024/exact-tree-integration.log` under the existing private
program evidence root. Synthetic 80-to-78 digest equality, old-tree retention,
private-only conflicts, stale inputs, interrupted publication, replay/restart and
concurrent edit checks passed. This head has no new release/native qualification.

Development corrections retained in separate logs: direct exploratory pytest on
disk was interrupted after partial passes and replaced by the standard tmpfs
lane; unsafe resolution input and fixture typing/description lint were corrected
before the settled pass. Partial runs are not qualification. One combined release
gate will cover settled Phase 2 shared changes rather than every PR separately.

Next: #46 shared read-only durable assessment, keeping fresh native/Bridge facts
unknown until independently observed; no runtime-status convergence or repair.

### Phase 2 milestone: shared durable deployment prerequisites

[PR #63](https://github.com/kcook98765/kodi-lab/pull/63), stacked on #57,
branch `feat/46-read-only-preflight-20261024`, commit `3409cb9`: shared Web/MCP
read-only durable assessment and fact/action DTO. Existing deployment permission,
credential, source/workspace/target grants, preset intersection and production
veto are reused and rechecked; mutation-time technical checks remain unchanged.
No runtime getter, broker/native probe, cleanup, repair or task scheduling occurs.
Native/add-on liveness and authenticated Bridge readiness remain unknown. This
foundation never returns ready or claims a reservation. Direct deployment gains
no Bridge requirement. Prior workspace evidence remains distinct from stale source
confidence; unsupported external/repository modes stay unknown.

**INTEGRATION PASS**, 53.7s: 17 application plus 2 authenticated Web/modern-and-
legacy wire cases individually, Ruff/mypy across all changed Python, then 122
authorization/deployment/baseline interactions. All six changed/new modules ran
individually (104 total), including exact catalogs, production exclusion, native
ZIP authority and existing MCP Web behavior; their initial lane reached a lint
failure after every module passed, followed by corrected static checks and the
settled integration lane. Typing/fixture/catalog corrections are retained in the
private evidence logs. No new release or runtime acceptance is claimed.

Next increment in progress: #51 migration and shared reservation/admission,
serializing opt-in session claims against every broker provision and start. The
new flow must refuse existing stopped containers, not count them as a free slot.
#49 lifecycle-owned prepared continuation and #50 bounded evidence remain needed
before claiming a complete fresh/version-switch development workflow.

### Phase 2 milestone: Bridge continuation and session admission

[PR #64](https://github.com/kcook98765/kodi-lab/pull/64), commit `f6fe7dc`,
stacked on #55: explicit Web-only prepared-reviewed continuation binds displayed
trust generation and confirms fresh identity synchronously under the live
lifecycle lease. Existing secret retirement/build/release/CAS checks remain.
No restart/restaging or MCP authority. **INTEGRATION PASS**: 14 recovery plus
3 Web/browser tests individually, Ruff/mypy/JS syntax, then 48 trust/restore/
lifecycle/broker/Web-isolation cases; 21.4s. The real broker lease checker is
exercised against fixture ORM bytes; do not call that a migration/native proof.

[PR #65](https://github.com/kcook98765/kodi-lab/pull/65), main-based session
admission foundation: parent `11227e2` achieved **INTEGRATION PASS**, 35.3s,
with 13 session, 2 real migration, 1 production-registry cases individually and
123 protocol/authorization/provision/capacity/agent/workspace interactions.
The complete 112-case migration and 3-case current-schema OSTree modules also
passed individually. Migration 0080 is empty/opt-in and retains populated
history/reservations on downgrade. No existing resource is enrolled or removed.

The settled child `0eb5790` tightens uncertainty: current absence does not prove
that a timed-out daemon create cannot finish late. Unknown provision remains
fenced across restart and cannot be closed or cleared through an absence-only
API. Only known image/network preflight refusals raised before any create free
their slot. **FOCUSED PASS**: 14 session plus 1 production-registry tests, files
individually, Ruff/mypy, 10.1s. Parent integration and this child-focused evidence
remain distinct until the combined release candidate. This is an intentional
fail-closed limit, not a complete uncertain-dispatch recovery implementation.

Next: minimal Web reservation/Prepare/Resume/Stop over the shared binding. No
Fresh Test/Switch/profile replacement will be represented as ready while retained
diagnostics and dispatch-settlement prerequisites are incomplete. #50 log policy
continues to reuse the preserved branch; no new truncation/recipe work duplicates
it. Prepare native sequential/version-switch and Windows acceptance only after
separate operational authorization.

### Phase 2 milestone: minimal Web workflow and stopped exit metadata

[PR #66](https://github.com/kcook98765/kodi-lab/pull/66), `2688e04`, stacked
on #65: Web Reserve/Prepare/Resume/Stop and ordinary absent/idle close over one
exact session, with claim-time runtime and pre-dispatch identity rechecks. No
Fresh Test/Switch/deletion, acquisition, new MCP tool or grant. Initial integration
passed 5 workflow + 4 Web/Node cases individually, then 51 lifecycle/navigation/
admin-view/isolation interactions, 46.4s. After #65's uncertainty tightening,
settled **FOCUSED PASS** repeated the 9 cases plus static/JS checks in 12.1s.
Rebase preserved tree `e7a4f982419dea00565c81dd0129c3de45fc9daf` exactly.
Source/Bridge/native readiness still use existing explicit setup actions.

[PR #67](https://github.com/kcook98765/kodi-lab/pull/67), `8833c30`, supplies
#50's first Web-only exact stopped-container exit/OOM/timestamp metadata read.
Independent target/artifact/owner/container/image/state checks run twice; storage
and client/provider result identities are rechecked. No file/log bodies, arbitrary
path, shell, native wake, runtime convergence or grant change. Cause remains
unknown: exit numbers alone do not prove a stack trace or normal Lab stop. The
fixed broker operation catalog remains exact, now 39 entries. Preserved log-
retention commits/recipes/rotation remain unchanged and unpublished.
**INTEGRATION PASS**, 23.3s: 18 broker, 1 Web and 15 exact-catalog cases individually,
Ruff/mypy, then 46 protocol/resolver/control interactions. Three additional changed
catalog modules passed individually (61 cases), Ruff/mypy, 17.0s. No native crash
manufacture or Docker/Kodi fixture ran. Historical operation linkage, retained
log bodies, crash reports and symbolication remain later work.

Pre-release contract review also updated #63 to `c4dc028` (stdio/external exact
catalogs: 7+7 focused cases, Ruff/mypy, 41.0s) and #65 to `31a01af` (exact current
metadata plus preserved historical 0076 downgrade proof on matched current broker
schema, modules individually, Ruff/mypy, 11.0s). No application bytes changed in
these test-only follow-ups; original security/transition assertions are retained.

Next: compose all Phase 1 and Phase 2 published heads in a fresh qualification
worktree using ordinary cherry-picks, resolve shared imports/docs explicitly,
freeze the source/tree, then run one complete release gate. This remains source
qualification only; main and all running resources stay untouched.

### Phase 2 milestone: frozen replacement release candidate

Composition of the published Phase 1 and Phase 2 application heads used ordinary
cherry-picks in `qualification/54-phase2-20261024`. Code composed automatically;
the only shared conflict was appended decision records. All six records D0443
through D0448 were preserved exactly from their current feature heads.

During publication review, a reproducer exposed cancellation after a committed
exact-tree receipt rolling the filesystem back while the durable generation had
already advanced. The first full run at `c316c89` was deliberately interrupted
after 550 partial passes (pytest exit 2); it is **not RELEASE PASS**. Its
partial evidence and manifest are retained independently. #62 follow-up
`43a332f` checks exact durable publication state: confirmed commit keeps the
published tree, proven unpublished ordinary failure can roll back, and unknown
readback retains both trees and fences recovery. Recovery proves the tree and
acknowledges an already-advanced receipt without incrementing twice.
**INTEGRATION PASS**, 80.0s: 51 focused cases individually plus 179 interactions,
Ruff/mypy. The stronger actual SQL readback-failure regression then achieved
**FOCUSED PASS**, 24 cases and static checks, 9.8s. Logs retain the failing
reproducer and all corrected runs.

The replacement candidate is frozen at
`f6449a432a74837fdfaf67d4cd23181716775590`, tree
`3192fbccb66e6a23f0ae790efadd70561137a510`. Its full release gate is in
progress; there is no result yet. The manifest pins all 1,771 tracked entries,
65 changed-file hashes and clean status. No competing tests or acceptance run
while this gate owns the qualification slot. Later independent changes must
retain their own qualification boundary.

Preservation review before the gate matched all 24 original worktree HEADs and
tracked dirty states and all 210 container identities/names/images/states.
An initial composition command accidentally entered the primary checkout and
conflicted on the first documentation commit before HEAD advanced. Immediate
`git cherry-pick --abort` restored clean main at `e198592`; the original stash
remains. Composition then used the explicit qualification worktree. This recovery
is retained in `composition-cwd-recovery.md`; no merge or runtime change occurred.

Next independent increment: #49 read-only capability prerequisites, keeping
stored authenticated identity distinct from current liveness, native fallback,
log capability, launch build requirements, screenshots and native remote access.
This will be developed outside the frozen candidate, with focused/integration
checks only after the full gate releases the test slot. Fresh/Switch remains
blocked on retained evidence and independently proved uncertain-dispatch
settlement; current absence alone remains insufficient.

### Phase 2 release milestone: pinned combined candidate qualified

**RELEASE PASS** belongs to commit
`f6449a432a74837fdfaf67d4cd23181716775590`, tree
`3192fbccb66e6a23f0ae790efadd70561137a510`: **5,354 passed, 38 gated skips**,
complete gate **29m54s**, process exit 0. Ruff format/lint, strict mypy, six
contract guards, the full unfiltered suite, package build, Alembic history and
package/config import all passed. Pytest reported 1,738.12s; its wrapper stage
including shutdown/cleanup was 29m36s. These are distinct elapsed measures.
All three exact run-owned scratch roots were removed, and post-gate HEAD, tree,
1,771 tracked entries and changed-file hashes match the frozen manifest.

The 38 skips retain their explicit image/root/OSTree/acquisition/isolated-LAN
acceptance gates. No tests or security, migration, identity or regression checks
were removed. This includes all published application heads #55–#60 and #62–#67,
with decision-record composition proof retained. It does not transfer to later
application changes. Phase 1's separate `02c4054` RELEASE PASS (5,234/38, 29m59s)
and the interrupted `c316c89` partial run remain separate evidence.

Private evidence: `phase2-20261024/release-evidence/` contains stage TSV, full
pytest and contract JSON/JUnit; `phase2-release-result.json` binds counts, stages,
skip reasons, exact source and removed roots. `release-cost-summary.txt` uses
#59's retained-report tool without rerunning tests. The largest recorded phase
costs remain repository candidate transition (234.13s), origin migration
(108.41s), storage lifecycle (77.60s) and full migration history (58.48s). These
phase sums are not a second wall-time measurement or a reason to weaken checks.

Post-gate preservation matched all 24 original worktree HEADs/tracked states and
all 210 container IDs/names/displayed images/states using the same inventory
fields as the initial snapshot. A preliminary comparison mixed inspect image
IDs with displayed image tags; matching the original fields confirmed no change.
The preserved log branch, primary clean main and original stash remain intact.
No merge, running-Lab change, grant change, native acceptance or existing-resource
removal occurred.

The later #49 capability-prerequisite increment is outside this pin; its own
focused/integration result will be recorded separately. No second full release
run is scheduled for that observation-only extension.

### Later increment: saved capability prerequisites (#49/#51)

[PR #68](https://github.com/kcook98765/kodi-lab/pull/68), head `2080f4e`,
stacked on #64: shared SELECT-only managed capability-prerequisite projection,
authenticated HTML/JSON and a link from instance controls. Native readiness,
current container binding, prompt state and all live capability readiness remain
unknown. Candidate and last approved authenticated versions are distinct.
No-record native fallback, non-active/drifted denial, log capability and current
launch-build requirements remain separate; screenshots/native remote/direct
deployment gain no Bridge prerequisite. No MCP tool/schema, grant, secret read,
probe, repair, migration or scheduler.

**INTEGRATION PASS** at `2080f4e`, 23.4s: each new module individually (22
application, 2 Web), Ruff/mypy, then 56 trust/recovery/log/lifecycle/Web cases.
SQL tracing permits only SELECT; forbidden runtime/broker/native/secret doubles
guard the status boundary. Covers exact RC1, historical identities, missing or
unsupported profiles, malformed saved identity, runtime drift and auth. An
initial test-only typing failure after all 24 tests passed was corrected before
the settled lane; its log is retained.

Separate composition `24c1aeb` on
`qualification/54-capability-overlay-20261024` overlays the release pin without
changing its worktree. **INTEGRATION PASS**, 48.9s: same 24 cases individually,
static checks, then 20 stopped-diagnostics/session/Bridge/UI-MCP interactions.
Adjacent additions conflicted in `web/app.py`; both complete route blocks were
preserved. AST comparison proves every previous route function is unchanged,
and exact new route/decision hashes are retained. D0443–D0448 remain unchanged
and exact D0449 is appended. An initial nonexistent test-path selection failed
argument validation before any tests and was corrected.

**Neither `2080f4e` nor `24c1aeb` has RELEASE PASS.** The complete release result
remains attached only to `f6449a4`/tree `3192fbc`. This later observation-only
change has the integration evidence above. No native/Windows acceptance is
inferred. Both branches are pushed; no main merge or deployment occurred.

Next planned increment: #50 fixed bounded approved log/crash artifact reads and
private retention tied to an exact operation/runtime/container, coordinated with
the preserved closed-log/rotation branch. Explicit size/count/age, redaction and
access semantics must be reviewable before enabling retained bodies. Then #51
Fresh/Switch needs that evidence plus independently proved dispatch settlement,
explicit old-profile disposal review and fresh trust/baseline bindings. The
current Prepare/Resume/Stop flow preserves stopped/failed profiles; uncertainty
cannot be closed from absence or elapsed time. New settings permissions (#52),
native ZIP feasibility (#53), merges/deployment, existing resource disposal and
native/Windows acceptance retain their grouped decisions in the review.

### Continued increment: durable stopped-log retention (#50/#51)

[Draft PR #69](https://github.com/kcook98765/kodi-lab/pull/69), branch
`feat/50-retained-stopped-diagnostics-20261025`, parent `9bffcb0`, based on the
existing published composition `24c1aeb`: fixed stopped internal-log tails and
Web-selected durable captures. New empty migration 0081 follows 0080. Exact
request replay, source/runtime/container/image checks, standard private profile,
source/archive/tail byte bounds, full-input redaction before truncation, private
Web-only bodies, preserved reconciliation fences and state/digest-bound discard.
Capture history has no live-resource FK and survives target removal. No automatic
expiry/prune/overwrite, grant, native run or existing-resource mutation.

**INTEGRATION PASS** at `9bffcb0`, 120.4s: seven modules individually (29 broker,
25 retention, 2 real transition, 3 Web, 18 foundation, 3 exact current-schema
OSTree and 112 full migration cases = 192), Ruff/mypy, then 166 affected
catalog/diagnostic/protocol/audit/session/Web/migration interactions. The real
0080-to-0081 transition preserves an uncertain provision reservation, accepts
a real capture and refuses evidence-losing downgrade. Cancellation after commit
keeps the published receipt; abandoned read completion cannot publish.
Installed-SDK raw HTTP and real broker socket fixtures remain distinct from
actual Docker/Kodi or Windows acceptance. Private logs retain the corrected
fixture/type/lint/path-selection failures.

The preserved `3dddbc2` log branch remains unchanged/unpublished. The new read
does not duplicate its truncation, recipe or Docker rotation configuration.
Primary/qualified source branches remain unchanged; no production migration ran.
This later head is outside `f6449a4`'s RELEASE PASS and needs a settled broad
release candidate after its remaining additions.

Next addition in progress: a bounded non-following Docker stdout/stderr window,
strict multiplex decoding and explicit availability, within the existing fixed
read. Preserve schema-1 captures when introducing the expanded snapshot shape.
Native crash-report discovery remains uncollected: do not turn absence of a
bounded directory listing into proof of no crash report, or use generic home-
directory export/paths. Fresh/Switch and agent historical-body access still need
their exact evidence, permission and disposal decisions; no capture authorizes
profile disposal or settles an uncertain create.

### Continued increment: bounded Docker output and private review hardening

Draft #69 now includes child `80605de`: schema-2 five-artifact snapshots add a
fixed non-following timestamped Docker stdout/stderr window (200 lines, 64 KiB
encoded response, strict multiplex/TTY decoding, full-window redaction before
a 4 KiB tail). Schema-1 saved captures remain readable. Shared/profile mounts
are refused, actual database-filesystem reserve is checked, known broker errors
use fixed safe codes, and retain/discard require actual boolean confirmation.
Native crash reports remain uncollected; no lifecycle or agent authority added.

**INTEGRATION PASS**, 52.9s: 39 broker, 33 retention, 2 real migration and 3 Web
cases individually (77), static checks, then 89 interactions. An additional
Web private-evidence/CSRF/strict-discard case then passed with the complete
four-case module/static lane, **FOCUSED PASS**, 8.2s. These results are later
than the pinned `f6449a4` release result and do not inherit RELEASE PASS.
Keep #69 draft until a settled combined persistence/security candidate receives
the complete gate. No native Docker/Kodi/Windows acceptance is inferred.

Next #51 increment: bind an explicit Fresh/Switch review to the exact stopped
session, retained evidence and selected approved cached runtime. Retained bodies
must not disappear between review and retirement; old-session exclusion must
remain active until an atomic new reservation follows independently confirmed
old-container retirement. Existing lifecycle operation history alone lacks an
exact original-container witness. Unknown creates remain fenced; missing
inventory, restart and elapsed time cannot settle their dispatch.

### Continued increment: exact Fresh/Switch review and evidence holds

[Draft #70](https://github.com/kcook98765/kodi-lab/pull/70), head `782b33f`,
stacked on #69 `80605de`: one durable explicit review binds the stopped current
session and workspace generation/tree/source/add-on, immutable next cached
runtime/image and 1–8 selected capture IDs/digests. New empty 0082 adds review/
hold tables. A hold and explicit discard serialize on the SQLite writer boundary;
one open review, bounded history/snapshot/storage, exact nonce replay and
digest-bound cancellation preserve receipts. Open review blocks absent-session
release. Web forms and JSON retain admin/auth/origin/CSRF and strict inputs.
No lifecycle, source/baseline/trust copy, grants, worker or scheduling is added.

**INTEGRATION PASS** at `782b33f`, 107.4s: six modules individually (31 application,
2 real migration, 3 Web, 18 foundation, 3 current-schema OSTree and 112 full
migration cases = 169), Ruff/mypy, then 61 retention/session/shared-admission/
Web/Node interactions. Includes competing reviews, review-versus-discard,
post-commit cancellation/replay, unknown provision and preserved reconciliation,
exact evidence/hash/schema checks, private origin/CSRF forms and populated
0081→0082/FK/UTF-8-bound/lossy downgrade proof. Initial test-only fence setup
and lint/type issues were corrected. A direct test-only mypy invocation without
the lane's required MYPYPATH was discarded; the supported settled lane passed.

Reverified all 24 original worktree HEAD/status pairs, the original stash and
all 210 container ID/name/display-image/state tuples unchanged. Remote main
remains `e198592`; preserved log branch remains exact `3dddbc2`. No operational
mutation, production migration, native or Windows acceptance occurred.

#69/#70 remain draft, outside `f6449a4` RELEASE PASS. Next is exact original-
container retirement proof under existing lifecycle lease/terminalization and
atomic next-session admission. A review alone cannot authorize disposal or
settle uncertain dispatch. Schedule one settled combined broad gate after these
shared persistence/lifecycle changes settle, not a release run per small PR.

### Continued increment: scoped retirement receipts and combined qualification

[Draft #71](https://github.com/kcook98765/kodi-lab/pull/71), head `cbc8894`,
stacked on #70: optional internal scoped deprovision checks exact disposable
target/runtime/container/image before claim and again inside the leased action
before trust/evidence invalidation. Empty 0083 stores prepared intent with the
child operation. A matching broker removal reply plus unprovisioned postcondition
permits confirmation in the same owned transaction as lifecycle success and
lease release. Callback failure rolls back terminalization; post-commit
cancellation preserves confirmation. Referenced operation history is protected
from pruning. No historical backfill, public route, broker catalog/grant change,
successor admission, live execution or uncertain-create settlement is added.

**INTEGRATION PASS**, 148.9s: 20 receipt/lifecycle, 3 real migration, 18 foundation,
3 current-schema OSTree and 112 full migration cases individually (156), static
checks, then 134 lifecycle/history/claim/resource/agent/Bridge/session/review/Web
interactions. Covers reply loss, wrong reply, remaining-container postcondition,
interrupted dispatch, stale post-claim identity, ownership loss, publication
rollback, committed cancellation, bad historic evidence and pruning protection.
Actual temporary 0082→0083 preserves prior review holds, captures and unscoped
history; neither unknown nor confirmed receipt history permits lossy downgrade.
Test-only fixture signature and type/lint failures were corrected separately.

One settled combined full gate **is now running**, started
2026-10-25T07:47:37Z, on frozen/pushed
`qualification/54-retirement-20261025`:
`cbc889429138df19ed11ebf09706541313545c2d`, tree
`c262488b8d780420540168c5fda69789a3c2557b`. Manifest records 1,794 tracked entries
and SHA-256 for 45 files changed since `f6449a4`. This contains #68–#71 over the
earlier release pin. No competing tests/acceptance run during qualification.
Until all stages and cleanup finish successfully, this candidate has only the
focused/integration results above, **not RELEASE PASS**. Evidence/result/log are
private under the Phase 2 evidence root. The prior `f6449a4` and Phase 1 release
pins remain unchanged and retain their own exact qualification boundaries.

Continue next in a separate isolated worktree: bind reviewed intent to the exact
retirement operation, keep evidence held during dispatch/uncertainty, then close
old session plus reserve successor plus consume review in one SQLite commit.
Final execution needs explicit exact data-loss confirmation and fresh checks;
actual existing-resource operations remain unauthorized. Preserve unknown-create
fences, source/workspace/history, immutable pins and independent trust/baselines.

### Combined retirement qualification completed

**RELEASE PASS** only at `cbc889429138df19ed11ebf09706541313545c2d`, tree
`c262488b8d780420540168c5fda69789a3c2557b`: **5,515 passed, 38 gated skips**, all
eight stages/process exit 0, **32m12s** full gate. Pytest reports 1,839.63s;
its wrapper stage took 1,879s including shutdown. All three owned scratch roots
were removed. Peak RSS 2,859,764 KiB; minimum free scratch 6,570,737,664 bytes.
After completion, all 1,794 tracked entries, all 45 changed-file SHA-256 values,
clean HEAD and exact tree were independently reverified against the manifest.

This pin composes #68–#71 over `f6449a4`. #69, #70 and #71 are now ready for
review; their standalone focused/integration evidence remains separate from this
combined qualification. Ready for review is not approval, merge, deployment or
existing-resource authority. The earlier `f6449a4` (5,354/38, 29m54s) and Phase 1
`02c4054` (5,234/38, 29m59s) remain unchanged, separately qualified pins.
No native Docker/Kodi/Windows acceptance is inferred.

Later work is isolated on `feat/51-atomic-session-handoff-20261025`, outside this
release result: explicit final replacement consent; exact review/retirement
binding; atomic close/reserve/policy/consumption; clean stopped preparation;
confirmed-retired continuation/release and no unknown-create escape. Source and
temporary-DB/Web tests were written while the frozen full gate ran, without
competing test execution. Focused checking is now beginning; do not attribute
RELEASE PASS or a successful focused result to these later uncommitted bytes.

### Continued increment: explicit atomic Fresh Test / Switch workflow

[Draft #72](https://github.com/kcook98765/kodi-lab/pull/72), head `9067899`,
stacked on #71: explicit final data-loss consent, exact review/retirement binding,
atomic old closure/successor reservation/policy/consumption, then existing
lifecycle/shared-admission clean stopped preparation. Status/replay does not
redispatch. Unknown retirement/create keeps existing fences. Explicit continuation
only advances confirmed retirement; explicit release only closes its proven
retired empty slot while retaining saved captures/history. Source/workspace/Git/
runtime images persist; source grants, Bridge trust and baselines are not copied.
The session selectors reflect the actual switched version/workspace.

**INTEGRATION PASS** at `9067899`, 162.8s: nine modules individually (25 core,
3 new real predecessor migration, 2 historical 0082, 3 0083, 4 new Web, 4 existing
Web/Node, 18 foundation, 3 current-schema OSTree and 112 full migration cases =
174), Ruff/mypy, then 163 review/receipt/admission/retention/lifecycle/agent/
Bridge/Web/security/catalog interactions. Covers sequential distinct version pins
with at most one profile, concurrent nonces, held evidence, exact final consent,
atomic rollback, post-commit cancellation, revoked runtime/capacity and unknown
create/removal. Runtime acquisition/eligibility is doubled where noted; this is
not native sequential-version or Windows acceptance.

Real 0083→0084 and populated-review round-trip preserve captures/holds and verify
parent-deletion refusal, generation constraints and execution-losing downgrade
refusal. Historical 0082/0083 fixtures use their actual columns; current behavior
uses the matched head. An initial inline-FK reflection mismatch failed the
unchanged exact Alembic comparison. The new successor reference now uses SQLite's
default immediate NO ACTION; delete refusal, foreign keys and schema checks stay
enabled. Other test-only fixture reply/type/lint failures were corrected. No
production migration, resource mutation, merge or deployment occurred.

#72 remains draft and is **outside** the `cbc8894` RELEASE PASS (5,515/38,
32m12s). Next independent increment: bounded metadata for strictly approved
native crash-report names explicitly referenced by collected logs. Official
Nexus/Omega launcher sources confirm that such references exist; this does not
prove a current report, crash cause or supported stack. Avoid general directory
exports, arbitrary paths, report-body exposure or agent permission changes.

### Continued increment: bounded referenced crash-report metadata

[Draft #73](https://github.com/kcook98765/kodi-lab/pull/73), head `27abf6f`,
stacked on #72. Schema 3 captures retain HEAD-only size/time/state metadata for
at most two strict `/home/kodi/kodi_crashlog-YYYYMMDD_HHMMSS.log` names explicitly
reported by complete retained log lines. No directory listing or report-body
read, arbitrary path input, new permission or cause inference. Schema 1/2 saved
captures remain readable; private Web evidence survives explicit disposable
profile replacement and retains the original identity. Upstream pinned Nexus
20.5/Omega 21.3 launcher formats were checked and hashed privately, without
claiming those launchers ran here. Unreferenced reports remain unobserved.

**INTEGRATION PASS**: 91 cases across four individually focused modules, then
115 interaction cases, 78.4s, including installed Docker SDK HTTP HEAD-only
fixtures and durable session replacement/private evidence interactions. No native
Docker/Kodi was launched. #72/#73 remain outside the `cbc8894` RELEASE PASS;
qualify one settled combined candidate after the next bounded admission review.

Next: inspect runtime workspace deploy/execute/launch admission against the
selected session workspace/source. Any implementation must only narrow an
explicit active session, preserve existing grants/trust/baselines and replay,
and leave offline workspace editing and external development behavior intact.

### Continued increment: selected workspace runtime admission and preflight

[Draft #74](https://github.com/kcook98765/kodi-lab/pull/74), head
`557dcdddac66947e41dc6aabb85b4c4b013bba07`, stacked on #73. Fresh managed
workspace deploy/execute/GUI launch now requires the active bound session's exact
target/runtime/workspace/source; observed container is checked before mutation.
Uncertain/reserved phases or pending provision/start refuse. Shared checks run
inside existing writer claims before typed mutation leases; broker workspace-copy
resolution independently checks the selection. No grant, attachment, baseline,
trust, permission or readiness is supplied by selection. Historical receipt
replay, offline editing and separate external development paths retain their
existing behavior. Administrator source preparation/recovery remains separate.

The read-only deployment preflight exposes stored session conflicts and a closed
`review_session` administrator link. It creates no reservation and leaves live
container/native/Bridge/add-on state unknown. No migration or broker/MCP operation
count change. Initial import-cycle and test-only timestamp/type/lint failures
were corrected without weakening existing constraints. The storage policy's
broker record import is now type-only, keeping the broker resolver import safe.

**INTEGRATION PASS**: settled admission boundary 38 individually focused cases
plus 221 workspace/launch/session/replacement/recovery/repository/broker
interactions, 105.4s. Final preflight alignment: all 48 focused cases/static checks
across six sources, then 44 preflight Web/MCP and production authorization/schema
interactions, 58.4s. Covers real temporary upgraded schema, source/runtime/container
drift, pending/uncertain reservations, post-claim partial policy restoration,
replay, offline edits, external targets and SELECT-only assessment. No native
Docker/Kodi was launched. Private logs retain each separate lane/failure.

Preservation reverified: main `e198592`, all 24 original worktree HEAD/status
pairs, original stash, all 210 container ID/name/display-image/state records,
and fixed log branch `3dddbc281a4796f841e056688c340cd83b276341` are unchanged.

A single settled combined full gate is now running on frozen/pushed
`qualification/54-session-20261025`, exact head
`557dcdddac66947e41dc6aabb85b4c4b013bba07`, tree
`611eb6f8bf7820f0bf0e280ee351966b9169bc6b`. Manifest records all 1,802 tracked
entries and SHA-256 for 38 changed files since `cbc8894`; it composes #72–#74
above that prior release pin. No competing tests/acceptance run during this gate.
Until all stages/process/cleanup complete, this candidate has only its focused/
integration evidence, **not RELEASE PASS**. Earlier exact qualifications retain
their separate boundaries, including Phase 1 `02c4054` (5,234/38, 29m59s).

Next independent source increment, in a separate worktree while qualification
runs: a read-only selected-session preparation checklist using stored preflight
and Bridge prerequisites. Bind composition to one exact generation and refuse a
switch rather than mix identities; no probe, secret read, automatic enablement,
new agent grant or settings/managed-screenshot permission. No existing-resource
operation, merge, deployment or production migration is authorized.

### Qualification harness correction; exact source pin unchanged

The first `557dcdd` attempt accidentally forced disk-backed `/tmp` rather than
the previously successful standard automatic tmpfs selection. Its first 240
cases passed, but at 9m37s the process was repeatedly in `jbd2_log_wait_commit`
with substantial I/O pressure. This was a harness configuration error, not a
product failure or RELEASE PASS. The exact owned pytest PID received SIGINT;
wrapper exit 2 and complete removal of all three owned scratch roots were
verified. Partial JSON/stages/log/result are retained privately as the disk
attempt; no history or evidence was overwritten.

The same frozen `557dcdddac66947e41dc6aabb85b4c4b013bba07` / tree `611eb6f`
is now rerunning with standard harness temp auto-selection. Source manifest,
HEAD/tree and changed-file hashes rechecked before dispatch. No tests, reserves,
assertions, stages, default watchdog or product bytes changed. Snapshot scratch
continues on the normal filesystem with its production 2 GiB reserve. No
competing tests/native acceptance run during this corrected full gate.

The next preparation-checklist worktree now contains uncommitted source/docs and
two new test modules, still **unvalidated**. It composes preflight/reconciliation/
Bridge prerequisites in one explicit read snapshot via borrowed read sessions;
no runtime probe, scheduler, grant or schema change. Existing Web authentication
last-use tracking and bounded retention middleware remain in place. Static parse,
Ruff format/lint and diff checks passed; no focused/integration or release claim
is made for these later bytes. Its tests wait for the frozen full gate to finish.

### Queued regression review: exact current-profile deployment lineage

Source review found that execution success lookup is keyed by workspace, target
and source generation, while prior workspace-copy lookups match runtime but do
not scope the original profile. A same-version Fresh Test or switch away/back
can reuse those identities while retaining old receipts. Existing live/broker
checks may reject later; this is not evidence of a native bypass or incident.
Potential stale prerequisite selection and false multiple-success ambiguity need
focused proof before a fix is claimed.

Six new regression cases are queued in the isolated
`51-profile-deployment-lineage-20261025` worktree (base `557dcdd`): old-only
execution refusal, unambiguous current-profile success with retained history,
historical original-request replay, GUI receipt scope, read-only baseline reason,
and refusal before stop/sync when only old profile lineage exists. They use
temporary SQLite/fake positive transports and simulate the tested handoff's
stored outcome; no native acceptance is inferred. Static Ruff passed; tests have
**not run**, no production fix exists and no PR is published for this increment.
Private review is `session-profile-lineage-review.md` under the Phase 2 evidence
root. Future resolution must retain old receipts/replay and legacy/external
semantics, with no new grant/tool/table or guessed latest unrelated operation.

### Prototype receipt scoping prepared; before/after checks remain queued

The lineage worktree now has an uncommitted correlated SQL selection predicate
for active managed sessions. Fresh execute/recheck, GUI launch, preflight/prior
copy and broker running-smoke authority counts use the exact selected original
container/runtime/workspace/source/target. Existing historical
`prior_workspace_copy`, replay/recovery/promotion and retention paths are left
separate. Legacy inactive and external behavior are intended to remain unchanged;
this is an unvalidated prototype, not a compatibility result. D0457 records the
choice, independently of the checklist branch's D0456.

Nine queued cases include nullable historical bindings, a current receipt with
retained same-generation history and the real SQL smoke authority resolver,
historical exact request replay, GUI scope, baseline reason/early refusal,
inactive legacy nullable behavior and current-source-baseline broker fallback.
An untouched detached `557dcdd` worktree contains the identical new regression
file (SHA-256 recorded privately) for the before/after comparison. No regression
failure or fix PASS has been claimed; neither prototype has been executed while
the full gate runs. Static parse, Ruff and diff checks passed. No PR is published
for either later increment, and neither inherits the frozen qualification.

### Combined session qualification failed; evidence and preservation verified

The standard-temp full attempt ended 2026-10-25T09:44:02Z at frozen `557dcdd` /
tree `611eb6f`: **RELEASE FAIL**, 5,617 passed, 1 failed, 38 gated skips,
32m09s wrapper attempt. Pytest reports 1,877.49s; its wrapper stage took 1,919s.
Ruff format/lint, mypy and contract guards passed; pytest exit 1 prevented package
build, Alembic history and package/config import. All three owned scratch roots
were removed. All 1,802 tracked entries, all 38 changed-file SHA values, exact
HEAD/tree and clean status were reverified. Peak RSS 3,189,104 KiB; minimum free
scratch 6,329,876,480 bytes. Private verified result is
`session-release-failure-verified.json`; full evidence remains intact.

The single failure was the recovery-coordinator migration test: it invoked the
current fresh-deployment producer on historical 0078, before 0080 session-policy
tables exist. The new matched-head guard correctly cannot run against that older
schema. The proposed test-only correction seeds actual 0078 interrupted-receipt
columns, then runs the real recovery coordinator and retains all outcome,
integrity/FK checks; additional assertions keep the database at 0078 and prove no
session-policy table was created. No missing-table bypass or production guard
change is proposed. Focused cases passed; import formatting was corrected and
the complete selected integration lane is running. No correction PASS yet.

Main `e198592`, all 24 original worktree HEAD/status pairs, original stash, all
210 container identity/state records and fixed log branch `3dddbc2` were again
verified unchanged after the failed gate. #72–#74 remain draft. The latest
completed release result remains separately pinned to `cbc8894` (5,515/38,
32m12s); Phase 1 remains `02c4054` (5,234/38, 29m59s). Group the next full gate
only after the fixture correction and later checklist/lineage source settle.

### Historical recovery-fixture correction and profile-lineage increment

#74 now has test-only follow-up `79d7d2410a24bfaf3650df2d7c117a3ff2f47d52`:
actual 0078 interrupted receipt data is seeded before invoking the real recovery
coordinator. No current guard or production code was disabled. **INTEGRATION
PASS**, 3 historical migration + 48 admission focused cases, static checks and
164 recovery/full-history/foundation/OSTree interactions in 102.2s. This corrects
the failed frozen-gate test fixture; no successful full rerun is claimed.

[Draft #75](https://github.com/kcook98765/kodi-lab/pull/75), head
`d56e30a035bd015d9dce537a6713cf9157a675ee`, follows #74. A shared correlated
SQL predicate requires exact original-container/runtime/target/workspace/source
for fresh managed deployment lineage when a session is active. Execute/recheck,
GUI launch, preflight/prior workspace-copy and running-smoke authority counts
agree. Old same-version/source-generation receipts remain historical; inactive
legacy and external semantics, exact original-request replay, recovery/promotion
and reference-safe retention remain separate. No tool/grant/table or guessed
latest unrelated operation, and no live tree/identity check was removed.

The identical corrected nine-case file was run against untouched `557dcdd`:
7 behavior failures / 2 legacy-history passes. With the fix all 9 passed.
Initial extra-argument fixture errors were corrected on both sides and retained
separately; they are not counted as product regressions. Broader **INTEGRATION
PASS**: 60 focused cases (9 lineage, 48 admission, 3 real historical migration),
Ruff/strict mypy across nine files, then 238 core/launch/preflight/recovery/session/
repository/broker/Web interactions, 128.8s. The new early refusal checks assert
precise `execution_not_deployed`/`launch_not_deployed` and no allocated receipt;
all no-dispatch/no-lease assertions remain. Test-only expectation spelling issues
were corrected. Real SQL smoke-authority and broker baseline resolvers participate
with fake Docker/native transports; no native acceptance or incident diagnosis.

The source manifest/test SHA and before/after logs are recorded privately in
`profile-lineage-comparison.json`. The checklist and lineage worktrees adopted
the exact #74 test-only follow-up before testing, preserving uncommitted source.
The checklist's focused and integration lane is now running; no result or PR is
claimed yet. Both new increments remain outside the failed `557dcdd` gate and
the older successful `cbc8894` qualification. Continue to the explicit selected
source-copy foundation after these checks, then qualify a settled composition.

### Read-only selected-session preparation checklist published

[Draft #76](https://github.com/kcook98765/kodi-lab/pull/76), head
`2ebe7e1ff88f416d4e8b706294078237d66e93c0`, follows #75. The ordinary session
page links to one authenticated uncached checklist for the selected add-on and
Kodi version, with stored source attachment/baseline/validation, operation-history
and Bridge prerequisite guidance. No runtime probe, setup dispatch, credential
read, scheduler, new tool/grant/table or automatic enablement. Live container,
native/Bridge/dialog/add-on state remains unknown; agent authority is explicitly
not assessed. Existing Web session tracking/retention maintenance is preserved.

One explicit read transaction is borrowed across the existing preflight,
reconciliation and capability projections, keeping a coherent snapshot across a
concurrent switch. A stale requested generation or mixed stored identity refuses;
a coherent snapshot can become historical and each mutation still rechecks
current admission. The managed-agent guide connects existing preflight,
selected-profile admission and reviewed-tree staging without new agent authority.

Before stacking: 16 focused + 69 interactions, 65.1s. After rebasing on #75:
**INTEGRATION PASS**, 16 individually focused cases/static checks plus 126
preflight/Bridge/reconciliation/historical migration/session/profile-lineage/Web
interactions, 70.4s. Covers DELETE/WAL read behavior, exactly one connection,
concurrent generation/runtime change, stale/mixed identity, real migrated schema,
o runtime/secret dispatch, edits/validation, auth/no-store/escaping. An initial
test looked up validation by workspace rather than its own primary key; corrected
without changing production behavior. No full qualification or native acceptance.

Only appended decision/session documents conflicted while stacking. Both complete
D0456/D0457 records and both session sections were preserved and SHA-checked
against original source commits; preflight source combined cleanly. Private
`preparation-lineage-doc-composition.json` records that proof. #74→#75→#76 is the
review dependency order; there is still no merge or deployment authority.

Continue with the exact stopped-source-copy queue foundation before adding a
guided mutation: preserve the queued container identity, avoid rebinding it during
acquire, serialize trusted claim preconditions and retain unknown/claimed work.
Do not introduce nonce replay that becomes unsafe after ordinary history pruning;
review durable attribution before a new guided copy API. Reuse the existing
source executor and admission, not a general task scheduler. Group a settled
future release candidate after these bounded source changes.

### Stopped source-copy queue binding foundation published

[Draft #77](https://github.com/kcook98765/kodi-lab/pull/77), head
`9ebbb28d4358682df5d430eaab74d95c2b6f0691`, follows #76. New queues retain
the stopped container observed before reservation in the existing field; create
now persists it and repository acquire cannot overwrite a positive pin. Trusted
preconditions execute inside BEGIN IMMEDIATE before operation/lease creation,
with current runtime/type/attachment rechecked. No public endpoint, nonce/replay
guarantee, new table/tool/grant, schema migration or general scheduler.

Acquire verifies unchanged queued intent and exact ownership of both unclaimed
reservations. Positive profile/runtime/attachment drift can fail only that queued
operation and atomically release its own reservations, without copy. Running,
claimed, malformed or foreign work stays fenced. Unknown inventory preserves the
existing behavior. Before-commit cancellation rolls back; after-commit cancellation
keeps the authoritative failed receipt. Legacy nullable queued records preserve
claim-time binding; no retrospective queue proof is invented.

**INTEGRATION PASS** at `9ebbb28`: 12 individually focused cases, Ruff/mypy across
three files, then 251 source/broker/session/replacement/admission/lineage/full-
migration/foundation interactions in 110.2s. The initial nine-case baseline had
5 failed checks and 4 passing compatibility/ownership cases; final cases add
commit cancellation and actual migrated-schema proof. The real-head test copies
fixture data only into the migrated schema and verifies graph head, integrity/FKs.
An initial version-name assertion was corrected to the actual graph head. The
existing create mapper's omitted expected-container field was fixed rather than
weakening the persistence assertion. No native acceptance or live source copy.

Next concrete increment: one small durable guided-copy admission ledger, tied to
retained session generation and immutable original Source-operation UUID. Preserve
nonce scope even when ordinary operation detail is pruned; report missing detail
honestly and never recreate/redispatch because it is absent. Use a trusted supplied
child operation UUID and same-transaction on-claim callback to couple admission
with the existing queue/reservations. GET remains stored observation; an explicit
POST may submit the same known-queued original child using the existing executor,
whose atomic acquire permits one initial dispatch. Running/unknown/failed work
must not be reset. Require current bound stopped session and an already attached
exact source, without copying grants or auto-enabling/starting Kodi. The private
`next-guided-source-copy-review.md` records the detailed review and tests.

No new full gate yet: qualify the next settled composition after durable copy
admission/UI and directly affected integrations. `557dcdd` remains a failed pin;
`cbc8894` remains the last successful release. No merges, deployment, grant changes,
resource disposal or operational migrations occurred.

### Durable selected-source admission published

[Draft #78](https://github.com/kcook98765/kodi-lab/pull/78), head
`6b87f12`, follows #77. Migration 0085 adds one empty durable request table,
referencing retained session generation and snapshotting exact target/runtime/
workspace/source/container. Server-namespaced child IDs remain historical values
independent of ordinary Source detail pruning. The existing queue's trusted
supplied-ID/on-claim extensions atomically commit receipt, operation and both
reservations. Replay only reads the original scope; closed session or pruned
detail never recreates an operation. Capacity refuses without automatic history
pruning at 1,024 total / 32 per generation. No endpoint or executor submission.

**INTEGRATION PASS**, 144.0s: 153 focused cases (18 admission, 2 direct migration,
112 full history, 3 OSTree schema, 18 foundation), then 135 interacting Source/
worker/broker/session/replacement/admission/lineage/Web cases; Ruff and strict
mypy across nine files. Covers one-winner concurrency, baseline producer,
before/after-commit interruption, exact replay after detail pruning or closure,
stored-only observation, scope drift, partial restores, populated0084→0085
transition and lossy-downgrade refusal. Exact head/table expectations include
0085; their initial unextended expected-table assertion was corrected, with
all existing schema/history checks retained. No runtime/native acceptance.

Preservation was reverified at 10:34:48 UTC: all 24 original worktree HEAD/status
pairs, stash, main e198592, 210 container ID/name/image/state tuples and preserved
log branch3dddbc2 unchanged. No merge, deployment, grant/resource change or
production migration. Private `preservation-after-source-copy-foundation.json`
and `session-source-copy-foundation-integration.log` retain the evidence.

Next isolated worktree: guided Source copy Web flow plus shared selected-source
admission at queue, worker and broker boundaries. The older attached-Source path
lacks the selected-session restriction already applied to workspace deployment;
add it before presenting the guided action. Stored receipt pages must not submit
work. Explicit continuation can only submit the same still-queued original child,
rechecking current exact selected scope; no running/failed/unknown reset. Require
existing attachment, no auto enable/start/trust/grants, and clearly distinguish
registered baseline from private workspace edits. Private review:
`guided-source-copy-web-review.md`. Then freeze a settled combined candidate for
one new full gate. Failed557dcdd and successfulcbc8894 remain distinct pins.

### Guided source-copy Web workflow published; settled composition next

[Draft #79](https://github.com/kcook98765/kodi-lab/pull/79), `6aaa097`, follows
#78. The preparation checklist explicitly copies only the already attached
registered-source baseline into the selected stopped disposable profile. Caller
inputs are request UUID and exact generation; all resource scope is derived.
Private workspace edits remain separate. Saved per-session receipt links recover
lost replies. GET is stored-only, while explicit continuation can submit only the
same queued child with exact unclaimed typed reservations. Running, failed,
uncertain, missing/pruned or foreign work is never reset/recreated. Unacknowledged
submission preserves the original work; existing atomic worker admission allows
one initial legal copy across duplicate submissions.

Shared selected-source policy now covers queue/worker writer claims and independent
broker resolution, including exact retained generation for guided children. Same-
scope partial successor restores refuse. Selected-scope uncertainty leaves fences;
existing positive unclaimed profile-drift refusal remains atomic/exact. Current
worker/broker state needs0085 with no missing-table fallback. Inactive legacy
behavior stays on the current schema. No new table/tool/grant, automatic source
attachment, native enable/start, Bridge trust or general task framework. Existing
Web auth/Origin/CSRF/closed inputs/no-store and maintenance remain. Core preparation
facts still use one snapshot; bounded copy receipts are read separately for only
the displayed generation. D0460 and user/agent documentation explain the scope.

**INTEGRATION PASS**159.7s:29 focused (17 application workflow,9 Web,3 materially
changed preparation), then293 Source/broker/session/replacement/admission/lineage/
Web/full-migration/foundation interactions; Ruff/mypy across8files. Initial Web
missing-attachment assertion expected422; corrected to existing409/unsupported,
retaining no-attachment/no-operation assertions. Two additional proof cases were
then added without product-source changes: final19-case workflow module FOCUSED
PASS7.3s covers simultaneous explicit continuation and a lost submit acknowledgement
after completed copy, with one real application producer dispatch through fake
transport. Later cases are focused-only until a new frozen full gate. No native
Kodi/live Docker/platform acceptance. No release result for this source.

The code stack is now settled for one new combined full qualification, including
#74's historical fixture correction and #75–#79. Freeze a separate worktree,
retain exact commit/tree/all tracked entries/changed-file SHA manifest and current
ledger snapshot, use standard auto tmpfs plus separate snapshot scratch, and run
all unchanged release stages without competing tests. Keep557dcdd failed and
CBC/Phase1 successful pins distinct. Do not merge/deploy or migrate running Lab.

### Guided session composition frozen for full qualification

Frozen/pushed branch `qualification/54-guided-session-20261025`, worktree
`54-guided-session-qualification-20261025`, commit
`c788a3166002ea171074d08f8363a827fb1e3da0`, tree
`4211e3d7bda1b32b38e2044111e45d19959248a2`. Source is draft#79 head
`6aaa097d62a235a291274ce04bcc41c6ad5670c0`, plus the exact two durable report
files from ledger snapshot `96e8c5d`. The manifest retains all1,815 tracked entries,
61 changed-file SHA256 values relative to last successfulCBC, and no deletions.
It includes failed557's code plus separately corrected historical fixture and
#75–#79; no prior failed/successful qualification is transferred to this pin.

One unchanged full `scripts/check` began at10:47 UTC using standard harness
auto-selected pytest scratch and separate snapshot scratch. All stages, reserves,
2,400s pytest watchdog, gated native cases and security/schema checks remain.
No competing tests or native/DinD/acquisition acceptance are running. The private
`guided-session-release-result.json`, source manifest, full log and result-folder
stage/report files retain the exact attempt. Source remains frozen. Report the
actual exit/stages/cleanup and reverify manifest before assigning RELEASE PASS.
No result is available at this checkpoint; do not infer release/native acceptance.


### Independent next draft while the frozen full gate runs

The existing synchronous Web-admin copied-Source enablement path lacks selected-
session admission. A narrow draft exists in isolated local branch/worktree
`feat/51-source-enablement-session-scope-20261025` /
`51-source-enablement-session-scope-20261025`, based on#79 `6aaa097`. It checks
metadata scope in the existing writer claim, retains selected generation/CID in
its synchronous in-process handle, and rechecks positive observed identity and
original selection before enabled-state/mutation markers. All existing native
identity/tree/baseline/dependency and post-dispatch reconciliation checks remain.
No new endpoint/migration/tool/grant, auto enablement or durable receipt promise.
The shared helper's None CID is metadata-only; guided copy dispatch still needs
positive CID. This is outside frozen `c788a31`.

Five files are uncommitted, including D0461/user documentation and a new regression
module. **No validation lane has run**, no PR is published, and no claim of working
or release-qualified behavior is made. Ruff/type/focused/integration checks are
intentionally deferred until c788 completes, avoiding competing tests. Private
`source-enablement-session-scope-review.md` and timestamped SHA draft manifest
record intent/current bytes. Continue by validating/fixing this independent slice
after verifying the full result. Do not alter or prematurely qualify c788.

A separate read-only review identified analogous copied-Source smoke queue/worker
scope as a later foundation to assess. It has queued/running expected-container
history and native invocation markers; preserve all post-dispatch ambiguity and
baseline checks, avoid adding a general scheduler or blindly reusing a synchronous
handle design for persisted queues. No smoke implementation has been started.


### Current user walkthrough corrected independently

[Draft #80](https://github.com/kcook98765/kodi-lab/pull/80), `945b3af`, follows
#79 with Markdown-only corrections. The everyday walkthrough connects selected
workspace/version, stopped profile, explicit source baseline, saved request
recovery, independent native/Bridge/enablement, in-place iterations and reviewed
Fresh/Switch. It removes contradictory phase-era statements that still claimed
replacement/retirement composition was unimplemented, while retaining the detailed
admission/evidence/unknown-dispatch/history safeguards. README labels it as the
composed feature-branch flow, requiring matched reviewed deployment, not current
installed behavior or platform acceptance. Native crash bodies remain uncollected;
metadata/excerpts may be incomplete and do not establish cause.

Documentation-only static review against6aaa097 verified template labels/routes,
local guide/anchor and removed stale claims; `git diff --check` passed. Private
`session-walkthrough-static-review.json` retains hashes. No tests added/rerun or
validation-lane/native result claimed. #80 is outside frozen c788 and inherits no
release result. It changes no code/schema/template/grant/resource and is independent
of the pending unqualified source-enablement scope draft. Review after#79;
its top/middle document edits do not overlap that draft's appended D0461 section.
The c788 full suite remains running; no competing tests were started.


Live PR recheck during the frozen gate confirmed#55–#60 original heads unchanged,
open and base-mergeable, plus recorded draft#72–#80 heads unchanged/base-mergeable.
Main remains e198592. This is no combined/native/human approval. Private current
PR JSON and midpoint preservation report retain the evidence. Source-smoke and
inner Bridge lifecycle scope review should establish a reachable/meaningful
contract before adding redundant coordination or another migration. The current
new enablement draft remains uncommitted/unqualified and outside c788.


Enablement draft update while c788 still runs:26 planned cases now include six
direct preflight/mutation-marker checks for expired, foreign and wrong-kind typed
authority. Writer markers require/renew the existing original operation/token/kind
lease using its existing duration and repository expiry guard before scope checks.
This follows direct source inspection; regression/qualification is still pending.
No new schema/endpoint/grant/policy duration. Five draft files remain uncommitted;
private `source-enablement-session-scope-unqualified-draft-v3.json` records current
SHA values separately from the earlier20-case snapshot. Source/helper/test enum
and synthetic receipt UUID conventions were aligned to current definitions before
any validation claim. Do not infer working behavior from this draft.


### Guided session combined qualification completed and independently verified

Frozen `c788a3166002ea171074d08f8363a827fb1e3da0` / tree
`4211e3d7bda1b32b38e2044111e45d19959248a2` achieved **RELEASE PASS**:
5,703 passed,38 gated skips (5,741 collected),34m09s full gate /2049.67s wrapper.
Pytest's own summary is1952.43s; wrapper pytest stage1995s. All eight stages0,
including strict mypy750files, six contract guards, package build, Alembic history
and package/config import. Three existing warnings are recorded in the full log;
no tests/skips/assertions or reserve/watchdog checks were removed.

The independent verifier checked exact HEAD/tree, clean tracked/untracked status,
all1,815 tracked entries and61 changed SHA values, result/stage exit status,
5,741 report outcomes and absence of all three exact owned scratch roots. Peak
RSS3,199,300KiB, minimum sampled pytest free space5,985,755,136B. Standard tmpfs
plus separate snapshot scratch remained in use. Cost/source/report evidence:
`guided-session-release-verified.json`, `guided-session-release-cost-summary.txt`,
`guided-session-qualification-source-manifest.json` and full private result folder.
An auxiliary cost-summary import guessed an absent helper; it was corrected by
using the existing module CLI. This did not affect the successful gate or verifier.

At11:21:47UTC, after-gate preservation again matched all24 original worktree
HEAD/status pairs, original stash,210 container ID/name/image/state tuples,
main e198592 and preserved log branch3dddbc2. The gate used only temporary/fake
transports; no native acceptance, live resource action or operational migration.
No merge, deployment, policy/grant change or release publication occurred.

The qualified composition includes#74's historical0078 fixture correction and
#75–#79, including all19 final copy-workflow race/ack cases. It is not a standalone
full run on every PR head. #80's later Markdown and the uncommitted enablement
scope/lease draft are outside this pin and inherit no qualification. The old
failed557 result, priorCBC and Phase1 pins remain separate immutable records.

Continue rather than stopping at review-ready source: the26-case enablement scope
module is now running its first focused lane, followed by appropriate existing
native-source/session/lease/migration interactions after honest fixes. No other
full run is scheduled merely for one draft; qualify another settled appropriately
scoped composition when required. Smoke/Bridge follow-on remains a minimality/
reachability review, with no new migration or generalized coordinator committed.


### Synchronous source enablement scope/ownership published after c788

[Draft #81](https://github.com/kcook98765/kodi-lab/pull/81), `87c36a4`, follows
#79, independently of Markdown#80. Metadata selected source/target/runtime/pending
admission runs in the writer claim. The synchronous handle captures original
selected generation/CID; positive observed identity and the same generation are
rechecked after runtime observation and in preflight/mutation marker transactions.
Both markers require/renew the original unexpired typed kind/op/token lease using
its existing duration. Scope failure before dispatch records precise failed
preflight; post-dispatch ambiguity retains existing reconciliation. Foreign
ownership is not cleared. Existing validation/baseline/tree/dependency/native
identity/bytecode/service restrictions and readback remain. No new durable request
promise, migration, endpoint/tool/grant, auto enablement or general task framework.
D0461 and session documentation explain metadata-only versus native proof.

**INTEGRATION PASS**,178.2s:27 focused cases, Ruff/strict mypy3files, then363
native-source/smoke/copy/queue/session/replacement/admission/Web/broker/direct-
migration/full-history interactions. A real current-head database received data
only from fixtures, then ran the actual producer with positive fake native
transport, version/FK/integrity/lease checks. Initial26 tests passed but import/
context-manager lint findings were corrected; the27th case adds real-schema proof.
No assertions were weakened. No native or platform acceptance.

Identical final test file SHA
`fad19b214c8ddebc162238cd5b80525e57c358d7439e0010f7f51705a2a96672` on untouched
`6aaa097` produced23 behavior failures and4 passing compatibility/schema cases,
without setup/import errors; all27 pass with the fix. The detached baseline
worktree retains its original tracked source and only the added test file.
Private regression comparison JSON/logs retain proof. These are synthetic boundary
cases, not reproduced live incidents or a claim of reachable cross-session exploit.

#81 is outside release-qualifiedc788 and has no RELEASE PASS. #72–#79 unchanged
heads were verified ancestors of c788; their PR descriptions now state the exact
combined result and they are ready for review. This is not human approval, merge
or deployment. #80 remains independently static-reviewed/outside the pin.

Next minimal source-smoke foundation should first reuse the existing nullable
expected-container field: new selected queues pin desired CID, create must persist
it, acquire must not overwrite it, actual observed CID must match before native
invocation. Shared scope/exact ownership at queue/acquire/native marker and a
captured acquire-time selection can serve the existing generic queue without a
new generation table/column or versioned request promise. Legacy nullable queues
must keep honest observed-binding compatibility, not invented original queue
proof. Existing typed reservation already prevents legitimate Fresh/Switch while
queued/running; preserve post-invocation ambiguity and all native/baseline checks.
Only add persistence if a separately established durable-generation invariant
requires it. No generalized manager/scheduler. Continue independent code work.


### Generic queued source smoke scope published without a new migration

[Draft #82](https://github.com/kcook98765/kodi-lab/pull/82), initial `8973454`,
final `683c2c5`, follows#81. Existing desired-CID storage now retains new selected
queues, acquire preserves positive pins and exact unchanged/unclaimed typed
intent, and actual observed binding can fill legacy NULL or confirm the same pin.
Acquired selection is captured in process and rechecked before native invocation.
Original historical target must equal the leased target. Live observation/marker/
finish check exact kind/op/token/expiry ownership before writes or cleanup; their
heartbeats keep the original90-second operation expiry. Foreign/malformed/unknown
work stays fenced. Finish does not invent current selected generation or erase
post-invocation ambiguity. Existing native/baseline/tree/dependency checks remain.
No new migration, model vocabulary, durable nonce/generation promise, endpoint,
MCP tool/grant, generic queue/scheduler or automatic retry/native action. D0462
and session documentation state desired versus observed versus historical scope.

Initial **INTEGRATION PASS**167.9s:31 focused plus382 subsystem/Web/broker/full-
history interactions, Ruff/mypy3files. Real migrated-head data-only fixture and
producer/FK/integrity/version, fixed bound, nullable active/inactive paths,
commit-interrupted dispatch marker and active timeout reconciliation included.
Its initial type lane found a missing Optional-result assertion in test code;
it was made explicit, reconciliation uses its exact exception type, and helper
seed semantics were clarified. No product guard/assertion was weakened.

A two-line original-target follow-up passed32focused+200 interactions in99.1s.
The acquire defect branch was then added to its parameter list (rather than left
unselected), and final33cases passed individually with Ruff/mypy19.0s; product
source unchanged between that integration and final test addition. Identical
final test SHA `66c17d2f5cee2f2769abc2884260d8b90bbe2eb7b66660ed7d5fd7d26cb071a5`
on untouched87c36a4 produced25behavior failures/8compatibility passes, then33passes
after the fix, no setup/import errors. Earlier31case test bytes/log/SHA are kept
separately (23fail/8pass baseline). Synthetic boundaries are not live incidents.

#82 remains outside successfulc788 and has no RELEASE PASS. Its shared repository
and native dispatch/ownership changes plus#81 now form a settled security-boundary
candidate; compose the independently static-reviewed#80 guide without dropping
D0461/D0462, take current report snapshot, freeze separate worktree/manifest and
run one complete gate without competing tests. This is grouped qualification of
shared authority changes, not a full run per PR. No further code churn is planned
in the frozen candidate. Existing scoped/runtime/native/Windows operational
acceptance and approval decisions remain distinct. The Bridge inner-scope review
must respect existing one-profile/broker identity/START lease/trust generation
invariants; no redundant new manager/migration has been introduced.


### Settled single-session authority composition frozen for full qualification

Frozen/pushed `qualification/54-single-session-20261025`, worktree
`54-single-session-qualification-20261025`, commit
`ba215e6d62dac996cfdf12e9df102bc1b16b6a99`, tree
`d6b76696e2eb7dbca0d115493bd0b137e4ae4862`. Source82 final683c2c5 includes81;
independent guide80 was composed as5a238ac without conflict. README matches80;
the complete appended selected-enablement/smoke session sections from81/82 were
SHA-verified unchanged. Current ledger/review snapshot5420c39 is included. Manifest
records all1,817tracked entries,11changed-file SHA256 values versus successfulc788,
no deletions, source/guide/report heads and composition proof.

Provenance preflight correctly noted that c788 itself is a sibling report-snapshot
commit, not an ancestor of source81/82. The check now verifies the qualified
functional head6aaa097 is an ancestor and that c788 differs from that head only
in the two report files. The exact comparison still uses the actual qualified
c788 tree. No source was rewritten to manufacture ancestry or qualification.
This verification happened before the gate; no failed full attempt was created.

One unchanged full gate started11:55UTC using standard auto tmpfs and separate
snapshot scratch, all stages/security/schema/native gates, unchanged reserves and
2,400s watchdog. No competing tests, native/DinD/acquisition acceptance or source
churn. Private `single-session-release-result.json`, source manifest/full log/
result folder retain the attempt. **No result yet**; c788 remains latestverified
RELEASE PASS. Follow completed exit/stages/cleanup and independent source proof
before assigning another result. Guide/81/82 do not inherit c788.

The separate inner-Bridge scope review remains bounded: current one-provisioned-
profile admission, exact broker container identity, owned START action and exact
prepared trust generation already exclude ordinary foreign-live-profile paths.
Do not add redundant state/migration merely for an impossible normal transition.
Native prompt and supported exact-version/platform acceptance still require
separate authorization/evidence. No new Bridge implementation or acceptance is
claimed. All grouped operational/security-design decisions remain unchanged.
