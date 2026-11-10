# Managed viewer tab switching

## Design assessment before implementation

Baseline: GitHub main `2013aed046a429a3c3b794bef93dabd88aa636f7`.

The browser currently calls `end()` when `document.hidden`. The expanded
viewer's separate selection observer also calls `end()` on every visibility
change. The inline Current Managed Kodi status controller also publishes an
unavailable observation when it pauses on visibility changes, which stops the
viewer. These paths explicitly stop the attachment; this is not a Firefox
transport bug.

An in-place native downgrade is unavailable. The sealed helper starts
TigerVNC with fixed `AcceptKeyEvents` and `AcceptPointerEvents` flags, and its
private protocol supports data, renew, close and initial arming only. Changing
`RFB.viewOnly` alone cannot release durable human ownership or prove neutral
input. No helper, recipe, runtime or profile change is needed for the supported
alternative: the existing Release Control handoff.

On hide, disarm local RFB and auxiliary input synchronously and latch release
intent, including during Take Control. Serialize the existing Release Control
POST after any pending handoff. Its successful view-only ticket proves the old
interactive worker terminated and keys/buttons are neutral: the broker's fixed
proof precedes removal of the durable ownership row and successor admission.
Consume that one-use ticket once, even if hidden. This preserves the logical
viewer and original absolute deadline, with one brief video interruption for a
mode change. A healthy view-only connection needs no handoff on hide/show.
This is a single supported mode transition, without reconnect retries.

Every asynchronous result must match its attempt and client. A hide observed
while Take Control is pending cannot be undone by a later show. If release is
failed, ambiguous, malformed or times out, stop the exact outstanding attempts,
close local video and retain bounded payload-free diagnostics. Claimed native
ownership remains reserved until the existing broker proves cleanup.

Browser visibility events/timers can be delayed or suspended. Add a three-second
interactive foreground lease, renewed through a fixed text message on the
existing authenticated WebSocket every 500 ms only while visible and interactive.
Renewal begins at WebSocket open, including the RFB handshake, so connection
setup does not consume the initial three-second lease.
This is an input-liveness signal, not status polling or a persistence/reconnect
loop. The server refuses renewal after expiry and stops forwarding input and
renewing native ownership. Existing independent Web/broker/helper leases then
bound cleanup even if browser JavaScript never runs again. View-only needs no
browser lease. No new ticket, grant, MCP capability or native control opcode.

Pause only the existing selection observer while hidden, fence its late replies,
and check again on show. Server authority checks continue while hidden and fence
runtime/container/incarnation/selection changes, logout and expiry independently.

Resource policy stays 640x360 at 2 FPS, one worker per instance/two globally,
bounded queues and a ten-minute absolute maximum carried across handoffs. Hidden
view-only streaming consumes the same bounded worker as visible viewing; no
additional worker or frame history is allocated. Browser discard, prolonged
transport backpressure, sleep or network loss can still end viewing safely.

References: [noVNC API](https://novnc.com/noVNC/docs/API.html),
[Page Visibility API](https://developer.mozilla.org/en-US/docs/Web/API/Page_Visibility_API),
`application/novnc_control.py`, `broker/novnc_service.py`,
`runtime/assembly_assets/novnc_view_helper.py`, `web/novnc.py`.

## Focused validation

`INTEGRATION PASS`: 113 tests, no skips or failures. The new file ran separately
(15 passed), followed by existing viewer browser tests (5), Current Managed Kodi
controller tests (4), and 89 selected Web/broker/native-helper contract tests.
Ruff format/lint, strict mypy on all selected Python paths and `node --check` on
both changed controllers and every affected browser harness passed. No complete
regression suite was run.

Reproduce with an existing Playwright installation and cached Chromium:

```console
KODI_LAB_BROWSER_TOOLS=/path/to/existing/node_modules ./scripts/check-integration \
  --focused tests/web/test_novnc_tab_handoff.py tests/web/test_novnc_browser.py tests/web/test_development_session.py \
  --integration tests/web/test_novnc.py tests/web/test_novnc_control.py tests/web/test_novnc_diagnostics.py tests/web/test_novnc_authority.py tests/web/test_novnc_optin.py tests/broker/test_novnc_transport.py tests/broker/test_novnc_state.py tests/broker/test_novnc_lookup_capacity.py tests/runtime/test_novnc_helper.py \
  --source src/kodi_lab/web/novnc.py src/kodi_lab/viewer_diagnostics.py
```

Coverage includes healthy inline/expanded/standalone view-only persistence;
serialized interactive release; native-probe success/failure and retained fences;
held-input cleanup contract; hidden input refusal; rapid hide/show during Take
and Release; release error, malformed response and late response; old RFB events;
connection completion while hidden; hidden disconnect and expiry; selection and
runtime fencing; page exit; repeated explicit control; one-use tickets; original
deadline and capacity retention; expired foreground permission without any
visibility event. Fixed liveness text cannot enter RFB or renew a view-only lease.

Chromium uses real DOM, focus, timers and desktop/mobile viewports, with synthetic
visibility and RFB/API doubles. It does not establish real Firefox tab events,
native Kodi input neutralization or production browser memory/performance.
The broker proof test invokes the existing fixed native-probe call with a Docker
double; it does not run a helper in a retained container.

## Firefox acceptance procedure (pending; no deployment in this PR)

Use an isolated test installation running this branch and an explicitly approved
managed target. Record Firefox version, OS, commit, target and initial viewer
state. The reported retained target is Omega 21.3
`908822fe-132a-5283-ac40-04e26d387db3`; do not change its profile, add-ons or
runtime to carry out these instructions. Repeat the viewing steps on both
Current Managed Kodi's inline viewer and its expanded page.

1. Sign in using the installation's configured Web access mode. Confirm the
   selected target and container binding. Click **Start Viewing**. Expect
   **View Only**, video, and no input from pointer/keyboard or the typing controls.
2. Switch to an ordinary documentation tab with Ctrl+Tab, wait five seconds,
   then return. Expect the same view-only video without Start Viewing, another
   admission or a new RFB connection. Click elsewhere on the same page and
   return focus to the viewer; viewing should remain connected.
3. Click **Take Control** and navigate one Kodi menu using an arrow key. Expect
   **Interactive**. On an approved test target, hold an arrow or pointer button,
   switch tabs, and release it in the other tab. Expect one Release Control POST;
   return after five seconds to **View Only**, live video and no stuck movement
   or button. A brief connection replacement during that handoff is expected.
4. After returning, try keyboard, pointer, Back and typing input before Take
   Control. They must have no effect. Click **Take Control** explicitly and
   navigate again. Repeat three times. Never expect automatic interactive focus.
5. Rapidly switch out/back several times during Take Control, during Release
   Control and while initial viewing connects. If necessary use DevTools network
   throttling in this isolated installation. A hidden pending Take must end in
   view-only, with no interactive attachment after its late response. A pending
   Release must issue no duplicate release. Initial viewing may complete hidden.
6. In the isolated test installation, block the `novnc-release-control` request
   or interrupt the viewer network while hidden. Expect video to end safely,
   no automatic reconnect and an understandable Start Viewing/recovery message.
   An ambiguous native release must keep conflicting Web/MCP input denied until
   the broker verifies cleanup. Restore networking and explicitly start viewing.
   Keep request/ticket/CSRF contents out of evidence; the bounded
   `document.querySelector('[data-novnc-viewer]').viewerDiagnostics` snapshot
   contains only allowlisted attempt/event/time/close metadata.
7. Start a fresh session, switch tabs near its original ten-minute deadline and
   return after expiry. Expect viewing ended and explicit Start Viewing required;
   Take/Release handoffs must not extend that deadline. On a separately approved
   disposable target, verify ordinary Stop/runtime replacement and development
   selection change while hidden also end stale viewing. Do not Stop, replace,
   Reset or repin the retained Omega/Nexus/Shield/Stage 3 environments for this PR.
8. Click **Stop Viewing**, then separately repeat with navigation away and
   logout. Expect connection/worker cleanup, no retained control and no automatic
   reconnect on Back. Verify capacity becomes available only after native cleanup
   proof. A view-only hidden tab may occupy its existing bounded slot until Stop,
   expiry or a real lifecycle/transport failure.

Background view-only now intentionally uses its existing display worker and
bandwidth for the remaining bounded session; it previously used neither after
hide. Interactive foreground adds two 13-byte liveness messages per second,
without HTTP polling. No new worker, frame buffer/history or reconnect loop is
added. Browser discard, sleep, stalled consumption or a foreground JavaScript
pause beyond three seconds may still terminate viewing safely. Native Firefox
acceptance and comparative resource measurements remain open. No production
service, container, managed target, source registration, workspace or Channels DVR
Hub installation was changed by this source-only work.

Final review corrected heartbeat startup during slow RFB handshakes. The targeted
20-test focused lane, Chromium fixtures, Ruff, strict mypy and JavaScript syntax
checks passed again; the original 113-test integration result remains recorded.
