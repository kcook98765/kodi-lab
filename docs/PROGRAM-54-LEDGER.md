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
wall time; collect exact phase data in the later complete candidate run before
changing those proofs. The release runner and its default timeout are unchanged.
Use the documented explicit timeout override for that qualification, justified
by this baseline; do not rerun unchanged main merely to obtain a pass.

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

Increment status (broader combined qualification remains pending):

| Increment | Isolated branch/worktree | Status |
| --- | --- | --- |
| #54 fixture performance | `program/54-test-performance-20261024`, `/home/kyle/kodi-lab-worktrees/54-test-performance-20261024` | INTEGRATION PASS for matched 269-case lane before/after; FOCUSED PASS for all nine changed modules individually (235 cases) plus the 15 existing template/cleanup proofs, Ruff/mypy, 43.2s. Non-fixture behavior ASTs match main in all nine modules. |
| #44 failed-copy recovery | `fix/44-fence-post-copy-failure-20261024`, `/home/kyle/kodi-lab-worktrees/44-post-copy-failure-20261024` | FOCUSED PASS: 31 application and 5 Web cases individually, Ruff/mypy, 19.5s. Broker interaction qualification next. Retains fence after inventory failure; explicit historical verified-copy enrollment; existing broker full-tree proof; exact history action. |
| #49 Bridge status | `fix/49-bridge-status-20261024`, `/home/kyle/kodi-lab-worktrees/49-bridge-status-20261024` | Commit `a77607f`; [draft PR #55](https://github.com/kcook98765/kodi-lab/pull/55). FOCUSED PASS: new 2-case module plus 6 existing control tests, each individually, Ruff/mypy and JS syntax. Lanes 23.6s and 8.3s; pytest 1.22s and 3.24s. Broader combined qualification pending. |
| #46 baseline diagnosis | `fix/46-managed-baseline-reasons-20261024`, `/home/kyle/kodi-lab-worktrees/46-baseline-reasons-20261024` | Draft: bounded missing/stale/unknown/runtime-mismatch reasons survive MCP; existing admission unchanged; full preflight remains later. |

No merges or running-installation changes authorized. These branches remain
independent of the profiling worktree while its baseline is running.

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

1. Finish dependency/current-main audit and establish focused, integration and
   full-gate baseline once; retain logs and phase/resource reports.
2. Select a measured test-only optimization; run matched focused profiling and
   appropriate broader validation once on settled implementation.
3. Address terminal failed-copy recovery (#44/#17), then exact-tree staging
   (#45), bounded preflight/readiness (#46/#49/#50), and disposable session
   orchestration (#51) in independent or explicitly stacked increments.
4. Audit #52/#53 separately; no new grants or live installation operations.
   Reassess #47/#48 against the one-session workflow and native platform gaps.
