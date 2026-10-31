# Standard managed noVNC implementation status

Issue #163 is in progress. New display packaging is separate from accepted
standard browser, security, control, latency and resource qualification. No
standard-qualified runtime is activated by the initial packaging work.

| Version | Display candidate | Standard viewing |
| --- | --- | --- |
| Omega 21.3 | Reviewed immutable r5, explicit candidate installation | Disabled pending complete browser qualification |
| Nexus 20.5 | Reviewed immutable r3, explicit candidate installation | Disabled pending complete browser qualification |
| Matrix 19.4 | Reviewed immutable r3, explicit candidate installation | Disabled pending complete browser qualification |
| Piers 22 RC1 | Reviewed installation/support/assembly r3 | Disabled pending complete browser qualification |

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
Actual native clipboard contract results are recorded below. Option absence alone
is not browser/security qualification or permission to promote a candidate.

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
support/assembly revision. Standard viewer admission stays disabled pending complete browser, control,
ownership and resource/performance acceptance; finite native clipboard results
are recorded below.

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

### Bounded viewer admission replay window

The previous 256 attempts per Web-process lifetime is replaced by at most 256
replay entries covering 120 seconds. Viewer request IDs use UUIDv7 creation times
(RFC 9562), seeded from server time plus monotonic elapsed page time. The server
rejects untimed IDs, timestamps over 5 seconds ahead and requests 120 seconds old,
and rechecks after authority lookup. Its clock advances monotonically across
wall-clock adjustments, so evicted requests cannot become valid again.

Cache saturation refuses admission through the latest expiry of every rejected
or cancelled ID. This constant-size quarantine preserves Stop-before-Start even
when no tombstone slot remains. Busy periods can temporarily prevent new viewers;
Stop and verified ownership cleanup continue. Replay metadata expiry never ends
native ownership or the separate ten-minute session. Attach tickets remain single
use and bound to authenticated owner, exact runtime and selected development
identity. Process-local replay state retains the existing restart boundary;
durable native ownership still requires verified release after a Web crash.
Existing stale JavaScript must reload to use the timed request protocol.

A Stop for an unknown ID beyond the future tolerance returns409 rather than
falsely confirming cancellation or reserving unbounded future replay state.
Expired or untimed unknown IDs are permanently inadmissible and need no row.

The pinned noVNC JavaScript module loads only after explicit Start Viewing.
A cancelled or retired selection is checked again after module loading, before
any server ticket is requested. This reduces idle viewer work; native browser
memory and latency still require independent acceptance on the exact candidate.

### Fixed compression preference; qualification remains separate

The viewer requests noVNC1.7.0 compression level0 instead of2, retaining image
quality level6,640×360, approximately2FPS, fixed runtime workers and all transport,
input and session bounds. This uses the existing RFB encoding preference; it adds
no configuration surface or replacement transport. Lower compression trades
native encoding work for potentially more bytes under the existing bounded
stream/backpressure safeguards.

An isolated Matrix Chromium configuration experiment at level0 maintained2.00
rendered FPS with550ms longest gap and passed the unchanged compound p95 checks:
overall agent866.50→963.31ms; screenshot capture882.89→1013.10ms (14.75 percent
increase, so the compound rule passes despite the130.22ms delta). The earlier
level2 trial failed its agent check. These are finite matched observations within
each trial, not a controlled cross-trial causal estimate or long-term guarantee.
Interactive browser RSS still failed:129.742MiB above its steady viewing-off
baseline (below its earlier blank baseline); the original128MiB limit was retained.
Firefox failures also remain. No runtime becomes standard-qualified through this
preference change, and native browser/lifecycle qualification remains required.


### Exact-runtime Stop after native helper loss

If the native viewer helper crashes while human input is held, durable ownership
continues excluding agent input. The normal advanced and Current Kodi Stop paths
now revoke viewing within the existing lifecycle and dispatch fence, then allow
the physical Stop to proceed. Only an exact matching stopped-runtime broker
receipt retires matching ownership. Unknown, mismatched or still-running results
retain exclusion; Release Control still requires verified termination and neutral
input. Actual isolated Omega held-input crash tests passed both normal Stop paths.
The original circular pre-Stop neutral-input failure remains recorded.

### Clipboard qualification evidence

The pinned TigerVNC implementation can query X11 TARGETS metadata when a selection
owner changes. Qualification permits only bounded atom-format metadata requests;
foreign text requests still fail before any text is served. Real CLIPBOARD and
PRIMARY conversion positive controls remain required, with unchanged ownership
and continuous bounded RFB clipboard-message observation.

The corrected direct native package trial did not reach injection: its additional
RFB client failed admission while the existing helper owned the single-client
connection. NeverShared and DisconnectClients policy remains unchanged. Both
selection positive controls succeeded, but this is not clipboard-content refusal
qualification. The original metadata-observer failure and this admission failure
remain separate failed trials. The original failures are not reclassified.

A subsequent independently reviewed native contract test used the unchanged frozen
helper's sole primary connection through its existing armed pipe protocol. All
four sealed images passed legacy and advertised extended clipboard probes: each
packet had a full-frame acknowledgment and at least three seconds of continuous
observation before the next packet. Both real X11 selection positive controls
passed; foreign text requests, server content messages and ownership changes were
refused by the test. Normal helper termination, neutral input, worker/socket
cleanup and normal Kodi Stop were verified. This finite view-only package contract
adds no clipboard feature and does not establish browser, interactive performance
or standard acceptance. All four images also passed real backend ten-minute
expiry and held-helper-crash recovery through Current Kodi Stop. These results do
not replace the separate browser lifecycle and resource checks.

## Qualification and rollout boundary

All four candidate images passed the supported sealed installation procedure and
its two ordinary native Kodi start, JSON-RPC, navigation, independent capture and
clean-stop cycles. Packaging establishes exact image provenance and native display
requirements; it does not qualify standard viewing. Candidate qualification
status remains explicit and disabled for ordinary viewer admission.

Current Kodi composes the exact selected candidate during isolated qualification.
An ordinary installation still uses the existing reviewed defaults, preserves
retained registrations and does not gain viewing from a version name or screenshot
support. The existing experimental Omega r4 path retains its original admission.
There is no automatic viewer start, profile reconstruction or runtime upgrade.

The native campaign preserves failed trials. Firefox on Piers exceeded the
original 128 MiB incremental browser RSS limit before Start Viewing in both
observations: 141.81640625 MiB and, after deferred module loading, 128.40625 MiB.
The latter remains a failure; neither a tolerance nor the historical experimental
256 MiB allowance applies. These finite measurements do not prove a stable
long-term browser memory ceiling. Chromium qualification is separate and cannot
turn the Firefox observations into passes. Human control and view-only outcomes
must also be reported separately, including the unchanged compound agent/broker
p95 rule: failure when growth exceeds both 20 percent and 100 milliseconds.

The complete release gate is deferred until a fully qualified standard candidate
exists. Source checks, sealed packaging and backend transport/control acceptance
cannot substitute for browser FPS, latency, resource and lifecycle acceptance.
The implementation campaign does not deploy application changes or authorize a
production profile transition.


### Finite browser observations

No version is fully standard-qualified. The sandboxed Chromium trials use the
original 128 MiB incremental RSS allowance against both blank-document and steady
viewing-off baselines; no forced garbage collection or cache override is used.
Loaded view-only observations and interactive outcomes remain distinct:

| Candidate observation | Displayed FPS / longest gap | Agent p95 off → view-only | Interactive result |
| --- | --- | --- | --- |
| Matrix compression0 configuration experiment | 2.00 / 550 ms | 866.50 → 963.31 ms | RSS +129.742 MiB, FAIL |
| Omega compression0 source trial | 2.00 / 550 ms | 872.52 → 960.16 ms | RSS +130.143 MiB, FAIL |
| Piers Stop-recovery source trial | 2.00 / 554 ms | 995.41 → 1083.06 ms | RSS +128.605 MiB, FAIL |
| Nexus Stop-recovery source trial | Initial 10-second sample 2.00 / 512 ms | 923.67 → not completed | Not reached; view-only RSS +128.117 MiB above blank, FAIL |

The Matrix, Omega and Piers matched view-only trials passed the unchanged compound
agent, broker and per-tool latency checks, with zero invalid motion samples.
Nexus aborted during view-only observation before its matched on-stage completed;
its initial motion sample is not a loaded latency or lifetime pass. They are finite observations,
not full view-only standard qualification. Matrix's later exact compression0
source smoke independently measured 2.00 FPS / 516 ms; it was not a repeat of the
full matched performance trial. The earlier Matrix compression2 trial failed its
agent latency rule (881.80 → 1103.43 ms). All original failures remain failures.
Those earlier resource aborts did not complete interactive timing or browser
ten-minute acceptance. Later fresh-profile Chromium observations are separate:

- Omega observer1 completed all three matched stages, eight displayed keyboard
  menu changes (p95 820 ms), actual ten-minute browser expiry, held-input cleanup
  and three explicit fresh admissions. Both loaded modes maintained about 2 FPS,
  with maximum gaps of 600 ms view-only and 551 ms interactive. Agent p95 was
  873.10 → 976.86 → 1008.93 ms; all compound per-tool and broker checks passed.
  The whole trial nevertheless failed its final closed-document RSS check:
  +188.361 MiB above viewing-off. The observer had created an unrelated blank
  target before closing the viewer. Its reviewed correction closes the actual
  target without replacement and preserves all memory and cleanup assertions;
  the failed trial remains failed. The subsequent Omega observer2 trial stopped
  at a separately added first-valid-frame-after-Take guard (1095.892 ms against
  1000 ms), before complete interactive qualification. This guard is additional
  campaign evidence, not a failure of the original compound latency rule.
- Nexus observer2 completed all three matched stages with no resource abort:
  loaded view-only 2.00 FPS / 553 ms and interactive 2.00 FPS / 601 ms, zero invalid
  samples. Agent p95 was 850.86 → 979.32 → 1002.49 ms; all compound checks passed.
  Eight displayed keyboard changes had p95 729 ms. The whole trial failed an
  immediate native mouse-down-state assertion; browser expiry was not reached.
  Normal Kodi Stop and zero live browser survivors were verified, while the
  browser UI-close error remains part of the failure. A separate focused delivery
  diagnostic was blocked at explicit Start Viewing before any pointer dispatch;
  it establishes no pointer-delivery result.

- Piers observer2 completed all three matched stages without a resource abort,
  eight displayed keyboard changes (p95 741 ms), actual browser ten-minute expiry
  (cleanup +3.452860 s) and three fresh admissions. Agent p95 was
  948.13 → 1051.85 → 1035.37 ms; all compound checks passed. The whole trial failed
  when an immediate target query still saw the page after the close acknowledgment.
  Normal browser-family cleanup and Kodi Stop succeeded. The corrected observer
  requires actual page absence within one second of the close invocation; it
  does not allocate another tab or waive the post-close memory checks. This
  correction does not reclassify the Piers trial or repeat its passed stages.

- Matrix observer3 could not measure displayed motion because a delayed Kodi
  Version Check modal obscured the native motion fixture. An independent native
  screenshot proved the dialog; this is not evidence of a frozen RFB stream.
  After a reviewed exact-dialog guard, observer4 measured view-only 2.00 FPS /
  600 ms with zero invalid samples and passing compound latency checks. It then
  failed the additional first-valid-frame-after-Take guard at 1056.220 ms against
  1000 ms; interactive performance and browser expiry were not reached.
- A separate Matrix functional component preserved its first failed trial
  (`GUI.GetInfoLabels` was unavailable). The corrected observer uses the existing
  `XBMC.GetInfoLabels` method. Its next actual trial failed because a 561 ms
  native observation exceeded the unchanged 500 ms sampling bound; normal Kodi
  Stop and browser close succeeded. This is an observation-coverage blocker,
  not proof of unauthorized input or complete input refusal. The component has
  no matched performance or standard-qualification claim.

- The separate Nexus functional component also failed its unchanged observation
  bound: a 502.698 ms receipt gap exceeded 500 ms in the late-effects window.
  Recorded native key, pointer and UI samples were unchanged, but coverage was
  insufficient for a refusal PASS. Normal Kodi Stop and browser close succeeded.
  Both disposable component profiles were retired through normal lifecycle APIs;
  control and browser-expiry qualification remain incomplete for Matrix/Nexus.

These later procedures removed an unused CDP Network event subscription, verified
actual Secure/HttpOnly/SameSite=Strict cookies, and required exclusively created
version-and-trial browser profiles. No browser cache override, forced garbage collection,
sandbox exception, memory waiver or latency threshold change was used. The older
Nexus trial also had a reused-profile limitation discovered later; it stays failed.
Backend ten-minute evidence cannot replace complete browser lifetime, resource or
control qualification. No partial observation activates standard viewing.

## Future transition of retained profiles

Runtime target identities are release-based. A newer recipe for the same release
therefore uses the existing canonical target identity; installation deliberately
preserves an existing registration's pin. Installing a candidate does not upgrade
that profile. Standard Omega r5 uses ordinary Omega's identity, while the retained
experimental r4 remains distinct and retains its current selection behavior.

The existing explicit repin procedure requires Stop and deprovision. It is a
profile replacement operation, not an in-place preservation mechanism. Fresh Test
and Switch must not be used to make viewing available on retained profiles.
Until a supported preservation procedure is reviewed and approved, retained
profiles keep their current recipes and eligible screenshot/JPEG fallbacks.

A separate production rollout needs explicit owner approval of a concrete plan:
a fresh supported backup and isolated recovery rehearsal; exact old/new runtime
and profile identities; Kodi profile, schema, add-on and Bridge compatibility;
a reversible profile restore/reconstruction procedure; and verified selection
and rollback. No arbitrary clone target, direct database edit or silent repin may
bypass the existing canonical target and retained-selection fences. If profile
replacement is necessary, that decision must be presented explicitly before any
production action. This campaign leaves all retained profiles unchanged.
