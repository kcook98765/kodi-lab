# Administrator views and managed runtime availability

This change adds presentation preferences and runtime diagnostics. It does not
deploy, acquire runtimes, pair Kodi, change MCP grants, or modify existing Kodi
instances. Local commit approval was granted in the subsequent final review.

## Diagnosis (2026-10-09)

The reported matrix was reproduced in the isolated preview at
`http://192.168.2.191:8180`. It was not reproduced in the current container
deployment on port 18100. These applications have separate state and brokers.

| Version | Isolated preview | Current container deployment |
| --- | --- | --- |
| 19.4 Matrix | Reviewed recipe; no local installation record. Acquisition unavailable because preview broker omits installation APIs. | Ready; approved, validated revision and exact local image; existing instance. |
| 20.5 Nexus | Reviewed recipe; no local installation record. Same broker restriction. | Ready; approved, validated revision and exact local image; existing instances. |
| 21.3 Omega | Mirrored validated installation record and one pinned instance. Catalog selectable; acquisition unavailable under the same preview restriction. | Ready; approved, validated revision and exact local image; existing instance. |

The preview broker in `kodi-lab-web-preview/screen-preview-broker.py` exposes only
ping, broker identity, selected inventory, runtime observation and screenshots.
It deliberately lacks `capabilities` and all installation operations. Its
`unknown_operation` response previously became the same opaque “Installation
unavailable” label as transport, image verification and eligibility failures.
The preview restriction is preserved.

Production was inspected read-only: its actual Web process uses
`/var/lib/kodi-lab`, its broker uses `/run/kodi-lab/broker.sock`, and all four
installation operations are present. All three installation records are ready,
with exact local images confirmed by the broker. The active acquisition worker
is absent and the acquisition lock is free. No missing image, policy, catalog,
eligibility or lifecycle fault was reproduced there. The separate host service
on port 8100 uses historical schema-0053 validation state and does not match the
reported matrix either.

## Existing workflow and trust boundaries

- `web/app.py` supplies `/runtimes`, authenticated polling endpoints and a
  same-origin/CSRF-protected empty-body installation POST. Only Matrix, Nexus and
  Omega and the existing install/retry/reacquire actions are accepted.
- `application/runtime_installations.py` requires the four broker operations,
  reconciles exact worker identity and the authoritative acquisition lock,
  and evaluates local installation records. Ready additionally requires an
  eligible revision and broker confirmation of the exact image.
- `application/runtime_matrix.py` projects catalog eligibility for selecting
  new pins. Selectable is not a check of acquisition capability, broker health,
  or physical image availability. A reviewed recipe is not an acquired image.
- `application/runtime_eligibility.py` requires the approved revision, exact
  artifact, compatible execution profile, recorded reviewed recipe, sealed
  acquisition provenance and matching validation evidence. Selection policy
  still governs release/support tiers and trusted custom builds.
- `broker/reviewed_installations.py` derives a closed worker command, exact image
  and fixed mounts from the installed broker. Web gains no Docker authority.
  Global acquisition admission, worker isolation and identity checks remain.
- `runtime/install.py` invokes signed/pinned recipe acquisition, assembly and
  fixed two-cycle validation, records the attestation and approved immutable
  revision, then bootstraps a **stopped disposable managed pin**. Existing pins
  are retained even after reacquisition; they are never silently repinned.
- Instance creation uses `application/managed_instances.py`. Provisioning,
  starting, stopping and other activation work remain under existing lifecycle,
  isolation, eligibility and fresh identity checks; installation does not start
  an existing Kodi instance.

## Changes

Installation cards now distinguish missing backend capabilities, unavailable
broker transport, denied service identity, unverifiable worker/lock, ineligible
runtime records and failed exact-image verification. Stored records and existing
instance navigation remain available when acquisition is disabled. Missing
runtime files retain the existing explicit Reacquire action. Web reports refused
POSTs, busy acquisition and interrupted status polling instead of silently
refreshing, and polling preserves keyboard focus. The page explains acquisition,
stopped instance setup and the separate provisioning/start step.

No runtime management architecture or backend acquisition trust checks change.
No new backend correction is needed for the currently inspected deployment.
For an installation genuinely lacking compatible broker APIs, the smallest
operational correction is to install a compatible reviewed broker through the
separately authorized deployment process. Enabling acquisition on this restricted
preview is not that correction. Incomplete instance setup retains its existing
administrator installer recovery instruction; Web does not invent a repair API.

## Standard and Development

The visible selector appears outside the collapsible navigation on every
authenticated shared-shell page. A dedicated authenticated, same-origin,
CSRF-protected form writes only `standard` or `development` into an HttpOnly,
SameSite=Strict cookie, with the application's existing Secure-cookie setting
and a one-year lifetime. It uses no local/session storage and no database table.
Absent/invalid preferences select Standard. This is a browser-local preference
for the existing single Web administrator, independent of the login session.

Standard navigation is Home, Kodi, Agents and Settings. Home and Kodi emphasize
external production household installations. Standard Add Kodi opens Home;
Development onboarding remains available by explicit deep link or recovery of
an existing Development operation. Agents retains basic setup, client/token
controls and household grants/scopes; advanced preset creation, workspace/source
bindings and development target controls are omitted. Current client presets
are displayed without changing them. Settings links to household connection and
screenshot settings, agents and Bridge setup instructions in Add Kodi.

Development preserves the existing five navigation sections and all prior
controls, including managed/runtime administration, external development,
workspaces, add-ons, advanced MCP and full settings.

The preference is never consulted by lifecycle, target classification, MCP
authorization or endpoint-security code. Existing authorized routes remain
available in Standard and Development. Development deep links display an
explanation in Standard. Explicit Home `#instances` and `#managed-details`
fragments reveal retained managed controls and load their existing polling
script without changing the preference. With JavaScript disabled, the selector
and navigation still work; choose Development for managed Home controls.
Managed errors retain a visible attention summary on ordinary Standard Home.

## Isolated verification and evidence

Focused tests cover default/persistence/authentication/CSRF/origin/no-state-write
semantics, retained routes and backend actions, runtime admission, eligibility,
provenance, instance selection and the Web-to-controlled-broker worker workflow.
No cold acquisition, real runtime build or live Kodi lifecycle test is used.

The browser runner is `tests/web/admin_views_browser.cjs`. It signs into the
isolated preview, tests its real preference endpoint and disabled installer, and
then renders synthetic household/runtime HTML from real authenticated controllers
against disposable test databases. Installation progress/failures are intercepted
test responses; screenshots of these states do not prove a real download/build.
No pairing, MCP permission, credential or lifecycle POST reaches the preview.

Evidence is outside application state at
`/home/kyle/kodi-lab-web-preview/evidence/admin-modes-runtime/`:
screenshots, browser report, focused logs, starting worktree patch/status and
container preservation snapshots. Sizes are desktop 1440x900, iPad landscape
1024x768, iPad portrait 768x1024 and phone 390x844. Both preview services remain
running for review.

## Task file manifest

The final commit contains these 23 files. Shared files were isolated against
the expected HEAD to exclude pre-existing screen-preview work. That earlier work
is preserved separately in a named Git stash and the external evidence backup.

```text
docs/ADMIN-VIEWS-RUNTIME-AVAILABILITY.md
src/kodi_lab/application/runtime_installations.py
src/kodi_lab/web/admin_views.py
src/kodi_lab/web/app.py
src/kodi_lab/web/static/add-kodi-mode.js
src/kodi_lab/web/static/navigation.js
src/kodi_lab/web/static/runtime-installations.js
src/kodi_lab/web/static/style.css
src/kodi_lab/web/templates/add-kodi.html
src/kodi_lab/web/templates/base.html
src/kodi_lab/web/templates/index.html
src/kodi_lab/web/templates/kodi.html
src/kodi_lab/web/templates/mcp-clients.html
src/kodi_lab/web/templates/navigation.html
src/kodi_lab/web/templates/runtimes.html
src/kodi_lab/web/templates/settings.html
tests/application/test_runtime_installations.py
tests/web/admin_views_browser.cjs
tests/web/test_admin_views.py
tests/web/test_app.py
tests/web/test_home_agents.py
tests/web/test_kodi_views.py
tests/web/test_navigation.py
```

`test_managed_instance_navigation.py` already existed untracked before this task.
Its task-only preference fixture was removed, and the original file is preserved
with the earlier work rather than included in this commit. Existing Web tests
of full administration now select Development explicitly. Their preference
fixtures avoid constructing a client for tests that require unconfigured login
state. New tests separately verify Standard first-use behavior.

## Implementation verification (before final scope isolation)

**FOCUSED PARTIAL** overall: all 195 focused tests across 14 files pass, and task
implementation/added tests pass strict mypy. The wider Web-test mypy check retains
the unchanged `tests/web/test_app.py:509` ASGI-callable `.state` typing error.
That same single error was reproduced from an archive of the expected HEAD.
No unrelated typing repair or full repository qualification was performed.

| Focused test file | Passed |
| --- | ---: |
| web/test_admin_views.py | 12 |
| web/test_navigation.py | 24 |
| web/test_home_agents.py | 6 |
| web/test_kodi_views.py | 26 |
| web/test_app.py | 46 |
| web/test_managed_instance_navigation.py | 4 |
| web/test_runtime_installations.py | 4 |
| web/test_runtime_installation_integration.py | 1 |
| web/test_runtime_matrix.py | 8 |
| application/test_runtime_installations.py | 14 |
| application/test_runtime_eligibility.py | 18 |
| application/test_managed_instances.py | 22 |
| broker/test_reviewed_runtime_installation_operation.py | 5 |
| web/test_unified_add_kodi.py | 5 |

Focused Ruff lint/format, strict mypy on nine changed paths, relevant JavaScript
syntax checks and `git diff --check` pass. The runtime subsystem and onboarding
focused gates independently report FOCUSED PASS.

Browser results: 44 page/layout and Axe states, 60 screenshots, zero horizontal
overflow, zero Axe violations and zero browser errors across all requested
viewports. Keyboard, touch-menu behavior, no-JavaScript switching, explicit
managed Home fragments, and real-preview cookie persistence pass. Installation
POST progress/refusal/failure results are controlled fixtures, not real builds.

Both independent read-only reviewers report PASS with no unresolved finding.
Reviewer A verified runtime integrity and the deployed/preview distinction.
Reviewer B inspected source and rendered screenshots across all four sizes;
screen-reader semantics were checked through source and Axe without a live
screen-reader session.

The Docker preservation comparison covers 104 containers with no state, image,
start/finish time or restart-count changes. No container lifecycle, image or
network mutation events occurred during the task. Thirteen pre-existing tracked
files outside the task allowlist match the recorded starting worktree bytes.
These implementation observations preceded the separately authorized final local
commit review. Final scope and repeat checks are recorded below; no production
deployment is authorized. Both isolated preview services remain active.

## Final local commit review

The read-only security reviewer found no authorization or runtime-integrity
blocker. Their scope finding was resolved by excluding the pre-existing untracked
managed-navigation test and removing its task-only fixture. The four shared files
were rebuilt from the expected HEAD plus this task, without screen-preview
implementation changes. No functionality was added during final review.

Final checks use this isolated 23-file scope. The previously passing application,
eligibility, managed-instance and broker tests are reusable because their files
and implementation dependencies are unchanged. Affected Web tests, controller
fixtures, browser assertions and accessibility checks are repeated. The existing
ASGI `.state` mypy error remains a reproduced baseline limitation; no unrelated
typing change is included. Exact results and Git preservation metadata are in
`evidence/admin-modes-runtime/final-review/` outside application state.

Runtime coverage still excludes cold acquisition, real installation and actual
provisioning or activation. Browser progress and failures use intercepted fixture
responses. Production services, images, instances, credentials and MCP grants
are not changed by this final review or local commit.

Final result: **FOCUSED PARTIAL** solely for the reproduced unchanged
`tests/web/test_app.py:509` mypy error. All 191 relevant tests across 13 files pass:
137 tests were repeated after scope isolation (132 Web and five broker tests),
and 54 application/eligibility/managed-instance tests reuse unchanged passing
evidence. The excluded pre-existing managed-navigation file accounts for four
of the earlier 195 tests and is outside the final commit scope.

Ruff lint/format passes on all nine changed Python paths. Strict mypy passes
on eight changed paths; the remaining changed test file has only the documented
baseline error. Four JavaScript syntax checks and Git diff checks pass. The
repeated browser run passes 44 layout/Axe states with 60 screenshots, no overflow,
violations or browser errors, keyboard/mobile/deep-link/no-JavaScript checks and
real-preview preference persistence. Its request log confirms exactly two
intercepted installation POSTs per viewport (busy then accepted), with no duplicate
installation requests from polling. Final independent security/scope review passes.

## Exact reviewed Ubuntu support failures

The socketless Piers acquisition helper returns a closed failure category for an
unavailable pinned Ubuntu support package, an exact support-byte mismatch, or a
support download exceeding its existing bounds. The installer records the same
category in the original failed operation. Raw upstream error text, URLs and
response bodies do not become public failure state. Unknown helper failures
retain the generic closed failure behavior.

`pinned_support_unavailable` requires checking the exact reviewed support closure
before another retry. A downloaded OSTree app/platform/graphics closure does not
prove the reviewed Ubuntu packages remain available. Do not resolve newer DEBs,
relax signature/hash/size checks, change recipe pins, or treat an image build as
installation acceptance. Recovery requires the exact pinned bytes to become
available, or a separately reviewed new recipe. These diagnostics do not retry,
approve an artifact, provision an instance or alter an existing runtime pin.
