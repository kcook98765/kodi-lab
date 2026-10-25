# Kodi Lab development program #54

Coordination: <https://github.com/kcook98765/kodi-lab/issues/54>.
This ledger records source qualification, not deployment or native acceptance.

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
