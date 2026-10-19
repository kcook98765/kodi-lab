# Add or reconnect Kodi

For a new everyday Kodi, open **Add Kodi**, enter its private LAN address and
current Bridge pairing code, then choose **Connect**. A friendly name is optional;
Lab defaults to `Kodi ADDRESS`. Kodi HTTP port 8080 and Bridge port 8765 are the
defaults. Open **Advanced connection details** for nonstandard ports. Enable the
Kodi sign-in fields only when its web interface requires credentials.

The Home flow checks the target and displays the exact HTTP disclosure on the
same page. Accept that disclosure explicitly and choose **Accept and continue**.
Connecting establishes read-only onboarding; agent grants remain separate.
Development is a separate explicit mode and keeps its existing permissions.

## Existing or revoked pairings

If **Add Kodi** finds one existing registration at the exact address and Kodi
port, it links to that registration instead of creating another. If an unfinished
Home Connect operation owns that endpoint, **Resume saved connection** opens its
saved progress without resending a code or accepting HTTP risks. Review the
current disclosure and enter a current code only when requested. Shared or
ambiguous registrations require administrator review; Lab does not choose by
friendly name.

An existing completed/revoked pairing still uses the advanced steps below; the
full guided reconnect flow remains tracked in issue #10. For an obsolete
registration, use [Retire connection](EXTERNAL-CONNECTION-RETIREMENT.md) and its
history view rather than deleting records or creating grants for a replacement.

An existing registered Kodi must not be registered again just to recover its
pairing. Full existing-device reconnect through the household Add Kodi page is
still tracked in [issue #10](https://github.com/kcook98765/kodi-lab/issues/10).
Until that workflow is available, use the existing device's controls in
**Advanced Kodi administration → Production Kodi targets**:

1. Check the retained name, address, native identity and Bridge port. A previously
   recorded Bridge port is selected automatically, including nonstandard ports
   still allowed by policy. Endpoint or identity changes require their ordinary
   verification; reconnect does not adopt a replacement device.
2. After revocation, choose **Acknowledge exact HTTP transport risk** for the
   current connection. Revocation invalidated the old acknowledgement; do not
   reuse it or bypass consent. The UI explains this prerequisite, and pairing
   errors identify the next action instead of only saying the request failed.
3. In Kodi, generate a current code. Reset pairing locally only when Kodi/Bridge
   actually reports an existing or uncertain transaction that requires it.
4. Submit the code once, check the provisional identity and reviewed package,
   then confirm. Official compatible builds use the existing reviewed-build
   registration; custom builds retain their separate review.

If acknowledgement is missing, the response says the code was not sent. If the
outcome is uncertain after dispatch, do not resubmit that code automatically.
Refresh and use the saved progress/confirmation controls first. Technical error
codes appear in **Advanced error details**; do not paste pairing codes or
credentials into reports.

For interrupted Home onboarding, use **Resume connection** or its saved operation
identifier. Resume without a code checks saved progress; it does not resend the
previous OTP. Codes and native passwords are not stored in browser storage.
Connecting/reconnecting does not grant an MCP client access, authorize control,
clear mutation suspension or discard trust history.

Repository address changes are a separate concern. If Kodi cannot download the
Bridge or refresh its repository, use
[repository address diagnosis](REPOSITORY-ADDRESS-RECOVERY.md) first; re-pairing
does not repair stale repository URLs.
