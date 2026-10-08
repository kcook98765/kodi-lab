# Production Kodi operator guide

## Authority boundary

Kodi Lab V1 manages registration, trust, least-authority presets/grants/scopes,
semantic inspection, screenshots and closed semantic remote actions. It does
not install or update production software, restart Kodi or its host, use SSH,
manage files, issue raw JSON-RPC, deploy user workspaces to production or own
production/host lifecycle.

The operator obtains, verifies, installs, upgrades, rolls back and restarts the
production Kodi bridge through ordinary operator/Kodi procedures. Kodi Lab then
proves and records which exact reviewed package is present. Operator action is
not evidence of Kodi Lab trust, and Kodi Lab trust is not installation
authority.

The bridge supports identity, capability proof and bounded screenshots. It is
not an arbitrary control channel. Production control uses fixed server-owned
native semantic calls. Bridge mismatch suspends bridge-dependent screenshot and
control authority; native read-only inspection remains independent when its own
production identity/generation remains coherent.

## Required production artifact

A candidate built from this source contains `bridge/service.kodi_lab_bridge-1.1.15.zip`
and `bridge/BRIDGE.json`. Verify the full source-free bundle and compare the
exact ZIP hash with both provenance and `BUNDLE.json`. A hosted Kodi repository
and automatic updater are
not required or authorized.

Do not use a source tree, locally rebuilt archive or filename/version alone.
Obtain the exact reviewed ZIP and compare its complete lowercase
SHA-256 with the value in its reviewed provenance. The reviewed record must
also identify add-on ID/version, protocol, semantic fingerprint, capabilities,
supported Kodi versions and source reference.

Bridge 1.1.7 and 1.1.8 are historical review evidence. Source bridge 1.1.15
artifact uses `otp-1`; its exact hash and source identity are in the local
candidate's `bridge/BRIDGE.json`. Neither this document nor a filename alone
approves any other ZIP bytes.

## Manual production bridge installation and onboarding

Keep production mutations globally blocked and the target mutation suspension
set while onboarding.
Bridge 1.1.15 uses the shared `otp-1` protocol. The bridge displays a temporary
four-character code; Kodi Lab creates a separate protected 32-byte credential.
The candidate's reviewed 1.1.15 ZIP is the pairing artifact after transition.

1. Obtain the exact bridge ZIP and provenance from the verified local bundle.
2. Compute SHA-256 locally and compare the entire value with the recorded
   checksum. Stop on any mismatch.
3. As the operator, manually install the ZIP through the normal local Kodi
   add-on procedure. This is not a Kodi Lab operation. Do not present SSH as the
   normal workflow.
4. In Kodi Lab Web administration, register the native production target with
   its literal private address, structured HTTP/HTTPS mode, port and protected
   credential.
5. Run native verification, inspect the bounded observed identity and explicitly
   confirm it.
6. Explicitly designate the target as production, then enable it read-only.
7. Register the exact reviewed bridge build in Web administration using its ZIP
   SHA-256, semantic fingerprint, add-on/protocol versions, capabilities,
   supported Kodi versions and source provenance.
8. For private HTTP, acknowledge the exact native/bridge address and bridge
   port risk in Web before pairing.
9. Enable the bridge on Kodi and obtain its four-character code. Enter the code
   and allowlisted bridge port in the production target's Web pairing form.
   Do not record the code in evidence.
10. Kodi Lab stages an authenticated provisional identity using `otp-1`.
    Refresh the page if the response is lost; a pending pairing can be resumed
    without entering another code.
11. Inspect the observed bridge UUID/build/protocol/Kodi identity and explicitly
    confirm installed production provenance. Pairing alone grants no scope.
12. Assign only the intended exact built-in preset, generic target grant and
    current revision-bound scopes. Leave both mutation vetoes set unless the
    exact validation or operator task deliberately needs control.

For private HTTP, record the exact Web risk acknowledgements required for
screenshot and native control configurations. HMAC authenticates bridge
messages but does not encrypt screenshot pixels; native HTTP control has no
bridge HMAC protection. Prefer normally validated HTTPS when available.

## Least-authority onboarding checklist

- Native registration is disabled/unverified initially.
- Native identity is observed and explicitly confirmed.
- Production designation is separately confirmed.
- Read-only enablement does not grant MCP authority.
- `Production Inspection` plus a generic grant and current `inspect` scope is
  the narrow starting point.
- Install/pair the reviewed bridge when associated container/selected-item UI
  observations, screenshots or production control are needed. Native UI
  observation fallback remains available without it.
- `Production Visual Inspection` adds exactly inspect plus visual authority.
- `Production Remote Kodi` is only a maximum; grant it only when remote control
  is intended.
- Add `navigate` only for the seven standing navigation actions.
- Add `request_transient_control` only when one-shot playback/volume requests
  are intended.
- Record the applicable HTTP screenshot/native-control acknowledgement when
  using HTTP; prefer HTTPS.
- Keep the global mutation block and per-target suspension set by default.
  Clearing either veto grants nothing by itself.

Do not recommend the maximum preset by default. Inspection, visual inspection
and remote control are separate intersections, not a progression every target
must receive.

## Manual upgrade

There is no automatic updater.

1. Independently review and Web-register the exact new build before use.
2. Block global production mutations.
3. Suspend target mutation/control authority.
4. As the operator, verify the new ZIP checksum and manually install it through
   ordinary Kodi procedures.
5. Kodi Lab must observe drift and keep bridge-dependent authority inert. Do
   not edit stored identity to make it match.
6. Explicitly reverify native/bridge identity and confirm exact new provenance
   in Web administration.
7. Re-pair with a fresh bridge-generated four-character code if bridge UUID or secret continuity
   changed.
8. Revoke stale authority and issue fresh scopes bound to current native
   generation and bridge revision.
9. Restore only the intended preset/grant/scopes; clear mutation vetoes only
   for a deliberate bounded action.

On failed upgrade, keep mutations blocked and control suspended. The operator
may manually reinstall the exact prior reviewed ZIP after verifying its
checksum. Treat rollback or reinstall as drift: reverify, re-pair if identity
or secret continuity changed, confirm prior provenance again, and issue fresh
revision-bound scopes. Never restore authority by lowering a generation or
revision, editing evidence, or retrying an ambiguous mutation.

## Transient approval behavior

Standing actions needing no one-shot approval are `up`, `down`, `left`,
`right`, `back`, `home` and `info`. Approval-required actions are
`play_pause`, `stop`, `previous`, `next`, `volume_up`, `volume_down` and
`mute_toggle`. `context_menu` and `select` are always prohibited.

A new transient `remote_action` request creates one pending exact intent after
current authority and veto checks. Pending lifetime is at most fifteen minutes.
Web approval is one-shot and dispatchable for at most five minutes from
approval; neither deadline extends. MCP cannot approve its own request and
receives no bearer approval token.

After Web approval, retry the same client request UUID with the exact same
target and action. Consumption and receipt claim are atomic before at most one
native send. Replaying the same request returns history and cannot send again.
A changed request conflicts. Crash or uncertain response after claim is a stop
condition: do not invent a new request UUID to “try again.”

## Screenshot and privacy

Production screenshots require the exact reviewed/paired bridge, current visual
scope and the complete native/bridge authority intersection. Visual scope is
exactly `target.visual` beside current inspect authority; it grants no control.

Kodi Lab does not durably retain production screenshot pixels, base64,
thumbnail, preview or raw bridge response. It retains only bounded metadata
such as UUIDs, generations/revisions, dimensions, encoded size, digest, outcome
and timing. The receiving MCP client may display, log, cache or otherwise
persist pixels outside Kodi Lab; its privacy policy is a separate boundary.

HTTP HMAC supplies integrity/authentication, not confidentiality, so LAN
observers may see PNG bytes. Prefer certificate-validated HTTPS. For validation
Stage B, put Kodi on a benign, non-sensitive screen before taking the single
authorized screenshot.

## Recovery and troubleshooting

| Condition | Kodi Lab behavior | Operator response |
|---|---|---|
| Native reachable, bridge missing | Native inspection may remain available; visual/control authority is unavailable. | Install the exact reviewed ZIP manually if those capabilities are intended, then pair and confirm provenance. |
| Bridge unreachable | Bridge-dependent work is denied; native inspection remains independent. | Check Kodi/add-on/network state under operator authority. Do not bypass trust or automatically retry a mutation. |
| Bridge identity mismatch | Bridge authority is suspended and its revision advances as designed. | Stop, inspect the installed package/identity, then explicitly reverify or reinstall/re-pair. |
| Unreviewed build | Pairing/provenance confirmation cannot authorize it. | Remove or retain it under operator control; install an exact registered reviewed build. |
| Bridge UUID reset | Prior secret and revision-bound authority are unusable. | Generate a fresh four-character code on the bridge, re-pair, confirm provenance and issue fresh scopes. |
| Native generation changed | Old native and dependent scopes remain stale. | Reverify native identity, reconfirm production designation, then issue fresh current-generation scopes. |
| Target reinstalled | Treat identity/build continuity as unproved. | Keep mutations blocked; redo native verification/designation and bridge pairing/provenance as applicable. |
| HTTP acknowledgement stale | Exact configuration-bound visual/control authority is denied. | Review the current HTTP risk in Web and create a new exact acknowledgement, or migrate to validated HTTPS. |
| Mutation blocked or suspended | No production mutation dispatch occurs. | Leave blocked unless a deliberate bounded task requires control; clearing a veto creates no grant. |
| Approval expired | The old request cannot dispatch. | Reassess intent and authority; only then create a genuinely new request. Never relabel an ambiguous claimed request. |
| Ambiguous action receipt | Receipt identity remains protected and no redispatch occurs. | Stop and reconcile evidence. Do not retry with a new UUID. |
| Failed upgrade/rollback | Bridge-dependent authority remains inert. | Manually install an exact reviewed package, reverify/re-pair and issue fresh scopes; do not downgrade revisions. |
| Kodi restart needed | Kodi Lab has no production lifecycle authority. | The operator restarts Kodi/host through normal operational procedures, then rechecks trust and reachability. |

Unknown, contradictory or incomplete evidence fails closed. Do not recover by
overwriting reviewed identity, lowering revisions, deleting receipts, repeating
an uncertain mutation, or adopting an unreviewed package.
