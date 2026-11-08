# Everyday Managed Kodi controls (#249)

The existing `/kodi#managed-kodi-heading` inventory keeps all four canonical
versions. Each card leads with the Kodi release, version name, status, profile
retention guidance and Start / Stop / View Screen controls. Desktop uses two
columns; narrow screens use one. View Screen is the former Open Screen control,
with the same exact URL and selectors.

The Web presentation distinguishes missing files (Create Managed Kodi for that
version), installed files without a prepared profile (Start prepares and starts),
and an existing stopped profile (Start uses its saved settings and add-ons).
Starting another version keeps retained profiles. Only one managed Kodi can run
at a time. Current Managed Kodi selects a version/workspace for add-on development;
ordinary Start, Stop and View Screen do not require a workspace.

## Local Advanced options

Reset, preparation/native connection details, exact profile/Bridge/diagnostics,
operation history, raw technical state and the version-specific add-on testing
link live in the existing card disclosure. Technical state is a named,
keyboard-focusable region. Reset still opens the same separate modal with its
required destructive-confirmation checkbox. This is no new Reset workflow.

Safety exceptions remain outside collapsed Advanced options:

- Each card's status, failure/protection message, action-unavailability reasons,
  required recovery link and completed-Reset trust warning.
- The global saved-request notice, original-request Resume action, required
  recovery link and error alert. Only the raw receipt remains in its disclosure.

These exceptions prevent hiding an unresolved operation or the next action needed
for safe continuation. All availability comes from the existing authorized status
adapter; opening Advanced never enables an operation or grants authority.

An available Stop or Reset means the action can be requested. Status polling does
not check playback. Its existing accessible action description explains that the
server checks playback and active use when the action is requested and may refuse
it. Reset's advice stays inside Advanced options. Known blockers, saved requests
and confirmation state take precedence over this advice.

Active or unavailable playback evidence still prevents safe shutdown. A refusal
remains visible in the original saved request while status refreshes; reading its
receipt never retries the action. If a late check leaves an uncertain operation,
its recovery message and request identity remain authoritative. This guidance adds
no playback observation, polling, endpoint or backend eligibility change. Actual
native playback-sensitive and connected-external acceptance remain separate.

## DOM and receipt contract review

`managed-slots.js` finds `[data-slot]` cards under
`[data-managed-slots]`, then queries descendants rather than direct children.
Reset under the disclosure retains its existing listeners. The controller uses
the description's `data-slot-available-description` only when the action is
available and has no blocker or saved-request/confirmation message.

| Required nodes | Placement / contract |
| --- | --- |
| `data-slot-action`, `data-slot-reason`, `data-slot-action-group` | Same action values and disabled reasons. Start/Stop remain primary; Reset is inside Advanced. The Stop group's state-dependent visibility is unchanged. |
| `data-slot-state`, `data-slot-message`, `data-slot-clean`, `data-slot-recovery` | Primary, updated in place; no card replacement or lost disclosure state. |
| `data-slot-screen`, `data-slot-screen-disabled`, `data-slot-screen-reason`, `data-slot-install` | Primary. Same exact screen/install URLs, disabled fallback and reasons. |
| `data-slot-preparation`, `data-slot-native`, `data-slot-advanced`, `data-slot-history`, `data-slot-technical` | Same selectors and values, now grouped inside Advanced. |
| `data-slot-request*`, `data-slot-resume`, `data-slots-error`, `data-slots-refresh` | Existing global request/recovery controls remain outside card disclosures. |
| `data-slot-confirm*`, `data-slot-cancel` | Same root-level modal, checkbox and focus-return targets. Advanced stays open when Reset confirmation closes. |

Human-facing labels, refusal messages and action advice are presentation only.
Machine state, snapshots, request UUIDs, saved local-storage
key, receipt fields, polling, explicit continuation, stale-response fences,
authorization and lifecycle services are unchanged. Failure/protection messages
retain precedence over ordinary guidance. GET never continues a mutation.

## Focused validation and visual fixtures

The managed slot DOM, route/lifecycle, conflict and MCP tests cover authorization,
mandatory Reset consent, stale snapshots, exact identity, protected use, uncertain
operations and original-UUID replay. Structural tests verify primary/Advanced
placement and accessible IDs/descriptions. No full release suite is required for
this presentation review.

```console
KODI_MANAGED_CONTROLS_EVIDENCE=/tmp/managed-controls ./scripts/check-focused tests/web/test_managed_slots_presentation.py tests/web/test_managed_slots_dom.py --source src/kodi_lab/web/managed_slots.py
KODI_LAB_BROWSER_TOOLS=/path/to/existing/node_modules KODI_MANAGED_CONTROLS_EVIDENCE=/tmp/managed-controls node tests/web/managed_slots_browser.cjs
```

The exporter uses real disposable lifecycle/controllers with a fake broker to
capture installation/preparation, starting, running, protected use, retained Stop,
completed Reset and uncertain Reset. Chromium intercepts every request and checks
1440/375/320px in both themes, availability, exact links, required warnings,
disclosure/modal keyboard behavior, stale-confirmation handling and accessibility.
Optional `KODI_MANAGED_CONTROLS_BEFORE` supplies #255 fixtures plus matching
`assets/`; the runner creates a self-contained `managed-controls-comparison.html`.
Keep generated evidence outside Git. These fixtures do not qualify native Kodi,
runtime acquisition, release or production deployment.

External Production and External Development page actions remain later #249 work.
