# Program #54 Phase 2 integration review

Live GitHub and local refs rechecked at Phase 2 entry on 2026-10-24 Pacific.
`origin/main` and clean primary HEAD remain `e198592b634bc5bc2cf876fd5fbded88b7cd827d`.
All six PRs are open, GitHub reports mergeable, and none has a submitted review.
No merge, deployment or runtime mutation was performed. Existing worktrees,
ignored state, stash and containers were inventoried before implementation;
private snapshots: `/home/kyle/kodi-lab-program-54/phase2-20261024/`.

| PR | Exact reviewed head | Compatibility and dependency |
| --- | --- | --- |
| [#55](https://github.com/kcook98765/kodi-lab/pull/55) | `0c45ff3` | Bridge browser/template only. Shares instance-detail template with #58 in separate regions. No backend grant or readiness change. Hidden-completion fix has focused evidence only. |
| [#56](https://github.com/kcook98765/kodi-lab/pull/56) | `bbd0768` | Nine fixture clones and durable program docs. No assertion/production changes. Shares test guide with #59/#60 in separate sections. |
| [#57](https://github.com/kcook98765/kodi-lab/pull/57) | `98d382a` | Baseline reasons and MCP allowlist. Shares deployment service with #58 in separate predicates. Does not implement complete read-only preflight. |
| [#58](https://github.com/kcook98765/kodi-lab/pull/58) | `4c2f681` | Recovery fences and explicit historical enrollment. Uses existing broker/migration contracts. Does not grant permission to operate on an existing target. |
| [#59](https://github.com/kcook98765/kodi-lab/pull/59) | `113111e` | Independent retained-report CLI and tests. No test selection or runner behavior change. |
| [#60](https://github.com/kcook98765/kodi-lab/pull/60) | `50975c8` | Independent watchdog default. Same effective 2400s bound as real pinned qualification; wrapper stubs supply unit evidence only. |

Recommended review/merge order, **only after separate merge authorization**:
#56, #60, #59, #58, #57, #55. The first three establish documentation and
measurement tools; recovery correctness precedes richer diagnostic/workflow
composition. There is no hard code dependency among these six PRs. All six
already compose by ordinary cherry-picks at `cf28e9f` (Phase 1 qualification
branch); later ledger-only updates in #56 do not alter those application bytes.
No reproduced compatibility blocker was found in the shared hunks. This review
does not replace independent maintainer review or exact-head checks after edits.

**Qualification boundary:** RELEASE PASS belongs to `02c4054`, tree
`1cdb95bff744d012faace9f05417191a42a375cc`: 5234 passed, 38 gated skips,
all stages passed in 29m59s. `2b33e1c` adds the hidden-tab correction with focused
checks; `cf28e9f` also adds #59/#60 with focused checks. Neither later tree has a
new full-gate result. Do not transfer RELEASE PASS to Phase 2 implementations.

## Implementation sequence

1. #45: opt-in reviewed tree staging with per-path private resolution, removal
   preimages, tombstone lineage, atomic exchange, retained history and replay.
   Preserve the existing default add/replace contract and all grants/limits.
2. #46/#49: shared observation-only native/capability facts, exact-target
   deployment prerequisites and safe Web/MCP projection. Preserve capability
   independence and all mutation-time checks.
3. #50: coordinate with preserved `feat/nexus-closed-log-retention-20261023`
   at `3dddbc2` (parent `09d847e`). It bounds four closed internal logs in Nexus
   recipe r3 and Docker output for newly provisioned marked containers; it does
   not expose stopped diagnostics or retain per-operation artifacts. Do not
   modify/publish that separately scoped branch or duplicate its truncation,
   recipe or Docker log policy. New diagnostics must use fixed bounded reads
   with exact ownership/container/runtime/operation evidence and no wake/start.
4. #51: Web-owned durable single-session reservation and shared provision/start
   admission, then minimal resume/stop/fresh/switch workflow over canonical
   exact targets and approved cached runtimes. No generalized manager/scheduler.
   Unknown inventory, unrelated containers, mutation leases and reconciliation
   block; never enroll or evict existing resources implicitly.

Code-only implementation is authorized by the Phase 2 kickoff. Merge,
deployment, security grants, existing-resource deletion/destructive migration,
and native/platform acceptance remain separate decisions. Settings (#52) needs
a separately approved permission design; unattended ZIP installation (#53) has
no demonstrated supported route. Keep #47/#48 deferred and #8/#17 native
evidence gaps open. Group future approval decisions in the durable ledger.

## Published Phase 2 dependency review

The application heads #62–#67 compose with all six Phase 1 heads in the frozen
candidate `f6449a432a74837fdfaf67d4cd23181716775590` (tree
`3192fbccb66e6a23f0ae790efadd70561137a510`). This exact pin achieved RELEASE PASS:
5,354 passed, 38 gated skips, all stages and cleanup, 29m54s. Later application
changes need their own evidence; do not transfer this result to another tree.

| PR | Reviewed head | Base and integration considerations |
| --- | --- | --- |
| [#61](https://github.com/kcook98765/kodi-lab/pull/61) | ongoing ledger | Documentation continuation of #56; later evidence updates do not modify the frozen source. |
| [#62](https://github.com/kcook98765/kodi-lab/pull/62) | `43a332f` | Main-based #45 staging and receipt recovery. Legacy receipt reads retained; new reviewed-tree receipts require this reader during recovery. Includes reproduced post-commit cancellation/readback correction. |
| [#63](https://github.com/kcook98765/kodi-lab/pull/63) | `c4dc028` | Stacked on #57. Read-only durable preflight, existing authorization, exact 27-tool MCP catalog. Live native/Bridge/add-on facts remain unknown. |
| [#64](https://github.com/kcook98765/kodi-lab/pull/64) | `f6fe7dc` | Stacked on #55. Explicit reviewed prepared continuation, displayed trust generation and lifecycle lease. No automatic installation or prompt acceptance. |
| [#65](https://github.com/kcook98765/kodi-lab/pull/65) | `31a01af` | Main-based. Empty migration 0080 and shared broker provision/start admission. Unknown create remains fenced; current absence cannot clear it. |
| [#66](https://github.com/kcook98765/kodi-lab/pull/66) | `2688e04` | Stacked on #65. Reserve/Prepare/Resume/Stop, exact current generation/target/runtime/container checks. No Fresh/Switch/deletion or new MCP authority. |
| [#67](https://github.com/kcook98765/kodi-lab/pull/67) | `8833c30` | Main-based. Exact stopped-container metadata read, fixed 39-operation broker catalog. No retained log bodies/operation association or crash-cause claim. |
| [#68](https://github.com/kcook98765/kodi-lab/pull/68) | `2080f4e` | Later increment stacked on #64. SELECT-only stored capability prerequisites; integration qualification only, outside the release pin. Adjacent Web route additions with #67 need both complete blocks preserved. |
| [#69](https://github.com/kcook98765/kodi-lab/pull/69) | `80605de` (draft) | Later increment over composition `24c1aeb`. Depends on #65/#67 and composes #68. Empty 0081 follows 0080; exact broker catalog becomes 40. Five fixed bounded stopped log artifacts and Web-only private capture retention. Schema-1 saved bodies remain readable. Integration/focused evidence only; keep separate from `f6449a4` RELEASE PASS. |

After the Phase 1 order above, #62, #65, #63, #64 and #67 can be reviewed in
parallel conceptually; #66 follows #65. #61 follows #56 and continues to collect
review evidence. This is a review dependency order, not merge authority.

Shared `web/app.py` routes/imports, #65/#67 broker registry changes and exact
catalog assertions composed without source conflict. Only appended
`docs/DECISIONS.md` records conflicted: D0443 through D0448 were preserved from
the exact feature heads, with hashes retained in the private composition proof.
0080 has one parent/head; no Phase 2 migration branch fork exists. #65's exact
metadata and historical 0076 downgrade assertions explicitly account for the
new empty migration without weakening retained-uncertainty refusal. All shared
security/regression guards remain selected by the full gate.

## Remaining single-session prerequisites

| Increment | Required proof or concrete decision |
| --- | --- |
| Uncertain provision settlement | Independently establish that the original Docker create dispatch has finished and cannot finish late. Neither absent inventory, wall-clock age nor service restart proves this. Retain the reservation and uncertainty until that proof exists. |
| Retain evidence before profile replacement | Fixed bounded approved reads; exact target/runtime/container and operation binding; explicit missing/rotated/truncated states; private size/count/age policy and body redaction/access. Metadata-only #67 does not satisfy retained-log acceptance. Reuse the separately preserved log-rotation branch. |
| Fresh Test / Switch | Explicit reviewed disposal plan and selected retained evidence, no busy/reconciliation or uncertain dispatch, exact old-container cleanup proof before any replacement, new trust/baseline/deployment binding. No existing-resource operation is authorized by this code-only phase. |
| Complete guided preparation | Source/workspace grants and attachment, stopped baseline copy, native readiness, explicit enablement and reviewed Bridge recovery stay separate until safely composed. No agent creation/deletion or installation authority is inferred. |
| #52 settings | Bind an active session to an exact durable successful deployment receipt first. Review ordinary add-on settings allowlist and explicit permission model; never select a latest unrelated operation or infer settings authority from deployment/launch. |
| #53 native ZIP installation | No demonstrated unattended native API in current evidence. Keep assisted installation honest until a supported exact-version provider path is established and separately accepted. |
| Native and platform acceptance | Separate authorized Omega then sequential Nexus/Omega tests, prompt handling and deployment preservation, plus fresh Windows/WSL2 account/host evidence. Source tests cannot provide these results. |

These are grouped next decisions and implementation prerequisites. They do not
prevent independent code-only diagnostics, status projection or documentation.
No general manager, task scheduler, automatic cleanup, grant broadening or
profile migration is proposed.

#68 follows #64. The later overlay `24c1aeb` composes it with the complete
release pin; adjacent route and decision appends were preserved explicitly,
with AST proof that all existing route functions are unchanged. Integration
passed 24 new cases individually/static checks plus 20 affected Web interactions
in 48.9s. This overlay is not RELEASE PASS. The original release worktree remains
clean at `f6449a4`, and all earlier qualifications retain their exact boundaries.
