# Reconnect an existing Kodi

Choose **Reconnect** on the Kodi card or its connection settings. Home and
external Development Kodi use the same guided page, with their existing class
and access settings retained. The saved address, native port, name and Kodi
sign-in are reused. A nonstandard Bridge port stays available in Advanced.

1. Choose **Reconnect**. Lab checks the saved native identity. If the existing
   pairing is healthy, it verifies that relationship without requesting a code.
2. If a new pairing is needed, review the exact Kodi/Bridge endpoints and HTTP
   disclosure on this page. Revocation invalidates old consent; the disclosure
   binds the next Bridge trust revision and current native policy.
3. Explicitly accept the disclosure and enter the current code shown in Kodi Lab
   Bridge. Compatible official builds use the existing provenance checks.
4. A confirmed result shows **Connected**. Agent grants and production scopes
   remain independently administered. Old revoked scopes are not restored by
   pairing again.

When a saved pairing needs replacement, the page requests explicit confirmation
before revoking its exact local credential. If Bridge still reports paired,
reset pairing locally in Kodi, then generate a fresh code. Lab never silently
resets remote pairing, registers a replacement UUID or changes a target's policy.

After a lost response, choose **Resume without sending a code**. Lab recovers
only the credential durably owned by that reconnect operation. It never adopts
another manual pairing or automatically resends a possibly consumed code. A
definitive wrong/expired-code rejection permits another explicit attempt; an
uncertain bootstrap without retained credentials requires cancellation and
Kodi-side recovery. Browser storage retains only an operation identifier scoped
to the current sign-in; credentials/codes are never persisted there.

Disabled targets require explicit enablement before reconnect. Permanently
retired registrations cannot reconnect. A changed native identity or access
policy requires review and a fresh operation. An unfinished original Add Kodi
operation must be resumed first. Completed original Home receipts remain in
history and cannot replay old consent over a newer reconnect.

Cancel reconnect explicitly to revoke a newly created credential owned by that
operation and retain the registration, policy and grants. A different current
pairing is never revoked by cancelling an older operation. Protected cleanup
failures preserve retryable progress. No external Kodi lifecycle, add-on, media
or profile deletion is part of reconnect.

## Administrative API

All routes require an authenticated Web session, same-origin request and CSRF.
No reconnect authority is added to MCP.

- `POST /api/external-reconnect/{target_id}` with `operation_id` and optional
  `bridge_port`. No code is accepted at this step. An existing active operation
  for that exact target is returned instead of allocating another transaction.
- `POST /api/external-reconnect/{target_id}/{operation_id}/resume` with `{}` to
  recover, or with `code` and the returned `transport_decision` to explicitly
  accept the exact disclosure and submit a code.
- `POST /api/external-reconnect/{target_id}/{operation_id}/replace-pairing` and
  `/cancel` require `{"confirmed": true}`.

Responses distinguish `connected`, `incomplete` and `requires_action`, with a
bounded code and explanation. Relevant codes include
`transport_disclosure_changed`, `pairing_recovery_required`,
`onboarding_binding_changed`, `connection_disabled`, `connection_retired`,
`saved_connection_incomplete` and `operation_in_progress`. Persistence errors
require resuming the same identifier before submitting another code.
