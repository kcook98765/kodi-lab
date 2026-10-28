**No concrete P1/P2 found in the focused correction.**

- `remote.py:132–163`: the undispatched subtype is raised only when TCP connection fails before `connection.request()`. Failures after that boundary remain ambiguous or generic rejected-response errors.
- `remote.py:264–275`: preliminary player-query failures become undispatched only around the read-only query. The mutating request is outside that exception handler, so its failures cannot accidentally receive this classification.
- `browser_remote.py:319–334`: only the undispatched subtype and the existing no-active-player outcome clear uncertainty. Generic malformed post-dispatch replies and `RemoteActionAmbiguousError` retain the durable fence.

The inspected tests cover pre-request connection failure, preliminary-query failures, and subtype-versus-generic fence handling. They were **not run**; no test PASS or native-validation claim.

This disposition is limited to the correction, not a renewed qualification of all prior safeguards. The human-only noVNC boundary, default-disabled posture, and prior native-validation limitations remain unchanged.