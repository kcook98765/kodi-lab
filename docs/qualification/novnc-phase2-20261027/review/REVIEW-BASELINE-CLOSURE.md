## Result

**One remaining P2 regression found; no remaining P1 established.** The previous “none found” dispositions need this qualification.

I read all seven prior reviews and the supplied `origin/main` → candidate `46c439a` patch, inspecting the affected first-party source and relevant test source. This closes the missing baseline/prior-review reconciliation at the static-review level—not native acceptance or release sign-off.

### P2 — Definitively undispatched remote failures leave a durable input fence

**Locations:**  
- `src/kodi_lab/application/browser_remote.py:310–330, 354–355`
- `src/kodi_lab/application/browser_remote_coordination.py:147–156`
- `src/kodi_lab/kodi/remote.py:128–159, 229–234, 259–265`

The service sets `unresolved = True` before entering `adapter.dispatch()`. It clears that flag on success or `RemoteNoActivePlayerError`, but **not** on `RemoteKodiRejectedError`.

That exception includes outcomes where no mutating request was delivered:

- `connection.connect()` fails before `connection.request()`.
- A player action fails during the initial, read-only `Player.GetActivePlayers` query, before its mutating request is constructed.

Both paths nevertheless reach `release_browser_remote(..., unresolved=True)`, persisting an `ambiguous` guard. Once connectivity recovers, subsequent remote actions and ordinary managed mutations remain blocked until native Stop clears it.

This affects the **default application with no viewer enabled**. A transient, definitively pre-dispatch failure has become a durable operational lockout.

**Correction:** propagate whether mutating delivery was actually possible. Release the guard for provably undispatched failures; retain it for genuinely uncertain mutations. Do **not** simply clear every `RemoteKodiRejectedError`: that class also covers malformed responses after possible delivery. Add regression coverage for connection failure before request delivery and failure of the preliminary player query.

## Prior-finding reconciliation

References below are relative to `src/kodi_lab/`.

| Previous finding | Final-source disposition |
|---|---|
| **ARCHITECTURE #1:** stale pending cancellation deletes claimed ownership | **Corrected structurally.** `application/novnc_control.py:147–166` uses conditional deletion of unclaimed ownership and conditional revocation of claimed ownership. |
| **ARCHITECTURE #2/#4:** ambiguous remote releases its fence; abandoned guards lack recovery | **Original unsafe release corrected.** Uncertain dispatch retains durable exclusion, and exact native Stop supplies recovery. However, the new P2 above shows the uncertainty classification is too broad. |
| **ARCHITECTURE #3:** enabled login raises `NameError` | **Corrected.** `web/app.py:3247–3263` no longer references an undefined authenticated session. |
| **ARCHITECTURE #5; FOLLOWUP disconnect finding:** abandoned admissions and unconsumed tickets strand ownership/capacity | **Corrected structurally.** Qualification performs provisioning/image work before claim; admission failures have cleanup; pre-`run()` acknowledgement failure reaches finalization; retained tunnels have an independent reaper; expired unclaimed rows have periodic reconciliation (`runtime/novnc_qualification.py:116–190`; `broker/novnc.py:354–373, 435–455`). |
| **ARCHITECTURE #6/#7:** shared-event-loop blocking and database/process-lock inversion | **Original mechanisms corrected.** Viewer database/lock operations on the reviewed asynchronous paths are offloaded with bounded outstanding mutation/check capacity. Remote coordination no longer holds `_LOCK` during database waits. Sweeper failures are retried, although silently suppressed rather than made observable. |
| **FOLLOWUP lifecycle preemption finding** | **Corrected structurally.** The trigger exception requires a matching running typed Stop, not arbitrary lifecycle activity. Actual remote dispatch and ordinary Stop hold the same cross-process flock. |
| **FOLLOWUP uncertain exec startup finding** | **Corrected structurally.** Exact exec identity reaches proof, and a delayed helper cannot launch X0 without answering a fresh challenge whose response requires current broker authority (`broker/novnc.py:159–162, 252–264`; native helper `:97–122`). Native behavior remains unqualified here. |
| **FOLLOWUP Stop-recovery finding; HARDENING P1** | **Corrected.** Ordinary Stop validates the exact stopped receipt and retires both remote and viewer ownership without injected viewer wiring (`application/runtime_lifecycle.py:285–330`). |
| **HARDENING P2:** delayed input-mutating cleanup probes | **Corrected.** Separate control probes use `check_input(release=False)`. Input release occurs only in the original armed helper’s cleanup while it retains native exclusion. |
| **ADMISSION-ORDER review** | **Confirmed.** Lease insertion is flushed before viewer cancellation. The previously documented pre-commit rollback limitation remains; the Web Stop entry point still explicitly revokes before lifecycle admission. |
| **CAPTURE-INDEPENDENCE review** | **Confirmed structurally.** Experimental geometry comes from the validated catalog/artifact, not viewer state; capture remains X11, not RFB. Current test source additionally includes the exact sixteen-event boundary and unknown-event rejection, closing those two earlier coverage gaps. |

## Human-only / no-MCP VNC boundary

**Confirmed at the reviewed application-interface boundary.**

- Viewer mutations require existing administrator Web authentication and CSRF.
- WebSocket admission requires same origin and an owner-bound, single-use ticket.
- Browser RFB payloads cannot supply trusted helper-arm or renewal envelopes.
- No MCP VNC tool, grant, credential, capture facility, or input channel was found.
- Existing screenshots remain independent X11 capture.
- Ordinary Web and broker wiring remain default-disabled; experimental activation requires explicit injection.

The authorized isolated Firefox qualification is separate and does not imply general agent VNC authority.

## Static limits

No shell, edits, execution, network, external-directory access, or delegation occurred. Tests were inspected, **not run**; no FOCUSED, INTEGRATION, RELEASE, or native PASS is claimed.

I did not independently verify the supplied patch’s Git provenance, recompute vendor/recipe hashes, exhaustively audit vendored noVNC/pako, or establish deployed-image equivalence. Delayed Docker execution, TigerVNC inherited-flock/parent-death behavior, real held-input cleanup, Firefox memory, and physical-device/IME compatibility still require separate native evidence.

**Keep the feature default-disabled. No merge, deployment, or audio authorization is inferred.**