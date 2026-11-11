# Agent-first implementation campaign

Authorization: owner prompt, 2026-11-11; scope #303 and #304-#310.
Claw lab-owned state disposable; unrelated data, repositories and host apps protected.

## Current checkpoint
- GitHub main verified: f3a666ec45ce5e23bebd6b5105799bdf7f459bae.
- Baseline schema verified: 0008_managed_bridge_preparation. Candidate schema: 0009_trusted_managed_lab.
- Worktree: /home/kyle/kodi-lab-worktrees/agent-first-20261111.
- Active issue: #304; branch feat/304-trusted-managed-lab; no PR yet.
- Completed/merged campaign issues: none.
- #304 final design: independent client managed_lab_role_version=1 opt-in; legacy/external preset and grants unchanged. Typed managed shortcut only for approved disposable managed instances; owner/source, runtime and lifecycle guards retained.
- #304 FOCUSED PASS: 192 distinct tests across role (10), role migration (1), Web admin/CSRF (1), initial schema (32), migration regressions (60), MCP foundation (17), production authorization (22), managed-version wire (10), workspace mutations (34), development inspection (5). Focused Ruff and mypy pass. Latest workspace lane: 49 passed, 18.9s including static checks.
- Self-review: corrected initial preset-only design to preserve separately authorized external access. Migration keeps role revision/audit history, refuses lossy downgrade. Corrected baseline stale initial-schema graph/future-head expectations.
- Additional review correction: workspace allocation honors role without base edit permission; creation/edit publication rechecks current credential, role, owner and source in the mutation transaction. Failed edits roll back bytes; dedicated post-admission role revocation regression passes.
- Evidence logs: 304-independent-focused.log (all 151 then-current cases pass; static lint subsequently corrected), 304-role-final.log (9 current role tests pass), 304-final-types.log, 304-workspace-focused-final.log and 304-workspace-types.log; earlier logs retain failed/abandoned design diagnostics.
- Native/browser acceptance: not started. Read-only Compose inventory in installed-ownership-inventory.jsonl. Current project kodi-lab-30d25bcc383f, bundle /home/kyle/kodi-lab-deployment/pr301-integration-20261111/bundle, image revision c17aa3343a9a61762050e6425502c9313770aee2. Services healthy at audit; installation untouched.
- Disk: 26 GiB available at start; no cleanup performed.
- Blocked/skipped: none yet; Windows #275 cannot be claimed from Claw Linux.
- Exact next action: complete #304 static checks/self-review, create and merge reviewed PR, record integration identity; begin #305 bounded owner-scoped intake.

## Delivery queue
304 role -> 305 projects -> 306 versions; 307 GUI can proceed independently; 308 reset -> 309 UI -> 310 final integration, full gate, bounded Claw acceptance.

All issue bodies saved beside this file. Keep per-PR tests, review, merge identity and limitations here. Never close issues on incomplete acceptance.
