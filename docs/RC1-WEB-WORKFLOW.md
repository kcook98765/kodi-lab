# Kodi 22 RC1 Development workflow

The existing Reviewed runtimes page includes **Kodi 22 Piers RC1** only in
Development view. It is Preview/prerelease software for Linux amd64. It is not
a Home recommendation or a default stable runtime.

Install requires authenticated mutation admission and the exact reviewed
recipe/revision/release acknowledgement. The existing broker-controlled worker
acquires the pinned upstream Flathub closure, verifies it, assembles and validates
the local image, and seals installation evidence. Kodi binaries are not shipped
in the repository or wheel. RC2, final and moving beta revisions cannot substitute.

A Ready card offers Create managed instance. Creation independently checks the
sealed approval, exact image and execution profile through the managed-instance
service. The initial disposable instance has no fixture. Reset uses existing
clean-profile semantics; it does not inherit a Beta fixture's approval.

The instance page has one read-only Screen preview, bounded 5/10/30-second refresh
intervals, pause/resume and hidden-tab suspension. View screen uses that page.
Window and selected-item observation reuse the existing managed UI service.
Screenshots require existing authorization and do not introduce confirmation
prompts or control permissions. MCP grants remain independent and explicit.

## Optional Bridge candidate

The stable Bridge source, archive builder and 1.1.15 approval remain unchanged.
`kodi_lab.kodi.rc1_bridge.build_rc1_bridge_archive` generates a reviewed 1.1.16
candidate from that exact stable source and a wheel-owned text overlay. The
overlay and all generated executable files must match their reviewed hashes.
The generated archive is deterministic and matches the RC1-qualified candidate.

The candidate retains `kodi_version` and adds canonical `kodi_release`. Updated
consumers accept legacy packets while requiring exact release identity for
prereleases. The new approval descriptor is selected only for exact RC1. The
candidate is not a published stable Bridge release.

Generic uploaded ZIP installation remains limited to non-service Python video
plugins. It does not install Bridge. The exact candidate is installed through the
reviewed instance action; no general service-add-on installation authority is added.

Reset retires resident Bridge credentials. The existing observation consumer
refuses an invalidated anchor until lifecycle trust is re-established, so UI-state
inspection remains unavailable until the reviewed Bridge action establishes fresh
trust. Post-reset recovery was qualified in the isolated preview. Native inspection
remains available independently of Bridge trust.

## Activation boundary

Phase 5 code and acceptance use a separate preview database, broker/worker and
Docker daemon. No production deployment, database migration, runtime installation
or grant change is authorized by these results. Production activation requires
separate review.

Initial installation has a provisional 5–15 minute estimate, supported by two cold
installations measured at 8m10.45s and 9m3.02s in durable operation records.
Two samples do not establish a guaranteed range. Additional managed
instances reuse the installed image without downloading Kodi again. Installation
continues after navigation or browser closure; the page polls automatically.
Busy states poll every three seconds, with idle/error backoff and hidden-tab
suspension.

The reviewed Bridge 1.1.16 candidate can be installed through the instance page.
After clean-profile reset, start Kodi and invoke the same reviewed action to
establish fresh trust. See [Bridge and installation guidance](RC1-BRIDGE-AND-INSTALLATION-UX.md).
