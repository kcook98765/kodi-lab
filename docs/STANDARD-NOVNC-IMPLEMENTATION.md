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
