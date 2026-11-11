# Agent-first implementation campaign

Authorization: owner prompt, 2026-11-11; scope #303 and #304-#310.
Claw lab-owned state disposable; unrelated data, repositories and host apps protected.

## Current checkpoint
- Starting main: f3a666ec45ce5e23bebd6b5105799bdf7f459bae. Current integrated main: d878c23bec54243e3588addd18b5a2c447e83025.
- Baseline schema verified: 0008_managed_bridge_preparation. Current main schema: 0010_agent_projects.
- Worktree: /home/kyle/kodi-lab-worktrees/agent-first-20261111.
- Active issue: #305 resource folder authoring; branch feat/305-project-resource-paths; no PR yet.
- #304 source merged: PR #311 https://github.com/kcook98765/kodi-lab/pull/311; head 860aa0221ed446341dabb5ad1d479aca6b4edef9; integration 4a78965be2eead684144f723e7fb9201bbf391bc. Native/integrated acceptance still open under #310.
- #304 final design: independent client managed_lab_role_version=1 opt-in; legacy/external preset and grants unchanged. Typed managed shortcut only for approved disposable managed instances; owner/source, runtime and lifecycle guards retained.
- #304 FOCUSED PASS: 192 distinct tests across role (10), role migration (1), Web admin/CSRF (1), initial schema (32), migration regressions (60), MCP foundation (17), production authorization (22), managed-version wire (10), workspace mutations (34), development inspection (5). Focused Ruff and mypy pass. Latest workspace lane: 49 passed, 18.9s including static checks.
- Self-review: corrected initial preset-only design to preserve separately authorized external access. Migration keeps role revision/audit history, refuses lossy downgrade. Corrected baseline stale initial-schema graph/future-head expectations.
- Additional review correction: workspace allocation honors role without base edit permission; creation/edit publication rechecks current credential, role, owner and source in the mutation transaction. Failed edits roll back bytes; dedicated post-admission role revocation regression passes.
- Evidence logs: 304-independent-focused.log (all 151 then-current cases pass; static lint subsequently corrected), 304-role-final.log (9 current role tests pass), 304-final-types.log, 304-workspace-focused-final.log and 304-workspace-types.log; earlier logs retain failed/abandoned design diagnostics.
- Native/browser acceptance: not started. Read-only Compose inventory in installed-ownership-inventory.jsonl. Current project kodi-lab-30d25bcc383f, bundle /home/kyle/kodi-lab-deployment/pr301-integration-20261111/bundle, image revision c17aa3343a9a61762050e6425502c9313770aee2. Services healthy at audit; installation untouched.
- Disk: 26 GiB available at start; no cleanup performed.
- Blocked/skipped: none yet; Windows #275 cannot be claimed from Claw Linux.
- #305a merged: PR #312 https://github.com/kcook98765/kodi-lab/pull/312; head 6bae08b29f73d36a98f2c4cd095b2b7e6d825060; integration 75954e6432d889f7503baa3d737ac0714e60ee34 (exact tree match). private agent_projects source/owner/request table and schema 0010; project_create + project_status tools; generated inert Program/video seed; internal root lookup reuses existing workspaces without development bind mount; owner/grant checks, quotas and replay. Files under src/kodi_lab/application/agent_projects.py and new storage SQL/migration.
- #305a FOCUSED PASS: 165 distinct tests (projects 10, project migration 1, authenticated MCP wire 1, MCP foundation 17, initial schema 32, migrations 60, workspace mutation 34, managed role 10), 51.2s. Latest replay/template and wire-status lane: 9 passed plus Ruff/mypy, 9.8s. 13-file strict types pass. Project quota/pagination, Program/video seeds and template-change replay are covered. Self-review complete; no native acceptance claimed.
- Exact next action: #305 resource-folder creation in existing file edit, preserving rollback, no-follow paths and one generation per create; focused tests and PR. Then package/checkout intake and #306. MCP body limit is 64 KiB; chunked payload transfer may be needed for normal packages. Existing addon_zip validator strictly accepts video plugins/passive repositories; reuse its structural safety without falsely labeling Program ZIPs or broadening native installers. New Program imports should use an explicit editable-source validation path if implemented. Retirement still outstanding. Remaining #307/#308/#309 independent work eligible. No native acceptance yet.

## Delivery queue
304 role -> 305 projects -> 306 versions; 307 GUI can proceed independently; 308 reset -> 309 UI -> 310 final integration, full gate, bounded Claw acceptance.

All issue bodies saved beside this file. Keep per-PR tests, review, merge identity and limitations here. Never close issues on incomplete acceptance.

## #305b intake investigation (not implemented, still eligible)
- MCP request body limit is 64 KiB. Existing ZIP upload helpers and AddonZipImportCoordinator provide bounded no-follow staging, safe validation, global-heavy receipts and typed stale recovery; reuse these instead of new extraction/lifecycle machinery.
- validate_and_publish_addon_zip and native artifact revalidation deliberately support Python video plugins or passive repositories, not Program ZIPs. Editable Program ZIP support requires explicit source validation, without falsifying the native extension classification or broadening existing installers.
- Existing /api/native-zip-installations is bearer-authorized and returns staged native Kodi GUI delivery; source/native success must remain distinct. AddonZipInstallationCoordinator supplies fixed copied-video installation; review its target authorization/receipts before exposing install-for-test.
- Options to resolve within #305: bounded small packages, chunked input for larger packages, owner-scoped artifact mapping, editable import via existing workspace snapshot root, retirement. No package source code changed yet.
- #307 existing findings: SQL noVNC input triggers already exclude managed_remote_guards during interactive human ownership. BrowserRemote and mark_remote_dispatch need an explicit typed check/error; MCP _claim currently allocates refusal receipts before that fence. Preserve exact target class, ambiguity and successful receipt replay.

## #307 checkpoint
- Shared human-input check added to remote admission and mark_remote_dispatch. Retained interactive fences block even after expiry until existing native release proof. View-only does not block input.
- MCP checks a new managed action before receipt allocation and applies its normal input rate limit to refusals. Existing completed/ambiguous receipts are resolved first; no buffered input is replayed on release.
- Current credential/target reauthorization now propagates through managed adapter to actual shared dispatch. FOCUSED PASS in 307-focused-final.log: 139 tests, 70.4s; Ruff and strict mypy pass 12 files. Includes real existing noVNC owner fencing, shared dispatch and external regressions. Self-review complete; PR #313 merged at d878c23bec54243e3588addd18b5a2c447e83025, head 9ab9cb2131fabb688bcab720b372a0ecbcf97cf3; exact tree match.
- Inert custom WindowXML fixture prepared in tests/fixtures/agent-window (Select label/log; Back closes; no network/backend/settings writes). Native behavior NOT VERIFIED; defer actual qualification to #310 after coherent installation. Existing UI already labels human control and agent blocking; no viewer replacement needed.
- Concrete #305 gap found: workspace/storage.py:_open_mutation_parent requires pre-existing directories. New projects currently cannot add nested resource directories through workspace_edit. Fix with existing safe batch-import machinery or bounded atomic parent creation before custom-window native acceptance; do not claim full authored GUI workflow yet.

## #305 private resource paths checkpoint
- FOCUSED PASS: 77 tests (new resource paths 6, existing projects 10, workspace mutations 34, source promotion 27). Latest public-validation/type lane passes in 4.7s; Ruff/mypy pass all five changed/new Python paths.
- Only agent_projects-backed workspaces can create missing private parents. Legacy registered-source workspace/source-promotion rules remain conservative. Parent creation, file and digest publish as one generation. Rollback proves exact parent inode/emptiness; unknown postimages are retained for reconciliation.
- Private seed/source promotion explicitly denied; validation uses new truthful agent_owned_project classification, while managed deployment remains separately eligible. Original private seed unchanged.
- Self-review complete; preparing resource-path PR. Native fixture still NOT VERIFIED.
