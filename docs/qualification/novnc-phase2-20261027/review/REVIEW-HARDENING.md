## Remaining findings

All source paths below are relative to `src/kodi_lab/`, except the migration.

### P1 — Successful native Stop still does not retire viewer ownership in the default application

**Locations:** `application/runtime_lifecycle.py:301–325`; `application/novnc_control.py:191–203`; `migrations/versions/0087_novnc_control.py:22–29,38–42`.

After validating the exact stopped-container receipt, `_stop()` calls only `retire_stopped_remote()`. The corresponding `HumanViewerOwnership.retire_stopped()` implementation exists, but has no caller in the reviewed source.

Consequently:

1. An interactive viewer leaves a claimed row after display-service loss or an unproven teardown.
2. Ordinary Web/MCP Stop successfully stops the exact container and removes its remote guard.
3. The interactive `novnc_sessions` row remains.
4. The triggers continue rejecting Start, other managed mutations, and existing remote dispatch.

With no viewer service running, repeated successful Stop does not recover this exclusion. This contradicts the stated default-application recovery boundary and makes existing workflows depend on restoring the experimental display service.

**Fix:** At the same verified-receipt boundary in `_stop()`, retire both remote and viewer ownership corresponding to the exact stopped container. This must be ordinary application/database coordination, independent of an injected viewer or display broker. Keep retirement inside the Stop dispatch flock and lifecycle lease.

### P2 — Input-reset proof execs remain untracked, independently executable cleanup workers

**Locations:** `runtime/novnc_qualification.py:81–114`; `broker/novnc.py:354–366`; `application/novnc_control.py:232–249`; `runtime/assembly_assets/novnc_view_helper.py:279–305`.

The new challenge protects the **display worker**, but `native_proof()` still starts `--probe-control` directly. That operation can mutate X input through `neutralize_input()`. Its exec ID is neither durably recorded nor retained when `exec_start()` times out.

A concrete problematic ordering is:

1. Cleanup starts probe A; Docker attachment/start times out with execution uncertain.
2. A later reaper attempt starts probe B, successfully proves neutral state, and deletes the controller fence.
3. Delayed probe A subsequently executes. It can acquire the native flock and reset input **after the durable ownership under which it was authorized has disappeared**.

The native flock excludes another X0 worker, but does not tie the reset to a current durable attempt or exclude existing HTTP/Bridge dispatch. A delayed probe can also contend with a newly admitted display worker. Replacement recovery has the related problem: it works from a row snapshot and performs reset without re-establishing current cleanup ownership immediately around that operation.

This is the same uncertain-native-execution class addressed by display-worker arming, remaining on the input-mutating proof path.

**Fix:** Treat control-reset execs as owned workers too. Track outstanding cleanup exec identities, require fresh attempt-bound authorization before they may reset input, and do not release the durable fence until every potentially executable reset worker is definitively finished or rendered incapable of acting. Coordinate stale/replacement proof callbacks against current durable ownership; a successful native flock alone is not sufficient authority.

## Corrections confirmed by source inspection

- Existing remote dispatch holds its kernel flock through dispatch and guard release; ordinary Stop holds the same nonblocking flock through its lifecycle request.
- Lifecycle history is flushed before lease reservation. The trigger exception requires the matching instance, operation ID, lifecycle kind, and a running typed Stop—not merely an arbitrary lifecycle lease.
- Pending cancellation uses conditional SQL rather than a stale ORM deletion.
- Browser payloads remain kind-1 envelopes. They cannot supply the trusted kind-4 arm response, and the broker strips the helper challenge from the browser stream.
- A failed acknowledgement before `run()` reaches proof/finalization or the separate retained-worker reaper.
- The retained-worker reaper is outside the sequential broker command loop.

I did not find a remaining direct browser-to-trusted-arm-envelope bypass or a live-dispatcher-versus-ordinary-Stop flock bypass in these paths.

## Human-only boundary

**The human-only API/capability boundary holds in the reviewed source:** no agent-facing noVNC tool, grant, credential, capture/input facility, or generic caller-selected VNC command was found. Viewer admission uses existing administrator Web authentication, CSRF, same-origin attachment, and one-use tickets. The normal application does not install viewer routes without explicit injection.

However, **the complete owner requirement is not yet satisfied**: the P1 recovery defect can leave existing agent workflows blocked until noVNC-specific recovery is restored.

## Review limitations

Read-only source review only; no execution, tests, or native acceptance. No FOCUSED, INTEGRATION, RELEASE, or native PASS is claimed. Docker delayed-exec behavior, X0’s inherited-flock retention and parent-death behavior, and held-key/button neutralization still require final native acceptance on the settled implementation.