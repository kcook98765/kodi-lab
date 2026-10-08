# Kodi list and detail views — Implementation 6A

This slice adds authenticated, read-only `/kodi` and `/kodi/{target_id}` pages.
All `external_production` records appear under **Kodi in your home**, including
existing manually registered production records. The detail route uses the
production administration service's classification check: managed, external
development, and missing target IDs return 404. Managed names come from the
existing target table, without requesting broker inventory. Development controls
remain reachable through Development; external development has its existing link.

The normal list contains friendly names, status, observed Kodi version, access
labels and View / Settings links. Technical IDs occur only in link destinations
and inside the detail's closed Advanced diagnostics disclosure. Viewing has no
mutation form or handler beyond the shared shell's existing CSRF-protected sign
out. There is no page-specific JavaScript, polling, pairing, image capture,
reverification, lifecycle control, client grant or settings mutation.

## Display sources and status precedence

| Display | Existing source and meaning |
| --- | --- |
| Friendly name, version, address | `ExternalTargetAdministrationService.list_targets/get`; name is the existing target name, version is recorded native observation metadata. Unknown versions say Not yet known. |
| Connected | `evaluate_production_bridge_anchor_state` succeeds, and native/Bridge stored observations contain no unreachable or trust failure. This existing, network-free evaluator checks enabled confirmed native identity, completed household onboarding, native observation digest, identity generation, paired reachable Bridge, reviewed active build, pinned identity and endpoint binding. No new trust decision is introduced. |
| Administratively disconnected | Stored native administrative state is deactivated, or onboarding is abandoned. |
| Disabled | Existing target administrative state is disabled after native verification. New disabled/unverified registrations say Needs attention / Setup is incomplete. |
| Incomplete or blocked setup | Existing `HouseholdOnboardingRow.stage`, native verification/designation and Bridge pairing snapshot. In-progress stages say Setup is incomplete; blocked stages say Setup needs administrator attention. |
| Identity, sign-in or trust attention | Native identity mismatch/reverification, native authentication/capability failure, invalidated designation, Bridge revoked/reverification/authentication/identity/protocol failure, or a rejected stored Bridge anchor. Trust failures take priority over reachability. |
| Temporarily unreachable | Native observation or `ExternalBridgeService.status` reachability is unreachable, once administrative, onboarding and trust failures have been excluded. |
| Connection unknown | Bridge status is unavailable or its stored reachability is unknown. Neither a target record nor native reachability alone qualifies Connected. |
| Bridge connection | Friendly summary of those same states. Stored technical pairing/reachability remains inside Advanced diagnostics. |
| Last verified | Native target and Bridge snapshot `verified_at`, explicitly labelled separately. Native `last_observed_at` is shown only as Last checked in diagnostics because it can include a failed check. |
| Access | Read-only baseline; configured `semantic_remote` plus production `mutations_suspended` qualifies the remote-configuration label. Configured is not a claim of current control authorization or an agent grant. Existing global blocks, presets, transport acknowledgements, scopes and transient approvals still govern execution. |
| Screenshot preference | `ScreenshotPreferenceService.read`, independent of capabilities and trust. Missing preferences say Unavailable. Enabled does not assert capture availability. |

Administrative disconnection, blocked/incomplete onboarding, and administrative
limits take precedence over Connected. Trust attention precedes reachability.

## Backend limits

Snapshots have no reliable live freshness contract or background heartbeat.
Connected means the **last recorded verified Bridge relationship**, not that Kodi
is responding at page-render time. Every list/detail page states that limitation.
There is no new expiry threshold and no network probe. Native and Bridge reads
are separate database snapshots, so concurrent administration may appear on the
next page load; this presentation confers no execution authority.

Reservations created before a target record exists remain in Add Kodi's existing
recovery workflow. This page deliberately lists the existing inventory and does
not invent a second inventory from onboarding reservations. Unknown recorded
states stay conservative. This slice cannot diagnose a current network outage
without a separately authorized existing verification action.

## Repeatable validation and browser evidence

Run the focused controller file individually:

```console
KODI_VIEW_BROWSER_EVIDENCE=/home/kyle/kodi-lab-web-preview/evidence/kodi-6a \
  uv run --locked pytest tests/web/test_kodi_views.py
```

The optional export writes only synthetic fixture HTML into the evidence folder,
from real authenticated controllers using disposable pytest databases. It neither
reads nor populates persistent preview target state. It covers successful stored
trust, unreachable, identity attention, incomplete setup and disabled screenshot
preference. Fault tests also cover retired builds and stale bindings; page-read
SQL monitoring rejects any write, and probe/mutation methods are trapped.

Browser dependencies are already isolated outside the repository:

```console
KODI_LAB_BROWSER_TOOLS=/home/kyle/kodi-lab-web-preview/browser-tools/node_modules \
  node tests/web/kodi_views_browser.cjs
```

This runner signs in normally using the private preview password file, visits
actual preview pages, then intercepts document responses for populated examples. For these synthetic
documents, it fetches unchanged preview assets through the runner to avoid
Chromium's private-network subresource restriction on intercepted documents.
Fixture document URLs are recorded as synthetic in `browser-report.json`; they
are not persistent populated pages. All subsequent browser requests must be GET
or HEAD and stay on the preview origin. It checks 1440, 820 and 375 px layouts,
stacked phone cards, overflow, 44 px actions, keyboard disclosure/menu/link/skip
behavior, visible focus, WCAG A/AA axe checks, dark mode and no-JavaScript detail.
Add Kodi and shared navigation are checked without submitting onboarding forms.

The actual persistent preview URLs are:

- http://192.168.2.191:8180/login
- http://192.168.2.191:8180/kodi
- http://192.168.2.191:8180/add-kodi
- Detail pattern: http://192.168.2.191:8180/kodi/{existing-production-target-id}

The preview has no target records. Details are reviewed through synthetic browser
responses and saved screenshots; an unmocked missing ID correctly returns 404.
Use the existing preview-only password in
`/home/kyle/kodi-lab-web-preview/admin-password`; do not reset credentials.
Read-only visual review: sign in, open Kodi, resize the window, use the menu and
keyboard, and expand Advanced administration. For populated detail states, open
the screenshots under `evidence/kodi-6a/` or run the fixture browser runner.
Do not submit Connect, Resume, Abandon or advanced administration actions during
this visual slice. The two persistent preview services remain enabled/running.

## Recorded result

FOCUSED PASS: 26 new controller tests; 37 directly affected regressions (24
navigation, 4 Add Kodi, 2 screenshot preference, 7 household onboarding); focused
Ruff format/lint and strict mypy; JavaScript syntax for the browser harness,
navigation and Add Kodi. Fixture-export assertions also passed. This is focused
validation, not release acceptance.

Browser PASS: 30 layout/state scenarios, 30 WCAG A/AA axe scans with zero
violations, 18 recorded keyboard checks, plus dark-mode and no-JavaScript
detail checks. All 32 final screenshots are in
`/home/kyle/kodi-lab-web-preview/evidence/kodi-6a/`.
A separate dark-mode axe scan also passed. No page errors, horizontal overflow
or post-login mutation requests were observed. The automated checks do not
replace screen-reader user testing or human visual approval.

Business-state verification compared row counts and deterministic row hashes
for all 110 non-session preview tables, unchanged before and after review.
Normal sign-in created Web authentication sessions; target, onboarding, policy,
preference and client-authority records were not populated or changed.

Starting Git status was clean at
`3ff38c45d5ac3bb99e75a7736122b8f88e3810d2`; HEAD remains unchanged.
Files changed: `src/kodi_lab/web/app.py`, `src/kodi_lab/web/kodi_views.py`,
`src/kodi_lab/web/templates/kodi.html`,
`src/kodi_lab/web/templates/kodi-detail.html`,
`src/kodi_lab/web/static/style.css`, `tests/web/test_kodi_views.py`,
`tests/web/kodi_views_browser.cjs`, and this document. Changes remain uncommitted.

Review entry points: `desktop-list-connected.png`, `tablet-detail-connected.png`,
`phone-list-connected.png`, `phone-detail-connected.png`,
`phone-detail-disconnected.png` and `phone-detail-attention.png`.
`browser-report.json` contains every exact screenshot path and browser URL.
`preview-state-verification.json`, `focused-final.log`, `regressions.log` and
`fixture-export.log` retain the verification results.
