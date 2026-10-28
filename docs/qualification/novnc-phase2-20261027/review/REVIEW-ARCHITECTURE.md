## Verdict

**Draft review: changes needed before acceptance.** I found two P1 ownership/exclusion problems and five P2 lifecycle/availability problems.

The **human-only API/capability boundary holds in the reviewed source**: viewer routes require explicit injection, use Web administrator authentication/CSRF and same-origin WebSocket admission, and expose no MCP VNC tools, grants, credentials, capture, or input facility. Existing MCP screenshot/inspection paths remain separate. However, the coordination defects below prevent declaring the complete ownership/lifecycle requirement satisfied.

All source references below are under `src/kodi_lab/` unless explicitly identified as migrations. This was read-only source review, not execution or native acceptance.

## P1 findings

### 1. Pending cancellation can delete ownership after the broker has claimed it

**References:** `application/novnc_control.py:147–163`, `76–101`; `storage/engine.py:12–22`.

`revoke()` reads the row, decides whether it is unclaimed, then calls ORM `db.delete(row)`. The deletion is not conditional on `broker_id IS NULL`, the attempt, or the state still matching.

A possible interleaving is:

1. Web reads the pending row with no broker.
2. Broker atomically claims it and proceeds toward worker startup.
3. Web deletes the row using its stale ORM object.
4. Remote guards and managed mutation leases can now be admitted while the claimed worker has not been proven dead.

SQLite serializes **writes**, but that does not make this read/decision/delete sequence atomic. The configured engine does not explicitly begin an immediate transaction for this operation. The comment at lines 158–159 therefore overstates the protection.

**Fix:** make cancellation a database compare-and-swap operation: conditionally delete the exact attempt only while still unclaimed; otherwise conditionally transition the exact claimed attempt to `revoking`. Alternatively, acquire `BEGIN IMMEDIATE` before the read. Apply exact-attempt predicates to subsequent writes as well. A claimed row must never disappear through the pending-ticket cancellation path.

### 2. An ambiguous existing remote dispatch releases its fence without proving completion

**References:** `application/browser_remote.py:308–321,334–359`; `kodi/remote.py:119–161`.

The durable remote guard is released unconditionally in `finally`, including after `RemoteActionAmbiguousError`. The HTTP transport explicitly classifies loss of acknowledgement after dispatch as ambiguous; closing that connection does not cancel work already received by Kodi.

Consequently, an input request can remain pending at Kodi after the guard is deleted. Take Control can then insert its interactive row and start a human worker before that earlier request executes.

The durable guard closes the ordinary admission race, but not the **uncertain-dispatch handoff**.

**Fix:** represent unresolved dispatch separately from completed dispatch. Retain exclusion after an ambiguous mutation until there is a credible completion/drain boundary or exact-runtime retirement proof. Do not retry the input. Pair this with the recoverable guard lifecycle in finding 4; simply retaining an unrecoverable row is not sufficient.

## P2 findings

### 3. Successful login fails whenever the prototype is enabled

**Reference:** `web/app.py:3238–3265`, particularly line 3253.

The login handler references `authenticated.csrf_token`, but does not define `authenticated`. On the enabled prototype, successful password verification reaches this reference before setting the new session cookie, causing a `NameError`.

It also creates a new authentication session before this failure.

**Fix:** resolve the previous cookie explicitly, retire its viewer owner only if that previous session exists, then perform the intended authentication-session replacement. Fresh login must work without an existing session.

### 4. Durable remote guards have no crash-recovery lifecycle

**References:** `application/browser_remote_coordination.py:37–82`; `storage/models.py:8593–8602`; `migrations/versions/0087_novnc_control.py:24–50,82–91`.

A guard contains only an instance and invocation UUID. It has no process/service incarnation, state, heartbeat, or reconciliation metadata. Its only application deletion path is the originating invocation’s normal `release_browser_remote()`.

If Web dies after inserting a guard, subsequent processes cannot release it through that API: `_ACTIVE` is empty and the old handle no longer exists. The row indefinitely blocks subsequent Web/MCP remote actions, managed mutations, and interactive noVNC admission.

This regression also affects the **normal application with no viewer injected**, because existing remote dispatch now always uses the durable guard.

**Fix:** give remote guards durable ownership and unresolved-dispatch state, plus explicit startup/operator reconciliation. Recovery must distinguish “owner died before dispatch” from “Kodi may still execute the request”; neither unconditional deletion nor expiry alone is adequate.

### 5. Several noVNC orphan paths require broker restart rather than ordinary recovery

**References:** `runtime/novnc_qualification.py:121–132,156–168`; `broker/novnc.py:375–393`; `application/novnc_control.py:197–223`; `web/novnc.py:265–274`.

Two concrete cases:

- **Failure after claim, before tunnel construction:** `ownership.claim()` commits before Docker client construction, provisioning resolution, and image inspection. If a later operation fails, `DisplayLane` has no tunnel and therefore no `finished` callback. The claimed row remains; Web cancellation can mark it revoking, but cannot prove and release it.
- **Web loss with an unconsumed ticket:** pending rows are normally removed by the process-local attachment sweeper. After Web restarts, those attachments are gone. Expired pending rows are considered only by broker startup recovery, so a broker that remains alive never reaps them. An unconsumed interactive handoff ticket can therefore retain the input fence indefinitely.

Recovery also skips rows belonging to the current broker incarnation, so it does not repair that broker’s abandoned admissions.

**Fix:** wrap post-claim admission in ownership-aware cleanup, closing allocated clients and obtaining appropriate proof before finishing claimed ownership. Add bounded reconciliation for expired, provably unclaimed tickets and abandoned current-incarnation admissions. Keep claimed fences until proof; provide a supported recovery path rather than requiring direct database intervention.

### 6. Viewer cancellation and expiry can block the shared Web event loop

**References:** `web/app.py:2323–2328`; `web/novnc.py:265–274,307–316,483–495,561–572`.

The asynchronous ticket sweeper takes a synchronous `threading.Lock` and calls synchronous database revocation. Relay teardown likewise calls `viewer.end()` directly on the event loop. Cancellation handlers call synchronous `viewer.stop()` there too.

Meanwhile, `_start()` holds that same lock across `ownership.create()` and its database commit (`web/novnc.py:225–245`). An admission thread blocked on SQLite can therefore make the sweeper block the entire event loop acquiring the lock. Other paths can block directly on database access.

The separate broker display lane does not protect shared Web/MCP/read-only availability from this bottleneck. An unhandled sweeper database exception also terminates the expiry task.

**Fix:** move these operations to a bounded worker facility, avoid synchronous lock acquisition on the event loop, shorten lock scope around database work, and make expiry failures observable and retryable. Preserve revocation authority while isolating waits from unrelated requests.

### 7. Durable remote coordination introduces a lock-order inversion

**References:** `application/browser_remote_coordination.py:41–58,67–81`; `storage/repositories.py:6575–6584`; `application/lifecycle_operations.py:119–128`.

Remote acquisition/release holds `_LOCK` while waiting for SQLite writes. Managed lifecycle admission first obtains SQLite’s writer lock using `BEGIN IMMEDIATE`, then calls `require_no_active_browser_remote()`, which waits for `_LOCK`.

Thus:

- a remote thread can hold `_LOCK` while waiting for SQLite;
- a lifecycle thread can hold SQLite’s writer lock while waiting for `_LOCK`.

Ordinary overlapping requests can deadlock until a database timeout, delaying remote completion and lifecycle control and potentially leaving coordination cleanup unsuccessful.

**Fix:** establish one lock order. Prefer making the durable database guard/triggers authoritative and removing the process-local lock dependency from mutation reservation. Never hold `_LOCK` across a potentially blocking database operation.

## What appears sound, and what remains unproven

- **Ordinary admission arbitration:** the migration’s reciprocal insert triggers correctly make interactive ownership, remote guards, and managed mutation leases contend through SQLite. Expiry is deliberately absent from the interactive exclusion predicate, so lease expiry alone does not release claimed input ownership.
- **Mode handoff:** the intended sequence cancels the previous attachment, waits for durable row disappearance, carries the absolute deadline, and issues a new one-use ticket (`web/novnc.py:331–357`). Its safety depends on fixing finding 1 and preserving broker-only proof-based deletion.
- **Transport/capabilities:** the helper fixes a private Unix RFB socket, disables VNC TCP, clipboard and resizing, and enables input only through the selected interactive flags. The display listener has separate bounded workers and bounded buffers; it does not run RFB forwarding in sequential broker dispatch.
- **Read-only access:** I found no noVNC permission requirement added to independent MCP screenshots, inspection, or logs. The orphan-guard and shared-event-loop failures nevertheless threaten the intended operational independence.
- **Held keys/buttons:** helper teardown proves process termination, not explicitly neutral X11 input state. The source contains no server-side release/reset proof before deleting ownership. Browser keyboard cleanup cannot establish safety for browser loss, broker loss, or helper SIGKILL. This is an **unresolved native acceptance limitation**, not a claim that TigerVNC definitely leaves keys held. Verify the exact X0 build’s key/button cleanup for graceful and forced termination, including queued key-down input.
- **Native revocation proof:** passing the flock descriptor and setting a parent-death signal are useful defenses. This review does not establish that the installed X0 process retains that descriptor, that its termination semantics meet the requirement, or that watchdog/process probing works under all Docker/service-loss conditions. Exact stored-container recovery is preferable to probing a replacement; unavailable proof must remain fail-closed with an explicit recovery procedure.

**Validation status:** source review only. No tests, shell commands, network access, delegation, or native execution were performed. No FOCUSED, INTEGRATION, RELEASE, or native PASS is claimed.