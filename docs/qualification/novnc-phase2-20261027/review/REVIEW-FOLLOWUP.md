## Result

**Remaining findings: two P1s and two P2s.** This is a read-only source review, not acceptance. I used only read/glob/grep; no tests, execution, or native validation were performed.

Paths below are relative to `src/kodi_lab/`, except the migration.

### P1 — Lifecycle preemption is broader than Stop recovery and does not fence a live cross-process dispatcher

**Sources:**  
- `migrations/versions/0087_novnc_control.py:24–37`
- `application/browser_remote_coordination.py:68–83,116–139`
- `application/browser_remote.py:288–315`
- `application/lifecycle_operations.py:119–128`
- `application/runtime_lifecycle.py:204–218,290–314`

The trigger exception admits **every `operation_kind='lifecycle'` lease** over an existing remote guard. That includes Start and reviewed-Bridge operations, not just recovery Stop. `_ACTIVE` prevents this only within the dispatcher’s own process.

There is also a remaining Stop/restart race:

1. Process A checks that no mutation lease exists and marks its guard `dispatching`.
2. A pauses immediately before `adapter.dispatch()`.
3. Process B acquires a lifecycle lease, stops the exact container, and deletes A’s guard using the stopped receipt.
4. After lifecycle completion, the container can restart and new input ownership can be admitted.
5. A resumes and sends its already-prepared HTTP action. The adapter does not validate the guard or a runtime generation at actual delivery.

An exact-container Stop receipt proves that the old native runtime stopped; it does **not** prove that another live process can no longer initiate its previously authorized request. The process-local check does not supply that cross-process proof.

**Fix:** Restrict preemption to an explicitly identified recovery Stop, rather than the entire lifecycle category. Additionally, place actual dispatch and Stop/restart retirement behind a cross-process dispatch fence, or a generation-bound dispatch authority enforced at delivery. Do not retire a live dispatcher’s authority solely because its destination was temporarily stopped.

### P1 — Unknown Docker exec startup can outlive the proof that releases ownership

**Sources:**  
- `broker/novnc.py:151–154,268–282,338–343`
- `runtime/novnc_qualification.py:81–110,173–181`
- `runtime/assembly_assets/novnc_view_helper.py:262–288`

The normal tunnel proof callback calls:

```python
native_proof(selected.container, interactive=...)
```

It does **not** pass the tunnel’s recorded exec ID. That proof can consequently succeed based on currently available flock/process absence without checking the exact exec.

This matters when `exec_start()` has an uncertain outcome—for example, the Docker request times out while startup is still pending. The finalizer can see the exec as not running, or the independent reaper can run before its native process appears. The probe then sees no worker, succeeds, and ownership is deleted. The outstanding startup can subsequently launch the interactive helper.

The helper’s flock protects against an already-existing helper; it does not revoke an old exec’s permission to start after the probe releases the flock. A fresh helper also begins with its own three-second initial lease, without requiring a first broker renewal.

**Fix:** Make uncertain exec startup an explicitly retained state. Proof must cover the exact exec **and outstanding startup**, not just instantaneous process absence. Pass the exec identity through normal teardown/reaping, and require a durable native start-revocation/generation mechanism—or exact-container Stop when startup cannot be conclusively retired. Repeated “not running/no process now” observations are not equivalent to preventing a delayed start.

### P2 — Disconnect between claim and `run()` creates an ownership reservation that the reaper never visits

**Sources:**  
- `broker/novnc.py:404–409,413–430`
- `runtime/novnc_qualification.py:127–134,168–181`
- `application/novnc_control.py:177–189`

After admission claims durable ownership, `_worker()` sends `OK\n` before entering `tunnel.run()`.

If that send fails because Web has disconnected:

- `run()` never executes its cleanup;
- `terminated` and `done` remain false;
- the lane does not discard the instance;
- the tunnel is **not** added to `retained`, because that requires `done.is_set()`;
- `finished(False)` leaves a durable blocked row.

There is no helper to terminate, but the current broker has permanently stranded both ownership and lane capacity. For an interactive handoff, existing remote/mutation admission remains blocked.

**Fix:** Give the entire post-claim path one cleanup owner, including the acknowledgement. Every admitted tunnel must either receive successful native proof and release, or enter the independent reaper—even if `run()` was never entered. Preserve the fence until that proof, rather than merely marking this case blocked.

### P2 — Unproven noVNC ownership prevents the native Stop needed to recover it

**Sources:**  
- `web/app.py:8278–8289`
- `web/novnc.py:371–375`
- `application/novnc_control.py:202–207`
- `runtime/novnc_qualification.py:86–90,116`
- `migrations/versions/0087_novnc_control.py:24–37`

The Web Stop endpoint waits for noVNC ownership deletion **before** dispatching native Stop. Meanwhile, interactive rows block lifecycle leases without exception.

For example, a paused container cannot run its helper watchdog or cleanup probe; `native_proof()` explicitly refuses paused containers. Thus Stop returns `input_release_unproven` instead of stopping the container—the very operation that could establish safe termination.

There is a related normal-app recovery limitation: switching back to the default launcher removes viewer/recovery wiring, but does not remove durable interactive rows. Those rows still block remote and lifecycle admission through the migration’s triggers.

**Fix:** Provide a narrowly authorized native Stop recovery path that retains the input fence while stopping the exact stored container, then retires ownership only on the exact stopped-container receipt. This recovery must remain available without the viewer service. Coordinate this with the live-dispatch fencing correction above; broadly bypassing all guards is not sufficient.

## Human-only boundary

**The human-only noVNC interface boundary holds in the reviewed source:** admission is through authenticated Web sessions, CSRF-protected mutations, same-origin WebSocket checks, one-use tickets, and the trusted Web-to-broker lane. I found no MCP noVNC tool, grant, credential, agent capture/input path, or generic caller-selected VNC command facility. Default `create_app()` does not install viewer routes unless explicitly injected (`web/app.py:1421,7144–7147`).

However, **the full owner requirement is not yet satisfied**: the stranded/recovery cases above can make existing remote and managed workflows depend on recovering noVNC ownership.

## Corrections that look sound at source level

- Pending cancellation now uses conditional SQL rather than stale ORM deletion (`application/novnc_control.py:147–166`).
- Remote coordination no longer holds `_LOCK` across database waits.
- Claimed ownership is not released merely because its lease expires.
- The separate display lane, bounded buffers, watchdogs, and fixed helper flags preserve useful isolation from ordinary broker dispatch.
- Interactive cleanup attempts held-key/button neutralization while the durable input fence remains; view-only probes do not perform that reset.
- I found no new direct noVNC admission requirement on independent screenshots, inspection, or logs.

**Native acceptance remains pending**, particularly for delayed Docker exec startup, inherited-flock behavior, parent-death handling, and XTest neutral-state proof. No FOCUSED, INTEGRATION, RELEASE, or native PASS is claimed.