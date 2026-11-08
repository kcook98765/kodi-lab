# External Production Kodi presentation (#249)

`/production-targets` remains the existing production registration inventory;
`/kodi/{target_id}` remains its read-only detail page. Both use the existing
`KodiViews` stored-state projection and a shared summary macro. No new inventory,
connection mechanism, classification, backend permission or lifecycle is added.

The page explains that household Kodi runs outside Kodi Lab. Kodi Lab does not
own its process, container, filesystem or runtime. Environment identity continues
to come from the stored target type, independently of status, trust and grants.
Connected means the last recorded verified relationship, never a fresh probe or
an assertion that an agent is authorized.

## Primary information and Advanced options

Connection status, human-readable Bridge relationship/version/verification time,
reconnect availability, inspection limits, capture preference and privacy warnings
remain primary. Unknown capture preferences are not interpreted as enabled.
The existing reconnect URL is offered only for enabled registrations; disabled
and retired registrations explain why reconnect is unavailable.

The existing native probe is labeled Check Kodi connection. Its service requires
a disabled registration, so the UI disables it for enabled registrations and
explains the existing reconnect alternative. A probe records native identity;
it does not verify Bridge trust or grant access. Retired registrations omit it.

Exact identifiers, raw verification/designation states, scopes, capabilities,
endpoint/credential settings, HTTP acknowledgements, OTP pairing, trust
confirmation/reverification/revocation and retirement remain in local Advanced
options with their original selectors, request bodies and confirmation gates.
Advanced opens initially for setup/trust attention or retired credential cleanup.
Trust failures remain prominently visible even if an administrator has disabled
the registration. Important errors are outside disclosures; reading Advanced
does not dismiss a permission denial. Active pending/approved control requests
remain outside disclosures. Only an empty queue is collapsed. The deny-only
global production control remains available locally under Advanced, automatically
open when blocked, with a primary notice/link when the block is active.

The detail page remains free of mutation controls. Its connection link targets
the existing registration fragment; overview, access, screenshots, connection,
global safety and reviewed-build bookmark IDs remain intact.

## Authority and limitations

Page views perform no network probes, captures or writes. Authentication, CSRF,
origin checks, native/Bridge identity binding, exact OTP/reconnect contracts,
production scopes, agent target grants, capability ceilings and screenshot
authorization/preferences are unchanged. Existing operation identity, expiry,
one-shot approval, cancellation and credential-cleanup semantics are unchanged.
No process/container Start/Stop/Reset controls are created for external Kodi.

The Kodi detail page provides Allow screenshots — Off / On through the existing
revision-protected preference API. Both external classes default OFF. Authenticated
inspection access and the exact target grant remain required, with the current
production inspect scope, trusted Bridge and verified capability/transport checks.
No separate visual preset or scope is required. Managed screenshots have no toggle.
Changing this setting does not capture an image or repair Bridge trust.
An existing blocked onboarding record can be identified here, but the page cannot
reconstruct its owner's in-browser request identity. The existing connection guide
is linked without promising that an arbitrary saved request can be resumed.

## Focused review

`test_production_presentation.py` exports disposable real server-rendered fixtures
when `KODI_PRODUCTION_UI_EVIDENCE` is set. Its structural tests cover connected,
unavailable, identity mismatch, invalidated trust, disabled, disabled with failed
trust, blocked onboarding, screenshots disabled and retired registrations.
Existing route/security tests cover anonymous/CSRF denial, production grants,
OTP, reconnect, screenshot preference authorization and retirement/recovery.

`production_ui_browser.cjs` consumes those fixtures with an existing Playwright/Axe
installation identified by `KODI_LAB_BROWSER_TOOLS` and exact-base fixture exports
identified by `KODI_PRODUCTION_UI_BEFORE`. Every request is intercepted; explicit
native probe attempts receive a synthetic 403. It checks both existing pages at
1440/375/320px in light/dark themes, disclosure/menu keyboard use, persistent
permission errors, WCAG A/AA Axe findings and horizontal overflow. Screenshots,
JSON reports and a self-contained before/after HTML comparison are saved outside
Git. Fixture browser results are not live household/native acceptance.
