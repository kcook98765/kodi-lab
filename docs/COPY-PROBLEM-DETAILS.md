# Copy problem details

Managed Kodi offers **Copy problem details** beside an actionable request error,
saved request that needs continuation or recovery, or a version's existing
recovery link. Completed requests and ordinary successful status have no copy
control. A saved request's summary takes precedence over its version's recovery
card and an earlier request error, avoiding duplicate copy controls. Copying does not send or resume a Kodi operation or refresh status.

The report is plain text. For example:

```text
Kodi Lab problem report
Area: Managed Kodi
Kodi version: Omega
Action: Reset Kodi
Result: Request requires recovery
Reference: aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa
Next step: Review recovery
```

Labels and order are fixed: title, Area, optional Kodi version, optional Action,
Result, optional Code, optional Reference, Next step. Unknown metadata is omitted.
Application version and timestamps are omitted because this presentation does not
already supply them. A missing request receipt produces “Request outcome is not
yet known”; it does not assert that an operation failed or authorize a new one.

The formatter uses fixed mappings for the four known Kodi versions, Start/Stop/
Reset, result categories and next steps. Codes must exactly match a small set
from the existing managed presenter. References are existing original request
UUIDs, already permitted in Advanced request details, validated for exact length,
UUID shape, version and variant. Cards without a request report their known Kodi
version and the existing need for recovery without guessing an action or cause.

No message text, DOM text, raw response, receipt JSON, nested technical evidence,
URL, browser diagnostics, log, screenshot or path is copied. In particular, a
server field being visible elsewhere does not make it safe to copy. This follows
the existing diagnostic convention of fixed categories and validated references;
it does not change server redaction or collect new diagnostics.

The button attempts the browser Clipboard API only after the user activates it.
Success is announced only after that operation resolves. HTTPS and localhost HTTP
support the API in compatible browsers. If it is unavailable or permission is
denied, the same sanitized report appears in a labelled, read-only text area,
selected and focused, with keyboard/device and phone copy instructions. Unchanged
polls preserve the report, selection and feedback. A changed report invalidates
pending clipboard feedback and clears old manual text. The new report can be
copied immediately even if an older clipboard permission prompt is still pending.

This convenience has no backend, storage, network, logging, download or export
behavior. Original request replay, Reset consent, uncertain-operation fences and
recovery eligibility remain with the existing controller.

Viewer support is deferred. The current viewer has safe fixed messages, but
connection setup, established disconnects, authority review, control handoff and
asynchronous cleanup can replace them at different times. Copy integration needs
an explicit review of those paths and their stale-callback fences; diagnostic
arrays, WebSocket events and session tickets must never become report inputs.

Focused tests cover formatter allowlisting, hostile and missing metadata,
secret-bearing receipts, Start/Stop failures, uncertain Reset, pending and missing
original receipts, success suppression, clipboard outcomes, manual fallback,
polling/clipboard races and no extra API calls or storage writes. The optional
Chromium harness `tests/web/managed_problem_details_browser.cjs` uses the existing
`KODI_LAB_BROWSER_TOOLS` and `KODI_MANAGED_CONTROLS_EVIDENCE` fixture export, as does
the existing managed-card browser harness. All browser requests are intercepted;
these checks do not establish native Kodi or installed application acceptance.
