# Experimental human Web viewer

The noVNC viewer belongs exclusively to authenticated human administrators in
Kodi Lab's Web UI. Agents use the existing independent X11 screenshots, Bridge
inspection and MCP/JSON-RPC operations. There is no agent viewer tool, credential,
grant, VNC capture path or input channel. Agent workflows do not require an active
viewer. JPEG preview and still screenshots remain supported.

The feature is disabled by default in both normal application launchers. A host
administrator may opt in one qualified disposable Ubuntu Omega instance:

1. With the normal broker installed, run the supported sealed installer
   `kodi-lab-runtime install omega --experimental-novnc` in the broker deployment
   environment. Preserve the printed experimental target UUID. This creates a
   distinct registration and never repins the ordinary Omega target.
2. Set `KODI_LAB_EXPERIMENTAL_NOVNC_INSTANCES='["<experimental-target-uuid>"]'`
   in the deployment environment for both Web and broker, and recreate those
   services through the normal deployment workflow. Compose accepts this
   setting; an empty array disables the feature. Initially only one UUID is
   supported. The broker independently requires the exact sealed, validated r4
   recipe, image and provisioned-container policy on every admission.
3. Use the normal authenticated administrator Web UI over HTTPS. In
   **Development → Development session**, select the approved Omega r4 target
   with your persistent workspace, then use **Prepare Kodi** and **Resume**.
   Complete the usual source baseline, deployment and reviewed Bridge setup.
   Click **Watch this Kodi**, then **Start viewing**. This displays the same
   selected Kodi while agents continue using their existing tools. Viewing never
   provisions or starts Kodi. Take Control is a separate action.

The development chooser admits the existing reviewed installation only: target
`d968c23e-67e7-5cb2-99be-472b13ba3536`, runtime revision
`9ee490cd-8eee-424b-b3b2-06c2757ccaa8`, and sealed image
`sha256:8df4cbf1988514ac9713fab7702b0d5f04483f2a167ea95507d9ade66032403b`.
Installing an arbitrary new experimental revision does not qualify it for the
chooser. Approval, installation/validation provenance and cached execution profile
must still match. Turning viewing off leaves normal agent eligibility intact.

For ordinary navigation, GUI launch and smoke testing on the unchanged selected
workspace/runtime, view-only observation remains connected. Other mutations,
including deployment, or any workspace generation/runtime identity change cancel
viewing. Uncertain operations require existing recovery before reconnecting.
Busy **Take Control** is refused without queuing input; a healthy view-only
connection stays open. **Release Control** must verify native worker termination
and neutral input before agent input resumes.

For a sequential version switch, Stop, preserve diagnostics, then use the existing
reviewed **Fresh Test or Switch** workflow. Workspace/source and retained evidence
stay outside the retired disposable profile. Refresh the development-session page
and explicitly open the successor's viewer when supported; old tabs and tickets
never transfer control. Nexus, Matrix and Kodi 22 have no automatic noVNC
qualification. Use the supported Managed resources setting `max_running=1` for
one active development Kodi after checking unrelated managed workload ownership;
keep the selected-session and broker capacity admission checks intact.

The private display socket has no public resolver endpoint, MCP capability or
VNC credentials. It is usable only by the existing trusted Web UID. Authenticated
Web sessions, current catalog identity, exclusive controller admission and native
handoff proof remain required. Administrator cookies are Secure whenever the normal viewer is enabled. HTTP
authentication writes and HTTP/WS viewer use are rejected; proxy trust must
remain restricted to the existing HTTPS proxy addresses.

The experimental r4 recipe uses Xvfb 640×360 and LP_NUM_THREADS=1. Its independent
X11 screenshot primitive expands pixels to the established 1280×720 screenshot
format only for the validated experimental runtime. JPEG preview remains 640×360.
The normal broker resolves this geometry even when the viewer is disabled.
Capture never uses RFB. Production display geometry is unchanged. An existing
agent development session bound to another instance is preserved; matching
sessions still fence workspace, source, generation and container identity.

Start viewing explicitly to connect in view-only mode. TigerVNC rejects keyboard
and pointer input on the server. Take Control terminates that worker, verifies
native termination, reserves exclusive durable input ownership, and establishes
an interactive worker. Release Control verifies revocation and worker death
before establishing a new view-only connection. A handoff preserves the initial
ten-minute deadline; it cannot extend the session. Stop, hidden documents,
connection loss, logout, authority loss and runtime changes revoke the session.

Existing Web/MCP remote input and managed mutations contend with human control
through reciprocal SQLite ownership guards. Read-only screenshot, inspection and
log operations remain available. Uncertain remote dispatch keeps an exclusion
fence, without replaying input. A broker-verified Stop of the exact native runtime
is the existing recovery boundary for an abandoned or ambiguous remote request.
Expiry or Web process loss alone does not authorize clearing that fence.

The native helper uses a private container-local Unix RFB socket and disables VNC
TCP, clipboard, primary selection and desktop resizing. Web has no Docker socket.
A bounded broker display lane owns the Docker-exec tunnel independently of
sequential broker commands. Independent Web/broker/helper leases limit failure
exposure. Claimed ownership survives expiry until native process/lock proof; for
interactive workers, fixed XTest cleanup also verifies neutral keys/buttons.

The display lane allows at most two pending handshakes separately from its
existing two native display workers. Established streams release their handshake
slot so concurrent status and watchdog identity lookups can complete. Lookup
requests never reserve input or create a native worker. Short lookup bursts wait
in the existing bounded socket backlog, subject to unchanged lookup deadlines.
Sustained saturation still fails closed; the per-instance and global display limits,
identity checks and revocation rules remain unchanged.

The embedded client scales the framebuffer, captures keyboard only while
interactive, supports upstream noVNC pointer/touch handling, and offers expanded
view, a mobile typing field and a Kodi Back button (Backspace). Clipboard/file
transfer and audio have no controls or authorized implementation. Physical
keyboard, touchpad and mobile compatibility must be reported only for tested
browsers/devices; browser automation does not qualify physical devices or IMEs.

Pinned upstream noVNC 1.7.0 runtime bytes and licenses are recorded in
`web/static/vendor/novnc/SOURCE.json`. The fixed update target is 2 FPS. The owner
allows a provisional 256 MiB peak incremental browser RSS during evaluation;
128 MiB incremental PSS remains diagnostic. Browser memory may remain after
Stop. Qualification uses a fresh isolated Firefox process with PID-fenced RSS/PSS
monitoring and bounded sessions. No stable active-memory ceiling is claimed.
The owner accepts the measured feature-specific agent p95 increase from 195.76
to 323.06 ms (+127.30 ms, 65.03%) and peak incremental Firefox RSS/PSS of
239.23/121.74 MiB. This does not relax any global latency or safety gate. Keep
sessions bounded to ten minutes and the feature disabled until explicitly
configured. Physical touchpad/touchscreen acceptance remains pending user tests.
Audio requires a separately authorized phase.

The root broker opens the catalog read-only. Ownership writes run in bounded,
one-shot subprocesses as the existing state UID/GID, with no inherited groups.
The closed writer uses standard-library SQLite without loading ORM/Docker/Web
modules, so startup fits the unchanged admission and two-second child budgets.
Broker containers retain SETUID/SETGID to establish this child identity and KILL
to terminate and reap their own timed-out children in the private broker PID
namespace. Without KILL, root cannot signal a child that changed UID. Web
receives no additional capability. An interrupted child leaves the authority
unconfirmed and retains exclusion. There is no additional service or listener.
The initial experimental Web process admits at most 256 attempt identifiers; a
normal service restart is required after that bounded lifetime budget.
