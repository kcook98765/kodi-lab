# Agent-first implementation campaign

Authorization: owner prompt, 2026-11-11; scope #303 and #304-#310.
Claw lab-owned state disposable; unrelated data, repositories and host apps protected.

## Current checkpoint
- Starting main: f3a666ec45ce5e23bebd6b5105799bdf7f459bae. Current integrated main: 4a78965be2eead684144f723e7fb9201bbf391bc.
- Baseline schema verified: 0008_managed_bridge_preparation. Candidate schema: 0009_trusted_managed_lab.
- Worktree: /home/kyle/kodi-lab-worktrees/agent-first-20261111.
- Active issue: #305; branch feat/305-agent-owned-projects; no PR yet.
- #304 source merged: PR #311 https://github.com/kcook98765/kodi-lab/pull/311; head 860aa0221ed446341dabb5ad1d479aca6b4edef9; integration 4a78965be2eead684144f723e7fb9201bbf391bc. Native/integrated acceptance still open under #310.
- #304 final design: independent client managed_lab_role_version=1 opt-in; legacy/external preset and grants unchanged. Typed managed shortcut only for approved disposable managed instances; owner/source, runtime and lifecycle guards retained.
- #304 FOCUSED PASS: 192 distinct tests across role (10), role migration (1), Web admin/CSRF (1), initial schema (32), migration regressions (60), MCP foundation (17), production authorization (22), managed-version wire (10), workspace mutations (34), development inspection (5). Focused Ruff and mypy pass. Latest workspace lane: 49 passed, 18.9s including static checks.
- Self-review: corrected initial preset-only design to preserve separately authorized external access. Migration keeps role revision/audit history, refuses lossy downgrade. Corrected baseline stale initial-schema graph/future-head expectations.
- Additional review correction: workspace allocation honors role without base edit permission; creation/edit publication rechecks current credential, role, owner and source in the mutation transaction. Failed edits roll back bytes; dedicated post-admission role revocation regression passes.
- Evidence logs: 304-independent-focused.log (all 151 then-current cases pass; static lint subsequently corrected), 304-role-final.log (9 current role tests pass), 304-final-types.log, 304-workspace-focused-final.log and 304-workspace-types.log; earlier logs retain failed/abandoned design diagnostics.
- Native/browser acceptance: not started. Read-only Compose inventory in installed-ownership-inventory.jsonl. Current project kodi-lab-30d25bcc383f, bundle /home/kyle/kodi-lab-deployment/pr301-integration-20261111/bundle, image revision c17aa3343a9a61762050e6425502c9313770aee2. Services healthy at audit; installation untouched.
- Disk: 26 GiB available at start; no cleanup performed.
- Blocked/skipped: none yet; Windows #275 cannot be claimed from Claw Linux.
- #305a source ready for PR: private agent_projects source/owner/request table and schema 0010; project_create + project_status tools; generated inert Program/video seed; internal root lookup reuses existing workspaces without development bind mount; owner/grant checks, quotas and replay. Files under src/kodi_lab/application/agent_projects.py and new storage SQL/migration.
- #305a FOCUSED PASS: 165 distinct tests (projects 10, project migration 1, authenticated MCP wire 1, MCP foundation 17, initial schema 32, migrations 60, workspace mutation 34, managed role 10), 51.2s. Latest replay/template and wire-status lane: 9 passed plus Ruff/mypy, 9.8s. 13-file strict types pass. Project quota/pagination, Program/video seeds and template-change replay are covered. Self-review complete; no native acceptance claimed.
- Exact next action: finish #305a focused tests/types and self-review; create/merge coherent project bootstrap PR. Then implement bounded package/checkout intake (#305b), followed by #306. Remaining #307/#308/#309 independent work still eligible. No native acceptance yet.

## Delivery queue
304 role -> 305 projects -> 306 versions; 307 GUI can proceed independently; 308 reset -> 309 UI -> 310 final integration, full gate, bounded Claw acceptance.

All issue bodies saved beside this file. Keep per-PR tests, review, merge identity and limitations here. Never close issues on incomplete acceptance.
