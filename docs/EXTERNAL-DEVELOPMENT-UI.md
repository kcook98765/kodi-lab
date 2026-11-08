# External Development Kodi presentation (#249)

The existing `/external-targets` inventory now leads with each friendly name,
known Kodi version, registration setting, recorded native identity/check time,
Bridge pairing/reachability/verification time, human-readable verified support
and the next setup action. Kodi runs on another device; Kodi Lab does not host,
start, stop or reset its process. There is no new inventory or registration flow.

The pure `web/development_targets.py` presentation accepts only the stored
`external_development` category. It uses the existing native and Bridge read
models. It does not import Production presentation or evaluate production trust,
grants, health-derived categories or deployment eligibility. A successful stored
connection can say **Connection verified**, never **Ready for deployment**.

## Distinct evidence and authority

- The configured capability maximum remains in Advanced administration.
- Verified native Kodi and Bridge support are shown separately in ordinary
  language, with their raw capability names retained in Advanced.
- Agent permissions, the exact target grant/policy, source grants, workspace
  binding and add-on provenance remain independently enforced by existing
  services. These are not inferred from connectivity or verified capabilities.
- Add-on transfer outside the configured maximum, or without recorded Bridge
  support, is explained. Even recorded transfer support does not establish
  deployment readiness or authority.

Existing links are `/mcp-clients`, `/development`, `/development-sources`,
`/agent-workspaces`, `/add-kodi?mode=development` (legacy fallback `/pair-kodi`)
and `/reconnect-kodi/{target_id}`. They are navigation only and assign nothing.
The Workspaces Web prerequisite check covers Managed Kodi, not this external
connection. External testing/deployment remains the existing authorized agent
workflow, with its capability, workspace and provenance checks. This page does
not introduce a deployment action, external preflight or source-grant mechanism.
The existing Web-only development-baseline API is unchanged; no alternative
baseline or deployment flow is created.

## Progressive disclosure and controller contract

Per-target Advanced holds exact identities, protocol/diagnostic fields, configured
maximum amendments and receipts, endpoint/credential changes, OTP/trust
administration, registration audit history and retirement. Manual registration is
the original form, now below the current connections. The original article UUID,
`include_retired` query, onboarding parameters and API URLs remain intact.

Identity mismatch, authentication/protocol failures, pairing/verification review,
saved reconnect, outstanding credential cleanup and HTTP/privacy notices stay
primary. Advanced opens for incomplete verification/pairing, pending native
confirmation, saved reconnect, trust failure and retired cleanup. The OTP section
opens when its supported pairing/confirmation action is needed. Opening a
disclosure or reading Bridge information no longer dismisses a request error.
Kodi retains an observation ID after native confirmation. Its presence alone is
not unfinished work: verified registrations keep Advanced collapsed and do not
offer a confirmation that the existing service would reject.

The existing `[data-target-id]` article still owns all controller nodes. Its first
`h3` remains the friendly name used in exact amendment/retirement confirmations.
The original `data-bridge`, `data-bridge-action`, `data-bridge-otp`, `data-action`,
`data-observation-id`, `data-capability-amendment` identity/fingerprint/current-set,
`data-resulting-capabilities`, endpoint/credential forms and error selectors stay
unchanged. Required amendment confirmation, OTP clearing, request fields,
confirmation dialogs and success-only reload semantics are preserved.

Native probing still requires a disabled registration. The UI disables that
control on enabled registrations and explains the requirement. Reconnect is
offered only for enabled, native-verified registrations. An unfinished standalone
Bridge pairing points to its existing confirmation/recovery controls first;
a saved reconnect still links to its own recovery page. Neither is a live check
on page load or a lifecycle control.

The route reads existing saved reconnect stages and opaque outstanding cleanup
target IDs to expose recovery needs. GET performs no writes, probes, pairing,
captures, grant changes or replay. No request identity is reconstructed; the
existing reconnect page recovers its existing operation. An old retired
registration retains the original cleanup retry and audit endpoint. If an active
registration has an outstanding credential obligation, the page requests an
administrator review; it does not suggest retiring an active connection as a
routine cleanup step or create a new cleanup API.

## Focused and visual validation

`test_development_presentation.py` exercises 18 stored-state fixtures, read-only
GET/no-I/O behavior and capability-amendment authentication, CSRF, origin, strict
confirmation/fields and exact service handoff. Existing route, OTP, reconnect,
capability, screenshot, retirement and provenance tests retain service denials.

Set `KODI_DEVELOPMENT_UI_EVIDENCE` to export fixtures from that test file. The
`development_targets_browser.cjs` runner uses an existing Playwright/Axe install
via `KODI_LAB_BROWSER_TOOLS` and exact-parent fixtures/assets via
`KODI_DEVELOPMENT_UI_BEFORE`. All browser requests are intercepted, and every
explicit mutation receives synthetic 403. It checks 1440/375/320px, light/dark,
keyboard disclosures and menu focus, denied actions, exact OTP/amendment/retire
fields, required checkbox validation, Axe WCAG A/AA findings, overflow and script
errors. Portable before/after HTML/PNGs and JSON evidence stay outside Git.
Fixture results are not live external Kodi, deployment or native acceptance.
