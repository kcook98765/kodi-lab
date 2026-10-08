# Unified Add Kodi — Implementation 6A extension

`/add-kodi` is the ordinary entry for both external Kodi classes. The accessible
radio group asks **How will you use this Kodi?** Home / everyday Kodi is the
default. `/add-kodi?mode=development` preselects Development; Kodi's secondary
development link, Development and advanced external administration use it.
Invalid mode query values return 422. Managed lifecycle controls stay separate.

This composes the existing workflows. `add-kodi-mode.js` controls panel visibility,
focus availability and the choice lock, without accepting or translating IDs.
The household form/script retains native sign-in, exact transport disclosure,
explicit consent and existing recovery. The development form is extracted into
`development-pairing.html`, shared with `/pair-kodi`, and uses `pair-kodi.js`.
It keeps the original four-character code, optional native authentication, ports
and older-bridge links. Its existing HTTP pairing is explained before submission.
Agent development/deployment access remains separately authorized.

## Routing and recovery

| Choice | Initial request | Recovery | Browser state |
| --- | --- | --- | --- |
| Home | `POST /api/household-onboarding` | `POST /api/household-onboarding/{operation_id}/resume`, existing abandon and original-detail retry | Existing household operation key, ID plus sign-in namespace and `mode: home` |
| Development | `POST /api/external-targets/onboard-otp` | `POST /api/external-targets/onboarding/{target_id}/resume` | Separate development key, target ID (or null if unknown), name/address, sign-in namespace and `mode: development` |

The first endpoint still delegates to the household coordinator and
`external_production`; the second still delegates to external development
onboarding and `external_development`. Backend modules, target classification,
reviewed-build requirements, identity, OTP, transport consent, authorization,
CSRF, closed request schemas and existing recovery contracts are unchanged.
Requests do not contain a mode/class/permission override. Selecting Development
does not issue an agent grant or deployment permission.

The choice is disabled while either workflow is busy or unresolved. Reloading
honors the active record's owning mode instead of the URL preselection. No
request is made automatically on load. Each script guards double submissions
and hidden-panel handlers. Confirmed completion clears its pending record;
starting another Kodi requires an explicit local button. Home also clears its
pending record after a confirmed abandonment. The existing two-field household
record remains recoverable in its original Home key for the same sign-in.
Wrong-mode or older-sign-in records are rejected. Legacy development records are
neither imported into the unified page nor erased by it.

Submitted codes/passwords/usernames are cleared, as are secrets when leaving a
mode or the page. Only Home's already-confirmed transport disclosure can retain
the code in the visible consent field until explicit continuation, following the
existing flow. No OTP or credentials go into sessionStorage or localStorage.
Development's initial submission saves an unresolved marker before dispatch;
a lost/malformed result cannot silently reopen code submission. A recoverable
response with a target ID uses the existing no-code resume. A resume success
must match that saved target ID. Known definite initial failures can reopen the
form for an explicit retry, after secrets are cleared.

## Compatibility and backend limits

`/pair-kodi` and its API contracts remain operational. Its standalone form uses
the same shared partial and script, original pending key/record shape and old
development recovery. The primary links now point to Add Kodi. Advanced legacy
registration/pairing remains available in external administration.

Development onboarding has no durable caller operation ID for the initial
request. If that response is lost before a target ID is returned, the UI cannot
reconcile it safely by itself. It locks Development, advises reviewing existing
development connections, and offers advanced recovery by existing development
target ID. It does not invent a reset/abandon or resend behavior. If no recoverable
target exists, advanced administrator review is needed; there is no new automatic
escape from the unresolved marker.

Class checks remain authoritative on the backend. A production target UUID sent
to development resume is rejected without mutation, but the existing backend
maps its not-found/class rejection to `target_persistence_failed` (503), which is
less clear than an explicit wrong-class error. A target UUID used as a Home
operation ID is rejected as `operation_not_found`. These mappings are unchanged.
Manually entered IDs are opaque, so the UI cannot independently infer their class.

The Kodi list/detail snapshot freshness limitation from
[Implementation 6A](WEB-KODI-VIEWS-6A.md) remains: Connected describes the last
verified recorded Bridge relationship. Page viewing does not probe a Kodi.

## Validation and review

FOCUSED checks cover five new controller tests and the affected Add Kodi (4),
legacy pairing (19), navigation (24), household onboarding (7) and Kodi views
(26) files, each run individually: **85 tests**, plus focused Ruff and strict
mypy. The two existing Node workflow harnesses and JavaScript syntax checks also
run. This is focused validation, not release acceptance.

`tests/web/unified_add_kodi_browser.cjs` signs in normally to the preview and
intercepts every subsequent mutation. Only queued, fixture-based household or
development onboarding replies are allowed; no pairing reaches the server.
It tests both modes, keyboard radio selection and focus, disclosures, correct
request bodies, duplicate/hidden/completed submissions, credential clearing,
lost initial responses, mode-specific reload recovery, mismatched results,
stale/cross-mode storage and legacy pairing/recovery. At 1440, 820 and 375 px it
records **30 screenshots, 30 WCAG A/AA axe scans with zero violations**, and three
workflow check groups. Unified controls have 44 px targets and no horizontal
overflow. The legacy page retains its existing smaller disclosure summaries.

The existing Kodi browser runner uses fresh controller-rendered synthetic HTML
from a disposable test database: **30 layout/state checks, 30 zero-violation axe
scans, 32 screenshots**, plus dark/no-JavaScript detail review and a dark axe scan.
It covers connected/disconnected/attention/empty lists and details, Add Kodi and
shared keyboard navigation. Populated responses never enter preview storage.

```console
./scripts/check-focused tests/web/test_unified_add_kodi.py tests/web/test_add_kodi.py tests/web/test_pair_kodi_web.py tests/web/test_navigation.py tests/web/test_household_onboarding.py tests/web/test_kodi_views.py --source src/kodi_lab/web/app.py src/kodi_lab/web/kodi_views.py
KODI_LAB_BROWSER_TOOLS=/home/kyle/kodi-lab-web-preview/browser-tools/node_modules node tests/web/unified_add_kodi_browser.cjs
```

Evidence: `/home/kyle/kodi-lab-web-preview/evidence/unified-add-kodi/`.
`index.html` is a local screenshot gallery. `browser-report.json` records actual
URLs, mocked request paths/field names, layouts and axe results. The
`kodi-regression/` subfolder contains the renewed list/detail evidence.
`focused-regressions.log` records the first 59 passing tests; its combined rerun
was interrupted by the tool time limit during the last file. `focused-final.log`
records the remaining 26 passing tests. `static-final.log` records explicit final
Ruff/mypy checks on all eight Python files. `javascript-checks.log` and `preview-state-verification.json`
record the other final checks. All 110 non-session preview tables retain their row
counts and hashes; the target inventory remains empty. Normal browser sign-in
creates only authentication sessions. No real Kodi is probed or paired.

Preview entry points:

- http://192.168.2.191:8180/add-kodi
- http://192.168.2.191:8180/add-kodi?mode=development
- http://192.168.2.191:8180/pair-kodi (compatibility)
- http://192.168.2.191:8180/kodi

For read-only visual inspection, use the existing preview sign-in, select either
mode, resize the browser and expand native sign-in, port and recovery disclosures.
Do not submit Connect or recovery actions with real target details. Use the local
fixture screenshots to inspect connected/recovery outcomes. Both preview
systemd services remain running; only the isolated preview application was
restarted to load its new presentation route. No production service or trust
state changed. HEAD remains `3ff38c45d5ac3bb99e75a7736122b8f88e3810d2`;
Implementation 6A and this extension remain uncommitted for visual feedback.

Extension application files under `src/kodi_lab/`: `web/app.py`; `web/templates/add-kodi.html`,
`development-pairing.html` (new), `pair-kodi.html`, `development.html`,
`external-targets.html`, `kodi.html`; `web/static/add-kodi-mode.js` (new),
`add-kodi.js`, `pair-kodi.js`, `style.css`.

Extension tests and documentation: `tests/web/test_unified_add_kodi.py`
(new), `unified_add_kodi_browser.cjs` (new), `test_add_kodi.py`,
`add_kodi_browser_harness.js`, `test_pair_kodi_web.py`; this document (new).
Existing uncommitted 6A list/detail files are retained.
