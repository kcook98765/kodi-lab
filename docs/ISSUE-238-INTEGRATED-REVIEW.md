# Issue #238 integrated review and stabilization

Review baseline, 2026-11-06:

- Current main: `71e6a8a946858e4b9738bc21d1294baca594b90c` (`0002_managed_setup`).
- Integrated draft head: `f7601a158bbc20b89bff134f2e36aac28d38286c` (#242).
- Corrections are isolated above #242 on `fix/238-integrated-stabilization`.
  No existing draft branch is rewritten. Nothing is merged or deployed.

Scope is the cumulative main-to-#242 implementation: version admission, lifecycle,
clean Reset, Web adapter, MCP authorization, migrations and development continuity.
No scheduler, Docker controller, permission, automatic switching or destructive
recovery mechanism is added.

## Ranked findings

| Severity | Finding | Disposition |
| --- | --- | --- |
| Medium, correctness | New Web actions called `crypto.randomUUID()` unconditionally. Supported private-LAN HTTP can omit it, throwing before any Start/Stop/Reset request. | Corrected with the existing cryptographic `getRandomValues` UUIDv4 pattern; reproducing DOM test fails before and passes after. No weak random source or relaxed transport policy. |
| Medium, concurrency | Development selection/policy admission checked child leases but missed durable version/Reset parents between children, permitting selection changes while the original operation was pending. | Include active Start/Stop/Reset parents in the existing global development-session busy check. Tests reproduce both Start and Reset gaps, preserve selection/policy during the parent, and prove normal selection resumes after completion. No new lock, scheduler, permission or recovery path. |
| Medium, compatibility | Web cached registration revalidated the recipe display name even for an existing authorized empty canonical target; a harmless rename made Web Start/empty Reset fail while MCP accepted the same target. | Only create the Target when absent. Preserve an existing canonical ID/type/version and its name; exact runtime/image/grant checks remain. Start/Reset rename regressions and wrong-version denial pass. |
| Medium, workflow | A stale Reset refused before the Web intent was accepted left the browser permanently attached to the obsolete snapshot. Resume repeated the same impossible request and all new actions stayed disabled. | Corrected only for an explicit HTTP 409 `stale_browser` plus a receipt lookup returning 404. This permits a new user action/confirmation, never automatic dispatch. A lost response or a missing receipt alone retains the original intent. |
| Low, recovery UI | Failed empty-slot Start linked to `/instances/<absent>/history`, which returns 404. | Corrected to authenticated global history when no managed registration exists; exact instance history remains for a present profile. |
| Low, validation | Cumulative Ruff found one overlong broker error literal inherited from the stack. | Wrapped the identical literal; no behavior or error text changed. |
| Medium, known workflow boundary | Reset of the selected development profile closes Current Kodi. Existing MCP selection requires an already selected same workspace; lifecycle/reset grants cannot restore it. Fresh Bridge/source baseline/enablement also require existing administrator controls. | Documented and integration-tested. The guide now preselects the requested version, states the stopped-selection prerequisite and links the existing administrator-use release before agent handoff. No trust, source authority or deployment proof is copied. Automatic reattachment remains a follow-up. |
| Qualification gate | Fixture results do not qualify native Kodi, Windows/WSL2, graphical viewing or installed upgrade. Claw root is 96% used. | Separate capacity-aware [native acceptance plan](ISSUE-238-NATIVE-ACCEPTANCE.md); no cold acquisition, native container or installed-state mutation in this campaign. |

No critical/high security defect was confirmed in this bounded source/fixture
review. This is not a claim that all possible races or platform behavior are
proved. Unknown removal can intentionally stay fenced indefinitely without its
exact original retirement proof; generic reconciliation/timeout is not a force
unlock. Receipt quotas also remain fail-closed and require review when exhausted.

## Integrated safety assessment

| Area | Assessment |
| --- | --- |
| Canonical identity | The four current reviewed recipes resolve canonical targets internally. Profile uniqueness is by Kodi major version across targets, pins, inventory and uncertain provision reservations. Canonical coordinators refuse legacy/noncanonical same-version profiles; no adoption/deletion/repin occurs. |
| Cached registration | Start/Reset use an approved eligible exact cached image. The Web role alone may register a missing reviewed canonical Target; MCP still requires a pre-existing exact target grant/policy. Reusing a stopped registration preserves its pin rather than following a newer recipe automatically. |
| One running Kodi | Existing broker admission combines inventory with durable start reservations under `BEGIN IMMEDIATE`. Effective capacity is one even if the historical stored policy says four. Older stored configuration is not silently rewritten. Unknown reservations have no timeout shortcut. |
| Parent/child receipts | Start/Stop and Reset have immutable principal/request/version binding and deterministic child identities. Completed requests remain historical and do not repeat Start after Stop. Unknown dispatch never triggers another create/removal. Child history is protected from ordinary pruning. |
| Reset and data | The exact stopped container/image/pin must be retired through the existing broker/lifecycle path; independently bound successful receipt precedes replacement. No fixture restoration. Source/workspace/Git/grants/selected evidence remain independent. Bridge, deployment and enabled-state assumptions, readiness, screenshots and disposed development selection are invalidated. |
| Shared concurrency | Version services use the existing cross-process target lock. Web/legacy/deployment operations share typed leases, SQLite admission and clean-Reset mutation/view/input triggers. New authenticated Web/MCP dispatch integration exercises MCP create versus Web Reset and stale replay after completion. |
| Viewing | Standard noVNC remains unchanged: exact binding/epoch, authenticated one-use ticket, view-only default, explicit control and cleanup ownership. Reset refuses pending/active viewer/input ownership; old bindings cannot select a replacement. |
| Authority | MCP checks current credential/client, client permissions, canonical target type/version, exact grant and target policy at dispatch. Reset requires both separate opt-ins. Source/workspace ownership and selection remain independent. No external Kodi, Docker, shell, arbitrary image/path authority is added. |
| Maintainability | The adapter's private coordinator helpers and legacy `client_id` owner column are coupling to preserve carefully, not reasons for a broad refactor here. The Web ledger has a distinct purpose: immutable human snapshot/consent, while shared parents retain execution/recovery ownership. Narrow tests cover both boundaries. |

### Reserved Web receipt owner

The UUIDv5 constant is a server-side Web administrator namespace, not an MCP
client, token, preset or grant. Normal client creation generates UUIDv4 IDs and
has no caller-supplied ID. MCP cannot select its principal through a tool argument.
Even a synthetic valid credential/client/grant colliding with this namespace is
explicitly denied by `recheck_managed_versions`. The integration test provisions
that collision in an isolated database and confirms Status/Start/Reset denial.

Web callbacks validate the actual current administrator session and canonical
target inside consequential transactions; the readonly resolver avoids nested
last-use writers. Origin/CSRF remains the existing Web mutation gate. A bearer
MCP credential cannot authenticate the Web Reset route; a Web session cannot
supply MCP authority. Owner-to-`web` activity mapping grants no authority by
itself. Existing issued presets/grants remain unchanged.

## Development after Reset

The complete development loop is **not fully autonomous after disposal of its
selected profile**, even with all previously issued source/workspace grants.
The new lifecycle tools intentionally do not grant selection, copying, add-on
enablement or fresh Bridge establishment.

1. Persist and call `managed_reset(version, request_id, restart=false)` with both
   lifecycle/reset opt-ins and the exact canonical target grant. Inspect the
   original receipt: retirement/replacement must be confirmed, native readiness
   separate. If `restart=true` was used, Stop safely before selection/source copy.
2. The administrator opens **Kodi → Set up for add-on testing → Choose this Kodi
   version and workspace**. Version selection must be enabled through the existing
   retained-development policy and all managed Kodi must be stopped. Select the
   existing workspace and exact replacement through the usual identity/generation
   preview. Never prepare a duplicate single-session profile for an occupied slot.
3. Open `/development-session/preparation`. Keep or explicitly attach the same
   registered source if needed. Copy the registered-source baseline into stopped
   Kodi using its saved request. This prepares new profile files and provenance,
   not private workspace edits, execution or enablement.
4. Start natively and use the existing **Reviewed Bridge** administration on the
   selected instance to install/resume fresh reviewed trust. Its controlled restart
   and identity verification must complete. Use the guarded source **Enable**
   control for the intended add-on; services retain their separate start consent.
   Do not transplant old credentials, pairing or deployment baselines.
5. Finish the administrator handoff before the agent begins. Close viewers and
   finish control cleanup. In the selected instance's **Resource and keep-awake
   settings**, set administrator keep-awake to zero. The existing
   `POST /api/managed-resources/{target}/keep-awake` with `{minutes: 0}` ends only
   Web's own hold/recent use, without stopping Kodi or releasing an agent's hold.
   Alternatively, Stop in Web while the agent is paused, then let the agent Start.
   Admin native/Bridge preparation can otherwise leave a Web hold/recent use that
   correctly blocks a different client's later Stop/Reset. The new integration
   test proves the supported handoff preserves the agent's independent hold.
6. The agent retains its exact source grant/workspace binding and target policies.
   Run `workspace_status`, edit/import if needed, `workspace_validate`,
   `workspace_deploy_preflight`, a fresh authorized `workspace_deploy`, then the
   supported `workspace_launch`/execution and inspection tools. Match current
   container, workspace generation and file digests. A historical deployment
   receipt never proves files or enabled state in the replacement.

Why the administrator is needed: clean Reset closes only the disposed Current
Kodi selection. `_selection_authorize` refuses with
`retained_workspace_requires_administrator` when no current selection exists,
even though source/target grants and workspace ownership survived. Existing
`managed_select`/`managed_selection_preview` require `managed.lifecycle` plus
`workspace.inspect`, exact scopes, an already selected same workspace and stopped
profiles. They cannot bootstrap a cleared Current Kodi selection. If Reset was
for a different, nonselected version, an existing same-workspace selection can
still authorize selection of that version under those original rules. Fresh
Bridge installation, registered-source copy and first add-on enablement remain
administrator preparation; they are not new powers of `managed.versions` or
`managed.reset`.

The integration fixture proves grant/workspace/file preservation, closes the old
selection, marks its canonical deployment baseline stale while retaining history,
observes MCP's exact refusal, and then proves existing administrator reselection
restores selection access. Existing Reset/source/Bridge tests cover invalidation
and runtime replacement. Automatic reattachment would need an explicitly reviewed
source/workspace/selection and trust decision, not a credential-copy shortcut.

## Migrations and compatibility

`0002 → 0003_managed_versions → 0004_clean_reset → 0005_managed_web` is a single
linear chain of frozen DDL, with no edits to the initial schema. Each adds only
receipts/fences and changes no existing grants, policies, pins or profiles. New
sequence tests preserve predecessor policy/preset/client/grant rows, including a
stored limit of four, across all three upgrades; validate integrity/FKs; exercise
empty downgrade/re-upgrade; and refuse lossy downgrade with historical receipts.
The direct predecessor suites verify immutable bindings and each populated
revision's downgrade refusal. No new migration is needed for these corrections.

Deploy the matching application/broker/repository/schema together through the
supported upgrade path; an old broker must not be mixed with new one-running
policy. A rollback with new receipts requires the supported backup/recovery
procedure, not schema stamping or deletion of audit rows. Pre-existing duplicate,
legacy and noncanonical profiles need explicit administrator review. Their
conversion/retirement is not automatically authorized by upgrade or this review.

Windows/WSL2 source assumptions remain Linux service processes, Unix socket/peer
UID authority, POSIX locks and private Linux data paths. Version APIs introduce
no Windows shell, Docker endpoint or host path input. A Windows-style checkout
contract passes; actual Desktop/WSL socket, storage, ingress and graphics remain
native qualification. The private-LAN UUID defect is corrected without changing
any HTTP/HTTPS authentication policy.

## PR-by-PR structural readiness

| PR | Current base → exact head | Assessment |
| --- | --- | --- |
| #239 | `main` (`71e6a8a`) → `ccea084820c6241777d8ee70870f9fdc2eb3dc1c` | Linear, GitHub mergeable; shared admission remains the sole broker gate |
| #240 | #239 branch → `ba74794886c1ddaac11397467f662b08d332a684` | Linear, GitHub mergeable; migration 0003 follows current main's 0002 |
| #241 | #240 branch → `94e99b1b17ed9f139911f47392fcebf5f3e8667c` | Linear, GitHub mergeable; 0004 preserves exact retirement/invalidation fences |
| #242 | #241 branch → `f7601a158bbc20b89bff134f2e36aac28d38286c` | Linear, GitHub mergeable; 0005 and administrator adapter need the small corrections in this follow-on draft |

A local `merge-tree`/unreferenced-object rehearsal of sequential merge commits
produced exactly each intended head tree, with no conflicts or omitted files.
The final #242 tree was `82d3d545544c2d513a9a6e01a3efdb1bb8888b74`.
No branch or PR was actually merged. The repository permits merge commits.

After qualification and owner approval, preserve ancestry with sequential merge
commits: #239 into main, retarget #240 to main after its parent lands, then do the
same for #241, #242 and this correction draft. Recheck head/base/diff at each step.
Do not accidentally merge a child only into its old feature base. Squash/rebase
rewrites are not necessary and would require another ancestry/diff assessment.
All existing PRs remain drafts and unchanged by this campaign.

## Focused integrated results

`check-integration` ran three focused files individually (17 Web/MCP integration,
2 full-stack migration and 2 DOM/semantic cases), then 251 directly affected
interaction cases together: **INTEGRATION PASS — 272 passed, no skips**, 213.7 s.
The final lane includes all corrected source and final tests; each new or materially
changed test component received its individual focused run.

Interaction selection: version lifecycle (35), clean Reset (42), broker version
slots/capacity (16/11), version/Reset MCP (9/7), Web slots (20), noVNC authority (8),
authentication (9), predecessor migrations 0003/0004/0005 (11/13/2), source baseline
runtime replacement (16), MCP retained selection (8), development sessions (14), retained development (26),
Windows checkout contract (4).
The final integrated review also runs Ruff/strict mypy on the cumulative changed
production Python files and new tests (**32 files**, Ruff and strict mypy pass),
plus Node syntax/DOM checks.

No full eight-stage release suite, image acquisition, native Kodi, actual desktop
browser/graphics or installed-state acceptance ran. The exact-tree release gate
and the native plan are still required. The recommendation is **ready for final
qualification with these corrections included**, not merge/deploy approval or a
claim of an unattended post-Reset development loop. Automatic version switching
and fully automatic development reattachment remain separate follow-ups.
