# Standard managed noVNC implementation status

Issue #163 is in progress. New display packaging is separate from accepted
standard browser, security, control, latency and resource qualification. No
standard-qualified runtime is activated by the initial packaging work.

| Version | Display candidate | Standard viewing |
| --- | --- | --- |
| Omega 21.3 | Reviewed immutable r5, explicit candidate installation | Disabled pending native qualification |
| Nexus 20.5 | Pending | Disabled |
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
