# Kodi Lab noVNC Phase 2: interactive human viewer

**Overall verdict: PARTIAL. Interactive control is implemented; native agent-latency qualification FAILS.** Achieved check level: **FOCUSED PASS with completed release-stage follow-ups**. A clean single-invocation RELEASE PASS is not claimed; the full-run expectation failure and its correction are retained. Keep the viewer experimental and disabled by default. No merge, default production deployment or audio implementation is authorized.

## Architecture and source

The viewer is exclusively for authenticated human administrators of Kodi Lab's Web UI. Agents retain existing MCP screenshots, Bridge inspection, logs and remote actions. There is no agent VNC tool, grant, credential, screenshot facility or input channel. The owner separately authorized deterministic Firefox automation only as an isolated native qualification harness; that harness is kept outside production capabilities.

Development reuses the unmerged Phase 1 prototype (`dccf1447`) on isolated branch `feat/novnc-phase2-interactive-20261027`, based on accepted main `775904bd`. Production and the original prototype worktree remain separate. noVNC is pinned to unmodified upstream 1.7.0. The experimental r4 Ubuntu Omega 21.3 runtime alone uses 640×360 Xvfb and `LP_NUM_THREADS=1`; the default recipe/production geometry remain unchanged. The viewer target is fixed at 2 FPS, one viewer per managed Kodi, explicit startup, view-only default, absolute ten-minute session deadline carried through handoffs, and no audio.

The existing container-local 0600 Unix RFB socket remains inside a 0700 directory. TigerVNC publishes no TCP VNC listener. Authenticated same-origin HTTPS/WSS carries an owner-bound single-use tab ticket; Web has no Docker socket. The bounded display lane and Docker-exec tunnel run independently of sequential broker operations. A fresh native nonce challenge must receive a trusted broker response after a durable authority check before TigerVNC can start. Browser RFB payloads cannot arm or renew a worker. Pending handshakes, workers, message lengths, backlogs and retained cleanup results have explicit bounds.

Changed first-party areas:

| Area | Principal files and behavior |
|---|---|
| Durable human ownership | `application/novnc_control.py`, storage models and migration `0087_novnc_control.py`: exact instance/attempt/owner/mode/container/start identity, conditional claim/cancellation, leases, recovery and loss-proof retention |
| Existing input coordination | `browser_remote.py`, `browser_remote_coordination.py`, `lifecycle_operations.py`, `runtime_lifecycle.py`, repositories: reciprocal SQL exclusion and shared kernel dispatch/Stop flock; exact stopped receipt is independent recovery |
| Native authority | `broker/novnc.py`, canonical and r4 `novnc_view_helper.py`: separate static view-only/interactive TigerVNC flags; trusted challenge, bounded leases, inherited flock, parent-death signal, termination and neutral-input proof |
| Web | `web/novnc.py`, app wiring, `novnc-phase1.js/css`, instance-detail link: Take/Release Control, Stop, typing field, Back and expanded view; existing authentication/CSRF/origin boundaries |
| Experimental runtime | reviewed r4 recipe/assets, eligibility, validation, recipe selection and qualification launcher; historical r3 assets remain pinned independently |
| Independent capture | broker screenshots, live-preview runtime map, validation: server-selected qualified geometry, fixed X11 capture and canonical 1280×720 still output without any viewer connection |
| Tests/docs/vendor | ownership/migration/transport/browser/capture/recipe regressions, D-0458, experimental documentation and upstream 1.7.0 provenance/licenses |

The 640×360 capture conflict is resolved by catalog/artifact-selected experimental X11 capture, with exact image and StartedAt checks before and after capture. Its pixels expand 2× to the established 1280×720 still format. The ordinary app/broker resolves this independently even with viewer routes absent. The fixed experimental X11 parser tolerates at most sixteen unsolicited MappingNotify events; other or excessive packets fail. Neither screenshots nor JPEG preview consume RFB.

## Modes and controller coordination

View-only TigerVNC has keyboard and pointer acceptance disabled on the server. Take Control first retires and proves the previous worker dead, then establishes exclusive durable input ownership before starting a fresh interactive worker. Release Control verifies revocation, worker death, lock release and neutral keys/buttons before a new view-only worker or conflicting input can proceed. Browser flags are only convenience controls.

Managed agent/Web remote input and managed mutation leases contend with the human controller through reciprocal SQLite guards. Read-only diagnostics remain available. A rejected Start no longer cancels the active viewer: mutation reservation must succeed before display cancellation. Remote actions hold a shared cross-process flock through actual dispatch. Uncertain mutating delivery retains a durable guard without replay; exact ordinary native Stop supplies recovery. Proven failure before any mutating delivery can release its guard.

Logout, authentication loss, socket loss, Stop, hidden/page-unloaded documents, deadline/lease expiry and runtime identity drift revoke the viewer. Claimed rows are never freed merely because time elapsed. Native death/neutral-input proof or an exact native Stop is required. A separate cleanup probe only observes input; only the original armed helper can release held input while still owning exclusion.

## Browser and input acceptance

Native qualification uses immutable Snap Firefox 158.0 build `20261009003443`, headless Linux, a fresh private profile per trial, test-only localhost CA trusted only in those profiles, HTTPS/WSS with certificate verification enabled, and real WebDriver BiDi input actions. No DOM mock input or production RFB debug handle is used. Test-only in-memory instrumentation reads the displayed canvas and network counters. It is neither an agent tool nor a production API.

- View-only: deliberately disabling only the client `viewOnly` flag still produced no native key/pointer change; native TigerVNC flags were independently inspected.
- Take/Release: real UI actions replaced the exact workers, preserved the original absolute deadline, blocked existing MCP/Web input, and allowed input again only after verified release.
- Keyboard: arrows, Enter, Escape and Backspace exercised actual Kodi window/control changes. Back is context-dependent; Backspace on Home has no visible navigation. Ordinary typing visibly produced `Phase2 test` in Kodi's virtual keyboard; the Web typing field appended ` web`. Independent X11 PNGs show the resulting buffer. Kodi's GetLabel did not expose the edit value and is not used as evidence of text acceptance.
- Pointer: independent native X11 observations verify movement, held/released mouse button and dragging. Firefox wheel events and upstream two-finger pan/tap gestures were exercised as simulations. Physical laptop touchpad and mobile touchscreen compatibility remain NOT TESTED.
- Focus/expanded view: an unfocused held key did not reach X11. Expanded viewer height increased from 720 to 808 pixels. Native browser fullscreen was unavailable in headless execution; expanded fallback passed.
- Clipboard/primary selection, desktop resizing and file-transfer facilities remain disabled/unprovided. No audio was implemented.

## Native lifecycle and security

Evidence includes authenticated browser login, server view-only rejection, owner isolation, CSRF/origin denial, invalid rate rejection, agent conflict/read-only behavior, disconnect with held input, logout/cookie reuse, actual ten-minute expiry, Web/broker pauses, broker SIGKILL/restart, and compound native worker SIGKILL.

The ten-minute session expired without extending its original deadline. At the bounded observation 4.58 seconds after deadline, its exact exec had exited 0, the socket/ownership were gone, keys/buttons were neutral, and agent input resumed. This observation includes the test's intentional three-second post-deadline wait and follow-up queries; it is not an exact revocation-latency measurement.

Disconnect cleared actual held key/button state. Logout retired ownership and rejected reused cookies with HTTP 401. A test fixture reduced only the isolated Firefox session expiry; the real WebSocket lost authority, native input/ownership were cleared, and cookie reuse returned 401. After authentication expiry the client conservatively retained its “Stopping” message even after independent server cleanup was proven; a fresh login/page recovers the UI. Temporary Web/broker loss independently expired workers and recovered. Broker crash cleanup took approximately 3.28 seconds after the restart observation began; normal release was faster.

Compound SIGKILL of TigerVNC plus its helper left a held X11 key/button and an inert private socket, with no live worker. Ownership remained revoking and agent input was rejected. Exact native Stop retired ownership and reset input through runtime termination. Stop does not purge inert filesystem socket entries from the stopped container's writable layer. The qualification harness removed only the exact verified fault artifact after a fresh native incarnation, while holding the native worker flock and proving neutral input/no worker/no owner. Final container deprovision removes its remaining filesystem. This exceptional recovery requires Stop; do not claim transparent recovery from every uncatchable crash.

## Performance and memory

Reference Phase 1G: approximately 1.996 displayed FPS, maximum frame gap 508 ms, menu p95 398 ms, peak incremental Firefox RSS 231.01 MiB and PSS 122.95 MiB. CPU and agent-latency gates passed then; a stable active-memory ceiling did not.

Phase 2 uses the same fixed 2 FPS target, no renderer/rate tuning, provisional 256 MiB peak incremental browser RSS, diagnostic 128 MiB incremental PSS, continuous whole-process-tree accounting fenced by PID/start time, and independent host/native safety monitoring. Browser memory is retained and measured through disconnect and document closure without forcing GC. RSS/PSS monitoring and threshold-triggered native Stop belong to the isolated qualification harness. Ordinary Web pages cannot measure Firefox PSS; shipped protection consists of bounded buffers/workers and the authoritative session deadline. These tests do not establish a production-enforced client RSS cap. The native active budget is 1,800 seconds across all trials. Host aborts are CPU >80% for ten consecutive seconds, available RAM below 2 GiB, OOM, browser RSS allowance breach, or budget exhaustion.

The 707-sample ten-minute/static lease trial peaked at +179.87 MiB RSS and +66.24 MiB PSS using its prespecified 15-second blank medians. Its final 30-second closed-document stage still measured about 939.54 MiB RSS versus a 915.20 MiB blank baseline. These bounded observations do not establish immediate memory release or a stable ceiling. The completed motion/reconnect follow-up peaked at **+239.23 MiB RSS** against the 256 MiB allowance and **+121.74 MiB PSS** against the diagnostic 128 MiB target. Both numerical comparisons pass; neither demonstrates a stable ceiling. The matched agent/broker pair uses the unchanged compound failure rule: both >20% and >100 ms p95 growth. View-only agent p95 increased from 195.76 to 282.84 ms (+87.09 ms), passing the compound rule. Interactive agent p95 reached 323.06 ms (+127.31 ms, +65.03%), **FAIL**. Broker ping passed in both modes. This result is retained; there is no latency retest to chase a pass. A separate bounded follow-up covers only the memory/reconnect checks left unreached by that early stop.

Eight actual Firefox menu changes were displayed, p50 369 ms and p95/max 540 ms. This human-input path is not the identical JSON-RPC trigger used in the earlier monitoring baseline. The initial strict motion probe recorded 20 distinct displayed IDs/19 transitions, 516 ms maximum gap and 2.01 Hz inter-frame cadence, but two invalid samples caused its zero-invalid assertion to stop the trial. The focused integrated continuation waited 1.2 seconds for initial painting before starting the steady-frame probe; geometry, cap, renderer, memory limits and source were unchanged. This is a harness measurement correction, not rate tuning.

Six independent X11 observations after actual Firefox actions bounded pointer delivery at p50 123.39 ms/p95 188.90 ms and keyboard delivery at p50 117.90 ms/p95 190.10 ms. These include Docker exec/query/IPC overhead, so they are upper bounds on event delivery, not exact physical-device latency. A Firefox wheel sequence changed Kodi Home selection from TV to Weather; physical touchpad acceptance is still NOT TESTED. Supported native Stop/Start changed StartedAt, stopped the old controller, rejected the old viewer pin and left independent screenshots and existing Web input available. A different physical container ID replacement was not exercised.

The first long-lived read-only ROI observer stopped on an unsolicited X11 MappingNotify, so its source-to-display measurements are unavailable; the displayed-canvas timing remains valid. The observer and experimental screenshot parser were subsequently corrected to handle bounded MappingNotify events.

## Independent review

OpenCode used the existing configured model/credentials through an isolated read-only profile. The global configuration and credential files are preserved and compared before/after. Reviews are static evidence, not claims that a reviewer ran tests or browsers. Initial findings drove conditional ownership cancellation, ambiguous-dispatch fences/recovery, verified delayed-exec arming, default-app Stop recovery, lock/event-loop corrections and read-only proof separation. Native qualification found and fixed the rejected-Start cancellation defect. Baseline reconciliation identified a final overly broad pre-dispatch fence classification; its narrow correction passed 23 Kodi-adapter and 22 Web remote tests, focused Ruff/mypy, and an independent follow-up with no concrete P1/P2. Generic rejected replies after possible delivery remain fenced.

The human-only decision and explicit test-harness exception are part of the review requests and findings. Review logs and all prior findings/followups are retained.

## Validation and limitations

The exact source candidate is `c5e5a616d36785e69c213fb774762f76a8f63c28`. Final app images were built from that frozen source manifest (build8); earlier native functional receipts use preceding candidate snapshots with the same pinned r4 helper and vendor. The final change only distinguishes proven pre-dispatch remote failures; it did not alter rendering/input transport or runtime assets. Native image validation acquired and validated the r4 runtime through the supported sealed recipe path, including two validation cycles.

Final source qualification is recorded in the release ledger below. New/materially changed test files were run separately during focused development, with directly affected interaction coverage. Focused evidence is not a substitute for the full release gate. No physical mobile/laptop device, IME, Safari, Chromium, WSL2 or native desktop fullscreen acceptance is claimed. Browser memory remains a bounded evaluation. Audio requires a separately authorized phase.

## Measured native results

| Trial | Continuous samples | Blank RSS/PSS MiB | Incremental peak RSS/PSS MiB | Result |
|---|---:|---:|---:|---|
| 600-second static controller | 707 | 915.20 / 421.48 | 179.87 / 66.24 | Bounded checks PASS |
| Matched motion/agent stages | 130 | 917.24 / 422.60 | 219.09 / 107.08 | Agent latency FAIL |
| 100-second motion + five reconnects | 254 | 916.35 / 422.69 | 239.23 / 121.74 | Bounded checks PASS |
| Input and incarnation recovery | 68 | 918.35 / 424.77 | 192.05 / 72.15 | Bounded checks PASS |

Peaks include both independent continuous samples and explicit stage samples. Summed RSS counts shared mappings more than once; PSS apportions them. Both use each run’s original 15-second blank baseline. No forced GC or immediate-release assumption was used.

During the 100-second ordinary motion interval, PSS went from 497.10 to 530.37 MiB (OLS slope 0.311 MiB/s). Five stopped-cycle PSS medians were 542.11, 543.75, 539.23, 513.73, 516.74 MiB. Natural reclamation occurred, so reconnect accumulation was nonmonotonic. Twenty seconds after native Stop the last PSS was 514.39 MiB; after closing the document and waiting another thirty seconds it was 438.85 MiB (RSS 944.55 MiB). The browser then exited. This is evidence of retained allocations, not a demonstrated ceiling.

| Displayed-frame probe | Window FPS | Inter-frame FPS | Max gap ms | Invalid samples |
|---|---:|---:|---:|---:|
| perf2-motion-fps-before | 1.9920 | 2.0202 | 556 | 0 |
| memory1-fps-before | 1.9942 | 2.0276 | 507 | 0 |
| memory1-fps-after | 1.9956 | 2.0439 | 554 | 0 |

These short probes support approximately 2 Hz; they do not prove every displayed gap across the full soak or a guaranteed ≥2.000 FPS.

| Matched agent stage | Agent p50/p95 ms | Broker p50/p95 ms | Host CPU mean | Combined server working set MiB |
|---|---:|---:|---:|---:|
| perf2-motion-off-agents | 152.90 / 195.76 | 0.59 / 0.77 | 45.85% | 534.49 |
| perf2-motion-view-only-agents | 156.45 / 282.84 | 0.69 / 3.39 | 48.06% | 556.65 |
| perf2-motion-interactive-agents | 180.37 / 323.06 | 0.60 / 0.65 | 50.73% | 557.70 |

Each matched stage contains eight existing MCP read-only UI inspections and ten sequential broker pings. Nearest-rank p95 with eight samples is exploratory and is not screenshot-heavy agent qualification. The interactive failure remains a failure under the prespecified rule. Server working-set increments were +22.16 MiB view-only and +23.21 MiB interactive, below the unchanged 160 MiB budget.

perf2-motion-view-only-agents: Firefox CPU averaged 4.17% of one core; WebSocket payload averaged 81.62 KiB/s in and 0.063 KiB/s out. Rates omit TLS/IP overhead and agent HTTP traffic.

perf2-motion-interactive-agents: Firefox CPU averaged 6.22% of one core; WebSocket payload averaged 76.05 KiB/s in and 0.039 KiB/s out. Rates omit TLS/IP overhead and agent HTTP traffic.

Across recorded trials, minimum available RAM was 11681.16 MiB and peak sampled host CPU was 97.48%. No ten-sample CPU threshold breach, OOM or resource safety abort occurred. Conservative native active accounting was 1657.00 seconds against 1,800. The separate installation watch also passed its bounds. [Memory/resource reduction](evidence/reduced-memory-resources.json), [stage components](evidence/reduced-stages.json), [network](evidence/reduced-network.json), [CSV trend](evidence/browser-trend.csv).

## Attempts, cleanup and review disposition

There were 17 runs using the final bounded Trial harness: 7 completed and 10 stopped early. Earlier smoke receipts are separate. Failed attempts are preserved: wrong proxy pin and harness closure/canvas/focus/scroll assumptions; an overly strict helper-death expectation; an inert socket from the deliberate crash fixture; the strict initial frame oracle; and the genuine interactive agent-latency failure. Product defects found during development (rejected Start canceling the viewer, capture geometry/MappingNotify handling and pre-dispatch fencing) were corrected, focused-tested and independently reviewed. Failures are not silently reclassified as passing complete trials. Valid subcase receipts from early-stopped runs are identified individually.

Native finalization observed 186 exact exec identities. Historical Docker metadata for 153 had expired/unavailable inspection records, so their final exit codes are unknown; the exact stopped container proves no live process remained. Individual earlier normal-stop receipts retain exit/neutral-input proofs. All 234 sampled browser PID/start identities were gone. Recovery archives preserve the isolated catalog, native profile and logs before supported deprovision.

Exact cleanup removed 77 disposable containers including the native target, 17 candidate/native images, 48 proven new build-step images, two volumes and one network. No prune occurred; two pre-existing cached steps were preserved. All original 271 containers retained identity/configuration/lifecycle, production Web/broker/repository stayed healthy with zero restarts, schema stayed 0086, workspace generation 18 and sixteen diagnostic captures were preserved, and 42 operation tables remained idle. Previous evidence, main/original prototype worktrees, global OpenCode configuration/credentials, normal Firefox registry and production ingress were checked unchanged. Test MCP credential reuse and all collected retired test cookies returned 401. Final audit details remain authoritative.

OpenCode baseline reconciliation found one last P2; its focused closure found no concrete P1/P2 in the correction. The full static review confirms human-only admission, no agent VNC capability, server-side mode authority, verified handoff, retained uncertain delivery and default-disabled wiring. This is not a native/performance endorsement. Independent review artifacts are included alongside their limitations.

## Recommendation

Keep the draft unmerged and the viewer disabled by default. The human-control implementation and bounded safety behavior are reviewable, and the owner-approved RSS/PSS comparisons pass this finite evaluation. **Native performance qualification remains FAIL because of interactive agent latency.** Resolve or explicitly review that failure, then obtain physical-device/native desktop acceptance before recommending broader production use. Stable browser-memory behavior remains unresolved. Do not start audio development until owner review and separate authorization. Issues #18 and #96 remain open.

## Release qualification ledger

The complete release qualification was attempted once and resumed from blocked stages; the unfiltered suite ran **once**. Original failures remain in the evidence. A clean single-invocation **RELEASE PASS is not claimed**. Achieved check level is **FOCUSED PASS**, supplemented by the completed release stages and full-suite evidence below.

| Check | Result |
|---|---|
| Full-tree Ruff format/lint | PASS, repeated after discovery correction |
| Whole-tree strict mypy | PASS, 787 files |
| Standalone historical r3 and current r4 mirrored helpers | PASS, two files each, separate strict-mypy invocations |
| Exact broker/schema contract guards | PASS, six cases after expected-table correction |
| Unfiltered full suite on `ead3b71` | **6,022 passed, 38 skipped, one failed** in 2,115.55 s; no rerun |
| Full migration module after correcting stale table expectation | **112 passed**, plus focused Ruff/mypy; 55.4 s lane |
| Foundation module after metadata contract update | **18 passed**, plus focused Ruff/mypy |
| Package build | PASS |
| Alembic history | PASS; head `0087_novnc_control` |
| Package/config import | PASS |

The initial invocation on `af80ac9` stopped before tests because recursive mypy discovery treated preserved r3/r4 standalone scripts as duplicate importable modules. The narrow discovery correction excludes only those immutable asset directories; canonical helpers remain checked, and each mirrored helper/guard pair separately passed strict mypy. OpenCode found no substantive issue in that correction.

The next exact schema guard and the full-run empty-database upgrade assertion each omitted the two newly added table names. Both expectations now explicitly include `novnc_sessions` and `managed_remote_guards`; the foundation and full migration modules passed individually afterward. No application, helper, recipe, vendor or migration bytes changed after native source `c5e5a61`. Post-test stage source is `8e7a482`; differences from the native manifest are only mypy discovery configuration and those two test expectations, plus documentation/evidence.

The 38 skips are preserved with individual reasons in [release-summary.json](evidence/release-summary.json); they include opt-in host/image/repository/LAN acceptance. They are not silently counted as passes or replaced by claims of clean-host, DinD, other-browser or physical-device qualification. The explicit native r4 acceptance campaign is separate evidence.

Every resumed command after its admission was identical to the corresponding original release-script command. No pytest test selection, marker, runtime threshold or timeout was loosened. The full run’s failure remains visible even though its stale assertion was corrected and the entire affected migration module passed. [Release stage/case summary](evidence/release-summary.json), [resume provenance](evidence/release-post-admission.json).

Qualification-only browser/measurement sources, including retained failed-attempt variants, are archived in [qualification-harness-sources.tar.gz](evidence/qualification-harness-sources.tar.gz). They depend on a retired disposable fixture and contain no credentials, grants, private catalog/profile state or production agent integration. Normal agents must continue using existing MCP screenshot, Bridge inspection and remote-control tools; this archive is not a normal-use viewer facility.
