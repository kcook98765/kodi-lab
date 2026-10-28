**No concrete P1/P2 regression or race found in this order change.**

- In `storage/repositories.py:6575–6586`, lease insertion is flushed before cancellation. An admission failure therefore raises before setting the viewer’s cancellation event.
- The reciprocal SQLite triggers in `storage/models.py:8610–8653` and migration `0087_novnc_control.py` arbitrate interactive ownership versus mutation admission. Moving cancellation after the INSERT does not create an unguarded interactive-admission window: concurrent writers cannot both establish conflicting ownership.
- The typed Stop exception requires a matching instance, operation ID, lifecycle kind, and running Stop record. The lifecycle coordinator creates that record before reserving the lease (`application/lifecycle_operations.py:119–129`). Exact-container Stop receipt validation and ownership retirement remain in place (`application/runtime_lifecycle.py:301–330`).

**Coverage and scope limitations**
- `test_refused_mutation_does_not_revoke_exclusive_human_viewer` directly covers the reported failure: pending interactive ownership denies a lifecycle reservation, and the registered display remains uncancelled. It does not itself cover claimed/active ownership, concurrent mutation admission, or successful Stop cancellation.
- Cancellation still occurs after **flush**, not transaction commit. A later rollback can therefore leave the display cancelled; that behavior is not introduced by moving an already pre-commit cancellation later.
- “Stop is admitted first, then revokes” describes this repository ordering, **not every entry point**: the existing Web Stop route calls `novnc_viewer.revoke_instance()` before `context.lifecycle.stop()` (`web/app.py:8284–8289`). That is outside this narrow order change.

Read-only source review only. No edits, execution, browser/VNC access, or delegation; **no native or gate PASS claimed**.