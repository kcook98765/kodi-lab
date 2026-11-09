# Unified administrator navigation (#248)

All authenticated administrators see one sidebar, independent of any old
`kodi_lab_admin_view` cookie. This is discoverability, not a permission grant.

| Destination | Existing URL | Purpose |
| --- | --- | --- |
| Overview | `/kodi` | Compact recorded-state summaries and links; no operational controls. |
| Managed Kodi | `/managed-kodi` | The existing four ordinary managed version slots, including lifecycle controls. |
| External Production Kodi | `/production-targets` | Existing household connections outside Kodi Lab. |
| External Development Kodi | `/external-targets` | Existing test/development connections outside Kodi Lab. |
| Development Tools | `/development` | Current Managed Kodi, sources, workspaces and testing. |
| Agents & Permissions | `/mcp-clients` | Explicit client, credential, grant and permission administration. |
| Advanced | `/settings` | Existing advanced administration and recovery links. |

Managed Kodi owns the existing four-version component and controller on its own
page. Current Managed Kodi remains the selected development environment/workspace
workflow. Managed registrations remain Advanced diagnostics and recovery. Other
page sections mark their dedicated destination active. The shared responsive
menu retains Escape, focus restoration, resize behavior and no-JS links.

Legacy `/` and `/kodi#instances` / `#managed-details` redirects still reach Advanced
registrations. Old `/kodi#managed-kodi-heading` and per-version bookmarks reach
`/managed-kodi` through client-side history replacement, preserving query strings
and fragments. Overview includes a direct link for browsers without JavaScript.
The server does not attempt to read fragments.

Overview reports Web availability and application version, prepared installations,
recorded running versions and attention, separate external production/development
connection counts, and registered/enabled agents. Counts use stored application
state; preparation means the latest installation was recorded ready. These are
not live readiness or agent authority claims. No service-wide health snapshot is
stored, so Overview adds no service checks, polling or probes. Existing service
status remains on diagnostic pages.

## Dependency audit

| Remaining dependency at #254 | Classification | Replacement |
| --- | --- | --- |
| `admin_view_context` reads cookie and exports `admin_view_enabled` / `admin_view_mode` | Presentation and older-template compatibility | No cookie read. Deprecated constant context keys remain for older templates; current templates do not consume them. |
| `base.html` derives `development_view` and prints a switch-view notice | Presentation | Removed. Existing authentication and page routes are unchanged. |
| `navigation.html` selector and Development link filter | Presentation | One seven-destination sidebar; selector removed. |
| `navigation.js` submits current fragment through the selector | Compatibility | Selector handler removed; existing bookmark redirects retained. Managed page has its own active state; old managed anchors redirect client-side. |
| `kodi.html` hides development category and advanced links | Presentation | Separate category summaries are discoverable. No target is reclassified. |
| `add-kodi.html` / `add-kodi-mode.js` hide managed help and external purpose choices via `data-standard-view` | Presentation | Purpose choices are visible; existing onboarding activity/busy locks still prevent switching an unfinished connection. OTP, reconnect, consent and route/query handling are unchanged. |
| `mcp-clients.html` filters presets and advanced administration | Presentation | Recommended production inspection presets stay in ordinary setup. Other assignable presets use a separate explicit client form inside the existing Advanced disclosure. Custom presets and Kodi policies stay advanced. |
| `agent-clients.html` filters preset options, targets and source/workspace controls | Presentation | All target categories are listed. Preset changes and source/workspace administration use the existing local Advanced disclosure. Actual eligibility, production trust and scope conditions remain. |
| `POST /admin-view` | Compatibility; authenticated mutation transport | Retained authenticated same-origin/session-CSRF checks, closed mode values and safe redirect validation. Returns 303 to the supplied safe path/query/fragment and expires the obsolete cookie. No preference or authorization state is created. |
| `novnc.py` HTTPS check for `/admin-view` | Actual transport safeguard | Retained unchanged, as are installer/proxy route guards. |

Search covered `admin_view_mode`, `admin_view_enabled`, `development_view`,
`ADMIN_VIEW_COOKIE`, `data-standard-view`, `/admin-view`, associated scripts and
Python/browser tests. No additional cookie-based authorization boundary remained
after #254. Authentication/session/CSRF dependencies and MCP application service
checks enforce actual authority; presentation flags never replace them.

## Agent security and conservative defaults

New client forms have no selected preset. The custom preset ceiling also starts
empty, with no permissions selected; unassigned target policies start empty.
Existing client presets and assigned target policies retain their stored
selection. Merely opening any control performs no permission mutation, target
grant or credential issuance. The page explains that visibility confers no access.

Both client creation forms call the same existing endpoint, with explicit form
validation. Assigning an empty policy sends nothing. Preset immutability,
assignability, custom permission ceilings, exact target/source grants, workspace
eligibility, production scopes/Bridge revisions and screenshot permissions remain
enforced by the unchanged backend. Preview retains #254's exact action-specific
consent and sealed evidence, independent of these presentation changes.

## Focused review and portable evidence

Run affected files individually with `scripts/check-focused`; no release suite
or runtime acquisition is needed for this UI review. Export disposable fixtures:

```console
KODI_ADMIN_VIEW_EVIDENCE=/tmp/kodi-navigation-review uv run --locked pytest -q tests/web/test_admin_views.py::test_export_isolated_browser_fixtures
KODI_LAB_BROWSER_TOOLS=/path/to/existing/node_modules KODI_ADMIN_VIEW_EVIDENCE=/tmp/kodi-navigation-review node tests/web/admin_views_browser.cjs
```

The browser runner intercepts every request and uses no service login or live
broker. It checks missing/Standard/Development/invalid cookies at 1440/375/320px,
both light and dark, plus agent defaults, onboarding choices, keyboard behavior,
legacy bookmarks and no-JS navigation. `browser-report.json` records the result.
Optional `KODI_NAV_BEFORE_FIXTURES` supplies the #254 fixture directory with its
matching `assets/`; the runner generates a self-contained
`navigation-comparison.html` with embedded before/after sidebar screenshots.
Keep full generated evidence outside Git; selected sanitized screenshots for
#277 are in `docs/images/overview-managed-kodi/`. This evidence is UI fixture validation,
not release, native Kodi or production acceptance.

The page ownership correction in #277 retains #249's existing managed action
organization. Browser fixture evidence does not establish deployed or native acceptance.
