# Managed lifecycle change assessment

Baseline: GitHub main `04c610cd3811852bfe7582fc3a2cd62fa2edc262`.

Recent MCP activity was treated as ownership in four places: ordinary Stop's
resource guard, the shared Web card protection, retained-session selection, and
ordinary Start's cross-profile admission. Each used the idle duration with a
60-minute fallback even when idle stopping was disabled. Completed deployment,
launch and lifecycle receipts record meaningful activity when their mutation
lease is released; that history is not an active operation lease.

Version Start also created a 15-minute hold for both MCP and Web callers, and
target-oriented ensure-ready defaulted to 15 minutes. The Web card reused Stop
protection for Start and Reset, trapping an otherwise stopped retained profile.

The change removes activity-derived ownership from ordinary Start/Stop and
retained selection, removes the version Start hold, and defaults ensure-ready
to zero. Positive explicitly requested holds remain supported. Start, Stop and
Reset receive separate Web eligibility checks. Reset retains recent-use and
hold protection, confirmation, identity binding and destructive authorization.

An explicit hold protects running use. A confirmed stopped profile can start
despite old hold records; a successful Stop ends the preceding running use.
On upgrade, unexpired running holds remain protected because older records do
not distinguish implicit from explicit origins. No schema/data migration is
needed. Omitted ensure-ready durations replay their saved intent; only fresh
requests default to zero. Saved version receipt hold values remain unchanged. Pending,
ambiguous and unresolved operations retain their recovery requirements.

Idle activity/enrollment and the 60-minute idle-stop default remain unchanged.
Default-OFF would make unattended development sessions less surprising, but
requires separate owner/product evidence and is not part of this change.

Validation uses disposable fixtures and a fake broker. No installed service,
real Kodi instance, production database or development-root mount is changed.

## Focused validation

| Selection (each file run individually) | Result |
| --- | --- |
| Application resources, version lifecycle, agent lifecycle, ordinary Start selection, retained development, clean Reset, development-session workflow | 154 passed |
| Web activity regression, slots, agent lifecycle, integrated Web/MCP, version MCP, resources, DOM/controller, presentation, development session | 88 passed |
| Ruff format/lint and strict mypy on all 20 changed Python files | Passed |
| Chromium managed-card fixtures at 1440/375/320px, light/dark | 54 layouts; no accessibility violations, JavaScript errors or mutation requests |

The new activity regression file covers completed deployment and accepted launch
receipts from a different actor, successful Web Stop and immediate restart with
idle stop enabled or disabled, profile/add-on/workspace preservation, Current
Managed Kodi state consistency, expired/unexpired surviving holds on stopped
profiles, and MCP Start followed by Web Stop/restart. Existing suites cover
mutation leases, unresolved reconciliation, active viewers/input, playback,
capacity, idle eligibility and destructive Reset. Added checks cover automatic
idle protection during viewing/input and explicit holds, omitted-duration replay
of older ensure-ready requests, and retained legacy version receipt hold values.

No full regression suite or native runtime acceptance was run. The original
stopped-Omega/disabled-Start condition is covered with the canonical Omega target
and disposable broker/profile fixtures; the real retained Omega was untouched.
