# Viewer disconnect diagnostics

Viewer attempt UUIDs already identify one exact attachment in the browser, Web
relay and broker. They grant no access. Diagnostic records reuse that UUID and
the managed instance UUID; they never include tickets, owner hashes, cookies,
challenges, binding identities, sensitive headers, screenshots, RFB payloads,
exception messages or unrestricted helper stderr.

The browser keeps the last 64 lifecycle records in page memory. After a failure,
collect a snapshot in the ordinary developer console:

```javascript
document.querySelector('[data-novnc-viewer]').viewerDiagnostics
```

The snapshot contains native WebSocket open/error/close events, allowlisted
close codes and clean/unclean flags, RFB connect/disconnect events, and cleanup
requests. Each connection event has elapsed milliseconds from socket creation
and its original attempt UUID, including callbacks from retired attachments.
Snapshots cannot mutate the retained ring. Reloading or leaving the page loses
this history. There are no telemetry requests, new timers, constructor wrappers,
pixel reads, vendor modifications or automatic reconnects.

The installed Web and broker entrypoints each write JSON lines to
`/tmp/kodi-lab-viewer-*/events.jsonl` and its single rotated `.1` file, inside
their own process/container. The process creates one private mode-0700 directory;
each file is capped at 64 KiB (128 KiB total per process). Installed containers
already mount `/tmp` as ephemeral tmpfs. Collect both processes' files promptly,
before rotation or container removal/restart. These records do not propagate to
the ordinary process logs, whose external retention is outside this feature.
No database, runtime recipe, shared-volume or Docker logging change is involved.
If temporary storage cannot be opened, diagnostics are disabled without changing
viewer admission or cleanup. Embedders which bypass the installed entrypoints
must call `configure_diagnostics()` to enable this sink.

Server records have fixed event names, enum categories, validated UUIDs and
bounded numeric/boolean fields:

| Event | Evidence |
| --- | --- |
| `web_rejected` | Pre-attachment authorization/protocol refusal or ticket refusal; requested close code 4403. No untrusted ticket or attempt is recovered for correlation. |
| `web_accepted` | Broker admission and WebSocket acceptance completed; admission duration. |
| `web_ended` | Admission stage, accepted flag, terminal task outcomes, observed peer close code if received, requested cleanup close code, cleanup completion and elapsed duration. |
| `helper_rejected` | Validated request refused by admission or existing capacity limits. |
| `helper_started` | Existing Docker exec attach completed; startup duration. This does not assert RFB or graphics readiness. |
| `helper_ended` | Terminal category, arming state, existing native termination proof, wrapper exec exit code when available, aggregate byte counts, startup/teardown/total durations. |
| `binding_checked` | Only checks taking at least 250 ms, including slow successes. Fixed Web watchdog, broker lookup or broker validation origin; elapsed/worker/thread CPU/queue durations and allowlisted phase totals/counts. No lookup request contents or binding values. |

The Web terminal summary records each task completed when `FIRST_COMPLETED`
returns. Pending tasks are cancelled exactly as before and are labelled `pending`,
not treated as initiators. If multiple tasks completed together, their categories
are all retained without claiming causal order. Watchdog evidence distinguishes
authorization rejection, binding replacement and ownership authorization rejection;
exceptions retain only timeout/transport/internal-error categories. The server's
normal cleanup close request of 1000 is separate from an observed peer close
code and must not be used to infer a clean initiating disconnect.

The broker distinguishes helper EOF, private-envelope or Docker-stream rejection,
stderr presence (without its contents), arming rejection, backpressure, consumer
stall, lease expiry and revocation. Exit status is read from the existing bounded
`exec_inspect` teardown loop; no additional Docker request is made. It describes
the Python wrapper, not the individual X0tigervnc child. Native termination proof
does not assert durable ownership-release completion. No per-frame, per-packet,
per-watchdog-tick or periodic success records are emitted. A connection without
slow checks adds two Web records and two broker records.

These events would have separated the broad failure paths missing from Nexus
issue #214, if collected at the time. They cannot reconstruct that incident or
establish its cause now. RFB `connect`, arming and byte counts do **not** prove that
actual Kodi graphics were displayed. This remains a separate browser observation;
the diagnostic code deliberately does not inspect pixels or hook private noVNC
rendering functions. Helper-internal cause, lost records, simultaneous failures,
and transport errors without a peer close frame can still require further evidence.
The existing redundant cleanup call on an already disconnected RFB object is
preserved; a subsequent noVNC state warning is not evidence of the initiator.

The current heartbeat contract uses read-only `ownership_check`, replacing the
historical `ownership_renew` phase and its slow-commit records. Ordinary viewing
no longer emits `ownership_renewed` events or performs durable renewal. Historical
records retain their original meaning. See [viewer ownership](VIEWER-OWNERSHIP.md).

Slow binding summaries finish in the synchronous worker's `finally`, so a Web
timeout can precede its complete summary. The worker retains its capacity slot
until actual completion and cannot send a heartbeat after its caller has ended.
`check_queue` denotes a watchdog waiting for its worker to start. `queue_ms`
separates that delay from `worker_ms`; `worker_cpu_ms` and `phase_cpu_ms` measure
only the current thread. Phase elapsed times include scheduling, I/O and lock
waits. They do not identify a competing database writer. Web `broker_lookup`
includes socket/handshake/server work; the broker has its own process-local
summary. Lookup summaries have an instance UUID but no attempt UUID, since the
existing lookup protocol carries none. Timestamp overlap alone is not exact
request correlation. Nested phase durations are inclusive; do not add them.

Collection is bounded by the existing rotating sink, and successful fast checks
emit nothing. Failed writes or rotation may lose records; the private handler
suppresses Python's stderr fallback so storage exceptions and paths cannot escape.
No SQL statements/parameters, database busy handlers, process
environments, tokens, paths or framebuffer contents are traced. See
[binding-check analysis and limits](VIEWER-BINDING-CHECKS.md).
