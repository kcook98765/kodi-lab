# Managed Kodi graceful shutdown

Normal Stop must give Kodi an opportunity to save pending settings before its
session bus and display disappear. This correction changes reviewed runtime
images; an application-only upgrade does not repair an existing image.

## Cause and corrected runtime revisions

The native Omega investigation for issue #238 observed an immediately readable
JSON-RPC device-name change that remained unsaved in `guisettings.xml`. Leaving
Kodi's Services settings page saved a native UI change. Managed Stop retained
that saved value and the exact container, but discarded a later unsaved RPC
change: it stopped in about 0.17 seconds with exit 143 and no normal save log.
Native `Application.Quit` saved the restored original value and exited 0 in
3.384 seconds. The defect concerns graceful application exit, not profile loss.

The old shell entrypoint stored the PID of `dbus-run-session`, signalled that
wrapper, and then terminated Xvfb without establishing Kodi's own exit.
Matrix, Nexus and Omega shared this startup mechanism. New reviewed recipes are:

| Version | Previous display recipe | Corrected recipe |
| --- | --- | --- |
| Matrix 19.4 | r3 | `matrix-19.4-graceful-r4.json` |
| Nexus 20.5 | r3 | `nexus-20.5-graceful-r4.json` |
| Omega 21.3 | r5 | `omega-21.3-graceful-r6.json` |

Piers already uses a Python supervisor and is unchanged. Its shutdown approach
is the basis of the correction; this does not claim new native Piers acceptance.
The earlier APT r1/r2 and experimental Omega shell revisions also retain
their original shutdown behavior. Historical recipes, hashes and asset bytes
remain loadable unchanged; no historical image is relabelled as repaired. The new
stable recipes reuse their historical package/profile/display/helper bytes and
seal a versioned entrypoint/supervisor pair. Future supervisor changes require
another asset version and new recipe identities.

## Shutdown contract

Docker SIGTERM reaches the Python supervisor as container PID 1. It starts
Xvfb and the Kodi/D-Bus session in separate owned process groups, preserves the
existing fixed display preflight, geometry, software renderer and thread limit,
and accepts no caller command, PID, endpoint or policy.

On Stop it requests supported native `Application.Quit` on the fixed private
loopback endpoint. Kodi then runs its own save and shutdown sequence. If HTTP
is unavailable or denied, the supervisor can signal exactly one `kodi.bin`
process only after checking its owned process group, UID, executable and kernel
PID handle. The D-Bus wrapper and display remain alive while Kodi exits. Missing,
ambiguous or unverifiable main-process identity does not authorize signalling a
different PID. PID-handle fallback requires Linux kernel support; qualify it on
the intended Linux/WSL2 Engine. The normal HTTP path does not need that fallback.

The six-second Kodi stop budget includes a one-second absolute Quit-request
budget. A single bounded request thread prevents trickling HTTP responses from
extending a socket inactivity timeout indefinitely; it adds no service or retry. Remaining owned
processes get bounded cleanup; an unresponsive group can be forcibly terminated
only as a failure path. Xvfb cleanup follows Kodi/session cleanup. PID 1 reaps
adopted session descendants, including after an early wrapper exit. Cleanup
failure, forced termination, Kodi failure or a missed deadline produces nonzero
container exit and a `supervisor-cycles.jsonl` diagnostic. Normal exit is 0.
The complete designed stop budget is under eight seconds, below the unchanged
ten-second broker Docker Stop timeout. Polling waits observe process completion;
there is no arbitrary settings-save sleep or configuration-file rewrite.

Broker ownership, identity, reservations, receipts, authorization and uncertain
operation reconciliation are unchanged. Existing Stop receipts establish exact
container stoppage; they are not newly defined as proof of a clean application
exit. Inspect the supervisor diagnostic and container exit result for that
additional distinction. No new MCP or Web authority is introduced.

## Fresh validation and installation

For these new recipes the existing two-cycle image validator now requires exit
code 0 and changes a harmless native setting before the first Stop. The next
Start must read that pending value; the second cycle restores the original.
Successful validation records `pending_setting_persistence=true` in the sealed
report. Exit 143/137, missing/invalid exit status, rejected setting changes or
lost values fail validation before the installation can become Ready.

The existing reviewed installer selects the new stable recipes, for example
`kodi-lab-runtime install omega`, in a separately authorized matching candidate
installation. The application bundle's loose recipe inventory is pinned to the
same new digests. This still uses the existing signed-package acquisition,
closed assembler, native validation, exact image registration and deployment-local
HMAC attestation. A cached r5 image or old Ready/HMAC receipt cannot stand in for
r6. Base/package layers may be reused only by the supported builder; no new
runtime image or native qualification was produced by the source correction.

Existing profiles retain their exact runtime pin. Installing r6, restarting
Kodi Lab or running clean Reset does not silently select a new runtime. Reset
recreates from its approved pinned image. An operator wanting to replace a
historical profile must explicitly authorize its supported retirement, satisfy
selection/hold/viewer/reconciliation fences, and use the existing guarded
`kodi-lab-lab repin omega <exact-ready-revision-UUID>` recovery command only once
the old container is deprovisioned. Then prepare/start the new pinned profile
through supported lifecycle services. Retain retirement/installation receipts;
reestablish Bridge trust and deployment baselines normally. Do not bypass a
retained-profile refusal or edit the database to make this transition possible.
This source change does not add or qualify a new profile-upgrade workflow.

## Native qualification and capacity decision still required

First review this draft and run one separately authorized full release gate on
its final exact source tree. Application deployment and runtime acquisition are
separate owner-authorized steps. Do not build on Claw merely because source
fixtures pass. The installed application and stopped Omega remain unchanged.

Read-only Claw measurements on 2026-11-07: approximately 19.38 GiB root free,
12.6 GiB RAM available and 0.81 GiB swap used. The retained Omega image reports
1,295,447,406 bytes (about 1.21 GiB). That virtual image size is not a build-space
estimate: account separately for missing base layers, downloaded APT archives,
unpacked/install layers and BuildKit cache, the new final image, sequential
validation writable layers, candidate application artifacts and evidence. Keep
the existing image and all unrelated Docker resources.

Use an explicit measured peak budget plus an operating reserve. As a planning
allocation, allow at least 8 GiB additional build/validation headroom; this is
not a measured guarantee. Retaining the existing standard noVNC qualification's
20 GiB free-space safeguard would require at least 28 GiB free before that
allocation, then confirmation against the actual planned peak. Claw currently
falls below even that reserve. Prefer separately approved storage or another
properly isolated Engine/host; another Compose project is not Engine isolation.
Do not prune, acquire or weaken a resource safeguard without a separate capacity
decision. No precise new-image peak is claimed before acquisition is planned.

Qualify Omega first on the corrected exact image: native readiness, genuine
noVNC, pending JSON-RPC setting plus UI-saved setting across managed Stop/Start,
exit 0 and save-before-display logs, exact profile identity, and original-value
restoration. Use a disposable isolated installation for hang/failure tests;
do not induce uncertain removal or artificial destructive failures in production.
Recheck install attestation, retained-source/workspace/Git preservation and
normal Bridge/deployment setup after any explicitly authorized replacement.
Then qualify Matrix/Nexus and Windows 11/WSL2 separately. Process fixtures and
source release gates do not constitute native Kodi acceptance.
