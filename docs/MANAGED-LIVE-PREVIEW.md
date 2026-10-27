# Optional managed live preview

In managed instance details, **Start** explicitly authorizes one view-only live
connection for the current administrator Web session and exact running disposable
profile. Preview never starts or reconnects automatically. **Stop**, hiding the
tab or leaving the page closes it. Logout, Web-session expiry, preview lifetime,
policy revocation, lifecycle activity and identity drift also end authorization.
Start again explicitly after resolving an unavailable or disconnected state.

The small picture is640x360 JPEG, quality68, without audio or recording.
The default target is1FPS. An administrator may select adaptive3FPS; achieved
rates can be lower. Capture/encode and browser feedback reduce the rate to keep
headroom. The transport never exceeds 5 FPS. It drops missed ticks and waits for a
decoded-frame acknowledgment before capturing the next frame. Decode/ACK and
send deadlines close failed connections. A retiring capture keeps its capacity
until its actual worker exits. No recurring captures run while preview and
explicit still refresh are inactive.

Live and Screenshot use separate images. Still capture remains manual by
default; Resume periodic refresh opts into its existing timer. Live suppresses
periodic still capture, and manual still capture may contend and be skipped or
refused safely. Still callbacks cannot replace a live image.

## Authorization and runtime qualification

Managed preview has its own process-local, default-disabled administrator policy;
it does not change external-target screenshot preferences, production authority
or MCP screenshot grants. Start requires authenticated Web access, exact Origin
and CSRF protection. Its temporary grant binds the Web session, runtime revision,
container, runtime-state revision and any active selected development-session
generation/workspace/source. Stop revokes a pending native probe as well as an
established connection. Restart drops all policy grants. The supported Web
entrypoint runs one worker; multi-worker/shared preview policy is not supported.
Start/Stop requests carry one single-use attempt UUID. Bounded cancellation
tombstones preserve Stop-before-Start and prevent old requests from retiring a
newer grant. The process retains at most 4096 attempts and logout fences;
exhaustion denies new Starts while exact existing Stop remains usable. Restart
clears that process-local budget and all grants; preview stays disabled until
an administrator explicitly Starts again.

The existing exact legacy Omega display capability is retained. For newer
runtimes, a closed reviewed recipe ID/revision/digest plus existing validated
screenshot/navigation prerequisites permits only a one-shot explicit native
JPEG probe. Streaming becomes authorized only after that probe succeeds for
the actual current runtime/container and its bound identities still match.
Generic screenshot eligibility does not authorize streaming. Recipe candidates:
Ubuntu Resolute Omega21.3 r2 and Ubuntu Noble Nexus20.5 r2, with exact pins in
`runtime/live_preview.py`. Source eligibility and a native frame probe are
separate from campaign-level native performance/acceptance evidence.

The existing authenticated WebSocket endpoint sends bounded version1 metadata
then one JPEG. Metadata includes capture/encode timing and the preceding frame's
value-free send/decode/total-worker timing for bounded performance assessment.
The cumulative `skipped_frames` count reports transient capture contention;
adaptive pacing is reported separately through the target and limited flag.
When a still capture holds the shared slot, preview checks again after250ms
instead of waiting its full adaptive period. These denied checks produce no
broker capture or queued frame. Successful frames retain normal adaptive pacing.
Session audit entries contain aggregate counts/timing, never frame pixels.
Capture retains the screenshot service's current eligibility and exact broker
identity checks, followed by full fresh eligibility validation before pixels
are sent. Lightweight current-authority checks guard the capture boundaries;
they do not cache permissions or replace the final delivery validation.
Its only accepted browser message is the exact decoded-frame ACK:
`{"v":1,"ack":1,"decode_ms":12.5}`. ACKs cannot control Kodi. Each wait and
completed frame revalidates Web authentication and current policy/identity.
Capacity remains one viewer per instance and two globally, for at most15minutes.
The single broker capture path and one active disposable profile remain intact.

## Packaging and performance

Pillow13.0.0 is pinned in the optional `preview` extra and development lock.
Matched Web images install this extra; broker images do not install Pillow.
Source/installed Web runners need `uv sync --extra preview` or the equivalent
locked packaging step. The converter first validates the fixed X11 frame layout,
then downsamples raw pixels directly, without PNG encode/decode. No codec,
new capture service, port or image decoder is added to the broker. Frame pixels
are neither persisted nor added to audit/diagnostic records.

Measure disabled,1FPS and adaptive3FPS on each exact native runtime. Retain the
lower rate when broker/agent p95 rises by both20percent and100ms, or headroom
is unsafe. Windows/WSL2 needs separate native and performance qualification;
Ubuntu source/fixture evidence does not establish Windows runtime acceptance.

Frame age is an estimate: the server capture-to-metadata age plus monotonic browser time since metadata arrival, including final authorization wait and decode/display. It does not depend on synchronized browser clocks and excludes the initial metadata packet transit time. Decode ACK timing remains decode/display time only.
