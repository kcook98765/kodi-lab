# Standard managed noVNC implementation status

Issue #163 is in progress. New display packaging is separate from accepted
standard browser, security, control, latency and resource qualification. No
standard-qualified runtime is activated by the initial packaging work.

| Version | Display candidate | Standard viewing |
| --- | --- | --- |
| Omega 21.3 | Reviewed immutable r5, explicit candidate installation | Disabled pending native qualification |
| Nexus 20.5 | Reviewed immutable r3, explicit candidate installation | Disabled pending native qualification |
| Matrix 19.4 | Pending | Disabled |
| Piers 22 RC1 | Pending | Disabled |

Omega r5 preserves the r4 frozen payload, TigerVNC package and helper hashes,
640×360 startup, `LP_NUM_THREADS=1`, private Unix RFB and fixed helper policy.
Its new recipe digest is
`sha256:505c5e7bf43e9b61e74250477c0dab5ee8fe4bd3ac8482d1b83222aff95ae8b9`.
It requires fresh sealed v2 display packaging evidence, including pre-execution
helper integrity and fixed Xlib geometry probes, plus exact helper/package,
geometry, renderer-thread environment, supported options, native libraries and
neutral-input/worker probes on both normal Kodi start/stop cycles. This packaging
proof does not establish actual browser FPS, ownership handoff or performance.

The controlled qualification installer can explicitly select the closed candidate
with `kodi-lab-runtime install omega --display-candidate`. This accepts a reviewed
generation, never a caller recipe, package, image, command or path. Ordinary
installation remains r2; experimental installation remains r4. The existing
experimental UUID setting cannot activate the r5 candidate. Viewing stays off.

Installation does not upgrade an existing registered profile: bootstrap preserves
its pin. New r5 uses ordinary Omega's release-based target identity, while the
historical experimental r4 target remains distinct. Existing retained profiles,
selection and images must remain unchanged. A future production transition needs
a separately approved backup/recovery and profile-preservation plan; Fresh Test
and silent repinning are not upgrade procedures.

All-version qualification, normal Web activation, browser resource/latency
acceptance and final integrated release qualification remain required. Keep the
existing ten-minute bound, approximately 2 FPS, view-only default, optional control,
input exclusion, HTTPS/WSS and independent MCP/X11/fallback paths. Experimental
memory or latency exceptions are not standard acceptance.

Nexus r3 retains r2 unchanged and pins Noble TigerVNC 1.13.1+dfsg-2build2 with
archive SHA-256 `f3844df856a95e4b306a61c03fa75433599d247ed2f9551df15003b191a8d054`.
Its frozen startup uses 640×360 and `LP_NUM_THREADS=1`, a Noble-specific frozen helper,
and the v2 native packaging proof. Controlled acquisition uses
`kodi-lab-runtime install nexus --display-candidate`; ordinary installation stays
r2. Qualification uses the existing exact-runtime entrypoint, now consuming sealed
display metadata for profile identity and enforcing HTTPS/Secure authentication.
This does not activate the ordinary launcher or qualify the standard Web workflow.

The first Nexus native trial rejected `AcceptCutText`, `SendCutText`, `SetPrimary`
and `SendPrimary`: they are absent in pinned TigerVNC 1.13.1. Its XDesktop clipboard
handler is empty and its inherited clipboard callbacks are no-ops. The candidate
therefore records `clipboard_policy=unimplemented`, requires that exact package and
helper hash, and excludes only these unsupported switches. Newer Omega's explicit
off switches are unchanged. Input/resize/TCP/lifetime safeguards remain. Source:
https://github.com/TigerVNC/tigervnc/blob/v1.13.1/unix/x0vncserver/XDesktop.cxx
and https://github.com/TigerVNC/tigervnc/blob/v1.13.1/common/rfb/SDesktop.h .
Actual native clipboard refusal in both directions remains required before standard
promotion; option absence alone is not browser/security qualification.

## Matrix r3 candidate (disabled)

Jammy r3 preserves r2 and all Kodi19.4 package pins. It adds reviewed
TigerVNC1.12.0+dfsg-4ubuntu0.22.04.1 (SHA256
`eb8928727ce07aae043211e9f35390e2aafabf89b16168d43d3c608322e5a852`),
full Python support, frozen640×360 startup with LP_NUM_THREADS=1 and the
exact native helper. Like Nexus, this pinned X0 implementation has an empty
clipboard handler and inherited no-op callbacks; it uses the exact-recipe
`unimplemented` policy rather than unsupported switches. Upstream source:
https://github.com/TigerVNC/tigervnc/blob/v1.12.0/unix/x0vncserver/XDesktop.cxx
and https://github.com/TigerVNC/tigervnc/blob/v1.12.0/common/rfb/SDesktop.h .

Controlled acquisition uses `kodi-lab-runtime install matrix --display-candidate`.
Ordinary installation stays r2. The sealed v2 native packaging proof remains
required, and all browser/clipboard/control/performance acceptance is separate.
Standard viewing stays disabled. Existing managed profiles are never repinned.

## Piers installation/support/assembly r3 candidate (disabled)

The new candidate retains the exact signed Flathub app/platform/GL closure, RC1
source, nonroot launcher/loader boundaries and all 97 r2 support package entries.
Its reviewed Ubuntu support closure adds19 exact dependencies, including Resolute
TigerVNC1.15.0+dfsg-2build1. The resulting116 DEBs total78,022,102bytes. Perl-base
from the pinned base is explicitly updated5.40.1-7ubuntu0.1 →5.40.1-7ubuntu0.3,
matching the exact added Perl libraries required by tigervnc-common. This applies
only to new r3 assembly; r1/r2 assets, closures and images remain unchanged.
Every added package is bound to a SHA256/length and Ubuntu-signed index; support
acquisition remains closed, bounded and replacement-free. Kodi package/source
pins are unchanged. New supervisor freezes640×360, Docker environment sets
LP_NUM_THREADS=1 and the root-owned0444 helper is identical to Omega r5.

Preview admission remains explicit development-only consent bound to the exact
recipe and operation. The application service's closed display-candidate selector
is available for isolated qualification, while normal Preview defaults stay r2.
Broker installation keeps its fixed command; the worker selects r3 only from its
already-validated application-owned operation acknowledgement. Direct CLI use
without that admission is rejected. Socketless acquisition adds only a fixed r3
support-helper command, not caller packages, URLs, paths or general execution.
Native inventory, attestation, image and sealed display v2 proofs bind the exact
support/assembly revision. Standard viewer admission stays disabled pending actual
clipboard, browser, control, ownership and resource/performance acceptance.

Native Piers trial exposed a loader-specific process identity: the unchanged reviewed
launcher executes Kodi through `/opt/fdo/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2`,
so its native process name is `ld-linux-x86-64`. A fresh isolated probe from the
verified cached RC1 image confirmed the executable and complete command line;
no retained production container was started or changed. The shared packaging
proof now uses the exact frozen loader path, library path, Kodi binary, argument
list and UID1000 for Piers, then reads that unique process's LP_NUM_THREADS.
Other versions retain their existing process check and receipt shape. Piers v2
receipts additionally bind `process_probe=piers-loader`; an ordinary process proof
cannot authorize Piers display capability. This corrects a process-identification
assumption; no thread, geometry, package, resource or performance gate is removed.

## Current Kodi candidate qualification composition

Current Kodi can select the canonical Piers r3 disposable profile only with the
exact reviewed manifest/reference and existing sealed Preview approval. Ordinary
r2 retains its previous retained-profile selection behavior; a version string,
substituted artifact or unapproved revision cannot grant selection. Existing
source/workspace, generation, empty-inventory and lifecycle reservations remain.

An injected exact-runtime qualification viewer now reports availability through
Current Kodi for each installed display descriptor, retaining the legacy r4 path.
Ordinary broker admission still rejects all candidates, so this composition does
not activate standard viewing. It lets isolated native acceptance exercise the
same selected-environment workflow before any standard promotion.
