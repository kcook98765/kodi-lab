# Retire an external Kodi connection

Use **Retire connection** on **External development targets** or **Production
Kodi targets** when a registration is obsolete. Check the displayed name, full
registration UUID and status in the confirmation, particularly when two cards
have the same name or address. Only that UUID is retired.

**Disable** is reversible and keeps the connection in current lists. Retirement
uses the existing permanent `deactivated` state: it removes effective agent
grants and policy assignments, retires Lab-held Kodi and Bridge credentials,
and hides the registration from current lists and grant selectors. It cannot be
enabled or paired again. Its UUID, observations, policy and audit history remain.

Retirement affects the Lab registration. It does not contact or stop external
Kodi, uninstall Bridge, reset remote pairing, or delete Kodi's profile, add-ons
or media. A replacement registration has a different UUID and receives no grants
from the old registration. If Bridge still holds the old pairing, an explicit
Kodi-side pairing reset may be needed before connecting the replacement.

## History and interrupted cleanup

Choose **Connection history (including retired)** to inspect retained records.
Previously deactivated registrations already appear there, including records
created before this feature. **Retry credential cleanup** retries the exact
registration's pending protected-secret deletion without re-enabling it.

Native deactivation closes access and new pairing publication before Bridge
revocation. A secret-deletion failure does not restore access. The response
reports failure and the retired card remains available in history for retry.
For Home connections, the saved onboarding endpoint reservation is released
only after cleanup and coordinator locking succeed. The original onboarding
record remains in history and can no longer replay a Connected result.

If Connect is still in flight, wait for it to finish, then retry cleanup. Do not
delete database rows or credential files manually to bypass the reservation.

## Administrative API

The authenticated Web API accepts either of these exact-ID operations:

```http
POST /api/external-targets/{target_id}/retire
POST /api/production-targets/{target_id}/retire
Content-Type: application/json
X-Kodi-Lab-CSRF: <current Web session CSRF token>

{"confirmed": true}
```

Use the matching target class, a same-origin request, and the Web session cookie.
MCP bearer credentials do not authorize this operation. Existing `/deactivate`
routes remain aliases with the same retirement and cleanup behavior. Successful
retries are idempotent and preserve the original deactivation timestamp.

- `GET /api/external-targets` and `GET /api/production-targets` list current
  registrations. Add `?include_retired=true` to include retained history.
- The corresponding Web pages accept the same history parameter.
- `GET /api/{external-targets|production-targets}/{target_id}` and `/events`
  retain exact-ID metadata and audit access.
- Missing or wrong-class UUIDs return `404 target_not_found` before changing
  any authority. Missing Web/CSRF authority returns `401` or `403`; omitted or
  false confirmation returns `422`.
- `503 bridge_secret_retirement_failed` requests a protected-cleanup retry.
  `503 connection_secret_retirement_failed` requests native credential cleanup.
  `409 connection_retirement_incomplete` means onboarding cleanup still needs
  to finish. Neither response means that old authority became active again.
- Trying to enable a retired connection returns `409 verification_required`.

See [Add or reconnect Kodi](KODI-RECONNECT.md) for a current connection and
[production administration](PHASE14-PRODUCTION-TARGETS.md) for grant boundaries.
