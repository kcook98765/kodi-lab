# Issue #238 native acceptance plan

This is a plan, not native acceptance or permission to deploy. The Phase 5
campaign changed source and isolated fixtures only. Run the final exact-tree
release gate separately after the correction PR is reviewed; do not combine it
with runtime acquisition or graphical/native qualification.

## Capacity and isolation first

Read-only Claw snapshot, 2026-11-06 20:25 UTC:

| Resource | Observed |
| --- | --- |
| CPU / RAM | 4 logical CPUs; 15.4 GiB RAM; approximately 12.4 GiB available |
| Swap | Approximately 0.83 GiB already used, out of 4 GiB |
| Root filesystem | 22,090,153,984 bytes available to this user, about 20.6 GiB; 96% used |
| Docker accounting | 294 images / 96.69 GB reported image size; 31.18 GB build cache; 20.4 GB volumes |

Docker accounting includes shared layers and is not additional free space.
“Reclaimable” is not authorization to delete resources. Do not prune images,
containers, volumes, build cache, worktrees or unrelated workloads to make room.
The deployment pointer still identifies main `71e6a8a` and schema
`0002_managed_setup`; this is receipt metadata, not a new running-source audit.

With the current root headroom, **do not schedule four cold image acquisitions
or a new large VM disk on Claw's root filesystem**. Obtain approved additional
storage/test capacity, or use an already available isolated test machine/Engine.
Check actual free bytes again immediately before any acquisition. Budget final
images, writable profiles, temporary build layers, bundle/DB backups and a
separate operating reserve; image catalog size alone is insufficient. A proposed
6 GiB test guest is only an initial sizing estimate, not a reservation or proof
of sufficient memory. Measure one running Kodi and stop if the host starts
swapping or existing workloads are affected. Run versions sequentially.

A Compose project name alone is **not isolation**: the broker discovers managed
containers by global labels and uses fixed managed network/container identities.
Do not point a second Lab database/broker at the production Docker socket. Use a
separate disposable Linux VM/Engine, or an explicitly approved dedicated Engine
with its own socket, data root, networks and application state. Confirm the
Engine identity and volume/socket mounts before starting the test broker. Do not
copy the installed Lab database, credentials or Bridge trust into that site.
Cached image bytes may be reused only through an existing supported reviewed
installation/attestation path; matching an image tag does not approve it. If a
supported import path is unavailable, stop and budget the reviewed acquisition
instead of fabricating catalog/approval rows.

## Authority boundaries

| Acceptance lane | Safe scope after separate acceptance authorization | Requires additional exact approval |
| --- | --- | --- |
| Disposable isolated Lab | New private database, app/broker/repository services, isolated Engine, test credentials/source/workspace/Git, known disposable canonical profiles; clean Reset of those test profiles | Creating the sandbox, storage/network allocation and any image acquisition must be included in that future authorization |
| Installed Claw or existing Windows Lab | Read-only inventory, resource measurements and supported preflight | Upgrade/deployment, service restarts, grants, runtime Start/Stop, profile Reset, trust changes, source copy/deployment, or cleanup of installed state |

The current campaign authorizes neither lane's native mutations. A later
installed qualification needs an exact target/version/container inventory,
protected-resource baseline, applicable verified backup and supported recovery
plan, plus explicit approval for any selected disposable profile's destruction.
Do not reuse sandbox acceptance as proof of an installed upgrade or vice versa.

## Common acceptance record

Record the candidate commit/tree and bundle digest, application/broker/repository
identity, schema head `0005_managed_web`, approved recipe/image digests, canonical
version target IDs and each observed container identity. Preserve complete
operation/retirement receipts, sanitized tool results and screenshots in a
private evidence directory. Never publish passwords, bearer tokens, pairing
secrets or private source. Preserve all original UUIDs across response loss.

Use a small original video add-on registered outside Kodi, with a private agent
workspace and Git repository. Capture source/workspace file manifests and Git
HEAD/status before and after Reset. Put separate harmless markers in Kodi
settings/add-on profile files so retention versus replacement is observable.
Kodi naturally updates logs, caches and timestamps while running: compare the
exact container and meaningful settings/add-on markers, not a byte-identical
whole profile after Start.

Administrator setup opts a dedicated test client into the required custom
permissions on both client and exact target policies. Grant each canonical test
target explicitly; grant the exact source and bind only that client's workspace.
`managed.versions` is necessary for lifecycle, `managed.reset` separately for
Reset. Existing development/inspection/control permissions remain independent.
Use fresh sandbox credentials. Keep household/external Kodi ungranted.

## Sequential test matrix

1. **Four-version discovery.** Verify Matrix, Nexus, Omega and Piers preview in
   Web and MCP. Confirm approval/cache/eligibility for each before Start. Piers
   retains explicit preview consent; legacy runtime eligibility is not inferred
   from a successful Omega test. Missing images must show the install prerequisite
   without launching an acquisition from `managed_start` or `managed_reset`.
2. **Empty Start and basic screen.** With no development session/workspace
   selected, Start Omega using a persisted UUID. Observe one provision and one
   Start; wait for native JSON-RPC readiness, then paint the actual noVNC
   framebuffer. Start Viewing defaults to view-only. Stop Viewing keeps Kodi
   running. A saved request or Docker `running` is not native/graphics acceptance.
3. **Retained Stop/Start.** Create recognizable Kodi settings/add-on markers,
   close viewers/input and release other clients' holds through their supported
   paths. Stop with the original owner's authority and require `stop_confirmed`.
   Start with a new UUID; verify the exact same container/profile and markers.
   Replaying the old completed Start after Stop must not start Kodi again.
4. **Explicit version switching.** Stop Omega, Start Nexus, Stop Nexus and resume
   Omega. Compare each retained container identity. Repeat sequential availability
   checks for Matrix and approved Piers. Never expect automatic eviction. Starting
   another version while one runs must refuse without stopping either profile.
5. **Protected concurrent activity.** In a disposable site only, exercise active
   view-only viewing, interactive input, a second client's hold/use, playback and
   a controlled in-progress operation. Web and MCP Stop/Reset must refuse. Finish
   the owner's activity normally; never force-release it to make a test pass.
   Concurrent empty starts must not create duplicate profiles or two running Kodi.
6. **Clean Reset, stopped and running.** Use explicit human confirmation or the
   separately granted agent permission. Default Reset must retire exactly the
   selected old container, retain its exact retirement receipt, create a distinct
   stopped replacement from the same approved image and omit old profile markers.
   Image-supplied defaults are legitimate; no old fixture/settings/add-on state
   may be restored. Test `restart=true` separately with no other running version.
   Confirm native Start acceptance and eventual readiness independently.
7. **Development re-preparation.** Follow the exact administrator/agent sequence
   in [the integrated review](ISSUE-238-INTEGRATED-REVIEW.md#development-after-reset).
   Verify old Bridge trust/secret references and deployment baselines are invalid,
   the disposed Current Kodi selection is closed, and private source/workspace/Git
   remains unchanged. Establish fresh reviewed Bridge trust and copied baseline;
   enable the intended add-on through the guarded administrator control. Close
   viewers and explicitly end Web's own keep-awake/recent use (zero) before agent
   handoff, preserving any agent hold. Alternatively, Stop in Web before the agent
   begins and let the agent Start. Keep a
   private workspace edit differing from registered source, validate and actually
   deploy it to the new container. Verify its fresh receipt/container/digests and
   execute the add-on; inspect a unique runtime/UI marker. An old successful
   receipt is historical proof only. A later unchanged deployment may be a
   legitimate no-op only with fresh verification of the replacement's identity
   and bytes. `agent_readiness=not_assessed` remains honest; actual authorized
   tool results establish the development loop, not that Status field alone.
8. **Stale Web/viewing identity.** Keep an old tab, close its active viewer before
   Reset, then attempt its old viewing/control ticket. It must not attach to the
   replacement. Open a fresh view explicitly and inspect pixels. A stale Reset
   confirmation must refuse; a proven unaccepted refusal permits a new human
   confirmation without clearing a lost/uncertain operation. Test keyboard
   traversal, Escape/focus return, mobile widths and the ordinary four actions.
9. **Response loss and restart.** In the disposable site, lose an HTTP response
   after an accepted/confirmed operation, inspect the original receipt and replay
   the exact UUID. Restart only the sandbox application at a known safe boundary
   and verify retained intent. Do not induce an uncertain broker removal against
   installed state. Fake transports cover unknown broker dispatch; an inconclusive
   native removal must stay fenced without another removal or speculative create.
   No receipt/lease timeout or inventory absence may unlock it. Preserve a failed
   sandbox for review unless its exact retirement is separately authorized.
10. **Authorization negatives and finish.** An inspection-only or lifecycle-only
    client cannot Reset. Revoked grants/approval, an unrelated target, another
    workspace and raw container/image/path inputs must fail closed. Check browser
    session expiry, Origin/CSRF rejection and MCP/Web separation. Finish with all
    sandbox Kodi stopped, retained source/evidence and an exact owned inventory.
    Do not clean up production resources or delete unresolved audit receipts.

## Claw / Ubuntu execution

Use the isolated Engine/storage decision above; current disk headroom is a hold
on cold acquisition, not a reason to reduce the four-version acceptance claim.
Reuse already qualified cached images only when their exact digest, reviewed
attestation and candidate catalog admission are independently verified. If four
versions cannot fit, record an Omega-only partial result and leave the others
unqualified. Do not silently retire production profiles for space.

Useful read-only preflight, before a separately approved sandbox launch:

```console
free -h
nproc
df -h / /path/to/approved/test-storage
docker --context <explicit-test-context> info
docker --context <explicit-test-context> system df
```

Verify the test context/endpoint is the isolated Engine before any lifecycle
request. Native screen acceptance needs a real authenticated browser; fake
readiness, a PNG file or successful RFB connection does not prove painted pixels.

## Windows 11 / WSL2 / Docker Desktop execution

Use a disposable Windows machine/VM or an explicitly dedicated Docker Desktop
Engine. A second Compose project on the existing Desktop Engine is insufficient.
Follow [the existing Windows guide](WINDOWS-WSL2-INSTALL.md) and record actual
Windows, WSL, Ubuntu, Desktop and Engine versions rather than assuming the guide's
historical qualified versions are current.

In Windows PowerShell, capture read-only `wsl --version`, `wsl --list --verbose`,
`Get-Volume` and Desktop's resource/disk allocation. In Ubuntu/WSL, capture
`uname -a`, `free -h`, `df -h`, `docker context show` and `docker info`. Confirm
WSL2, Linux containers/integration, sufficient Windows volume and Linux/VHD free
space, and the actual effective memory/CPU limit. Do not resize/move/prune a VHD,
change WSL networking or restart an existing Lab without explicit approval.

Application/broker Python, Unix sockets, peer credentials, SQLite, locks and
private data directories run in Linux containers; this stack adds no native
Windows Docker/shell path. Keep service data/socket/SQLite on supported Linux
storage. Test Git checkout line endings and independently verify real WSL bind
mount permissions, socket UID access, path handling and persistence after a
controlled sandbox restart.

Run the common matrix from a real Windows browser, including local HTTP,
configured private-LAN HTTP and HTTPS only where that mode is explicitly enabled.
Private-LAN HTTP must generate UUIDs without `crypto.randomUUID`. HTTPS still
requires its existing trusted proxy/certificate policy; the fix removes no
security boundary. Verify actual noVNC WS/WSS rendering/input cleanup and an
approved local/stdio MCP connection from the chosen agent environment. A LAN Web
listener does not make LAN MCP authorized. Test the forwarded localhost route
rather than inferring it from a WSL-local probe. Complete a genuine Windows-side
agent deployment/inspection loop before claiming Windows acceptance.

Native Linux, native Windows/WSL2, real browser graphics, installed upgrade and
release qualification are separate results. Record each PASS, partial, blocked
or not-run result honestly. This plan executes none of them.
