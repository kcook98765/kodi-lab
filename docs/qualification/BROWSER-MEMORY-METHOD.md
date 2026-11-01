# Proposed staged Firefox memory qualification

This testing method separates ordinary browser/page overhead from viewer cost.
It does not approve a revised threshold. Preserve every original failed trial.
The standard 128 MiB RSS gate and incomplete native qualification remain visible.
No runtime, permission, lifetime, production Web or schema change is part of it.

Use `scripts.browser_memory_qualification.TreeSampler` with the Firefox root PID
and `/proc/PID/stat` start time returned by the existing native BiDi adapter.
The qualification adapter must spawn Firefox with Node `detached: true`, making
Firefox its own process-session leader. This only changes harness process
ownership; keep all browser arguments, preferences and security settings fixed.
One sampler owns one fresh browser for its entire lifetime. The external driver,
Python observer, native Kodi and broker are outside that tree; record their
resources separately. Browser-internal BiDi allocations remain included.
Do not change Firefox process count, site isolation, cache, GC, sandbox or TLS
verification. Retain the original launch/build/profile/navigation identities.

## Fixed sequence and boundaries

Mark stages **before** issuing actions, and sample continuously at 1 Hz through
navigation, import, Start, Stop and browser close. A single serial sampling thread
owns the sampler; do not race foreground and background reads. Retain timestamps
for action start/end separately. Baseline observation windows start after action
completion. All transient/action samples count toward trial peaks and budgets.

| Stage | Observation | Boundary |
| --- | --- | --- |
| blank | 25 s clean `about:blank` | At least 15 samples; original baseline is final five of first 15 |
| neutral | 25 s ordinary minimal HTTPS | Fixed neutral document, no viewer or application scripts |
| page | 25 s authenticated selected Current Kodi, viewing off | Same browser, no intermediate home/standalone navigation |
| module | 25 s explicit `import('/static/vendor/novnc/core/rfb.js')` | No RFB constructor, canvas, connection or retained diagnostic decoder |
| cycleN-start | Through Start and connection handoff | Verify 640×360, compression 0, quality 6, view-only |
| cycleN-idle | 120 s stationary Kodi | Retain actual displayed-frame measurements |
| cycleN-motion | 180 s bounded navigation/motion | Independent MCP actions; no human control |
| cycleN-stop | Through normal Stop and proven native worker termination | Browser and original Current Kodi document remain open |
| cycleN-stopped | 30 s recovery | No replacement tab, reload, forced GC or rebasing |
| closing | Through normal browser close | Keep sampler alive after root exit |
| exited | 5 s after exit | No owned live identities, including observed reparented children |

Use three bounded Start/Stop cycles total per trial, and at most three fresh-profile
trials. Each active session remains below the existing 600 s deadline. Do not renew
or extend its deadline. One disposable native Kodi at a time; use the supported
exact candidate composition and lifecycle. These windows are a bounded study,
not evidence of long-term stability or proof that a leak exists or does not exist.

Baseline medians use final five samples of the fixed windows; never adaptively
choose a cheaper point, reset the page baseline between cycles, or discard early
peaks. Before Start, inspect the last ten page samples for RSS/PSS drift and process
transitions. If unsettled, retain the trial as inconclusive rather than silently
warming longer. The original blank metric always uses `/proc/status` RSS and its
original final-five-of-15 convention, even when the blank window lasts 25 s.
The summary helper uses samples 11–15 of `blank` even for a longer blank window.
This keeps the historical baseline reproducible.
The first 15 nominal 1 Hz observations must span at least 13.5 s, allowing 0.5 s
of ordinary timing jitter around the nominal 14 s span. This cadence tolerance
does not change the selected samples or any RSS limit; retain actual timestamps.

## Accounting and reporting

`TreeSampler.sample(stage)` records Unix wall time, monotonic read start/end,
per-process PID/start identity, parent, process state, RSS from `smaps_rollup`,
RSS from `status`, PSS, individual VmHWM, and individual read intervals. It expands
the descendant census again after reads to include newly created children.
Observed descendants remain tracked after reparenting or root exit. A census of
the dedicated Firefox session also includes previously unobserved children that
survive their parent's exit. The sampler refuses a shared session. PID reuse in
the identity tracker cannot transfer ownership. New ownership is never inferred
from a bare parent PID in a non-atomic census. New session membership must be
corroborated by a direct check that the original root is still alive. After root
exit, previously observed identities stay tracked; an unobserved session member
is ambiguous and fails collection rather than trusting a recycled session ID.
A browser child that creates
its own new session before its first observation could still evade polling;
cross-check the run's exclusive profile environment/arguments at final cleanup.
Do not claim full cleanup if this independent process census is unavailable.
Only call `sample(..., closing=True)` for intentional closing/post-exit phases.
Ordinary sampling requires the original nonterminal root before and after each
collection, even when no new child is discovered.
Verified terminal `Z`/`X` processes have no live address space; retain their states
and identities separately from live identities. A zombie is not an unreadable live
process or proof of reaping. Full cleanup also requires no unreaped identities.
Live unreadable processes and an unstable census fail
sampling; missing memory must never become zero. File/identity races caused by
exit are recorded. Verify the browser root is alive during every pre-close stage;
an unexpected exit fails the trial even if a subsequent sample has zero memory.

RSS counts shared mappings in each process; PSS is diagnostic, not a substitute.
The tree read spans an interval and is not atomic. A complete process read just
before exit still contributes to that interval. If a process disappears after a
successful smaps read but before identity validation, reject the sample/trial as
incomplete; never reduce its peak by silently dropping that measured process.
Preserve complete rows. Failed collections raise `IncompleteCollection` with a
partial receipt containing every successful process read and interval, observed
subset sums, failure reason, and any unfenced partial process fields. Journal it
separately. These are incomplete observations, excluded from baselines and PASS,
not silent zeroes; report their observed values separately from complete-tree peaks.
Never sum individual VmHWM
values and call the result a simultaneous tree peak. Peaks are sampled at 1 Hz;
processes born and retired entirely between samples and subsecond allocation peaks
may be missed. Preserve collection duration and cadence gaps in the report.

Report absolute RSS/PSS stage medians and sampled peaks, plus these differences:

1. Historical all-stage peak `status` RSS minus original blank median: unchanged
   **128 MiB RSS** gate. Preserve FAIL regardless of proposed metrics.
2. Ordinary neutral minus blank, and loaded page minus neutral costs.
3. Module minus fixed pre-import page; all feature stages minus that same page.
   The proposed **128 MiB viewer-over-page RSS** comparison includes module and
   connection allocations. It is provisional and never formal acceptance.
4. Active minus module as a separate attribution diagnostic, without resetting
   the proposed feature budget to the more expensive module baseline.
5. Every post-Stop residual relative to fixed page and to previous stopped cycles.
6. Post-browser-exit owned identities and resources, plus prior peaks retained.

`summarize()` calculates stage values and both separate metric results from
timestamped `Point` records. It cannot establish stage completion, native identity,
page stability, FPS or cleanup by itself. Missing/failed stages remain incomplete
in the surrounding trial receipt, even if a numerical comparison is below budget.
`trend()` reports OLS slope and half-window median change. Apply it to each full
idle/motion interval and its final 40 s separately. The historical finite stability
criteria (slope ≤0.05 MiB/s and median change ≤3 MiB) may be evaluated provisionally
for both RSS and PSS; an owner must approve any revised acceptance contract.

## Safety and disposition

Keep the existing host CPU, 2 GiB available RAM, 20 GiB local free-space,
160 MiB incremental server, worker cleanup and exact-runtime lifecycle guards.
The diagnostic whole-browser envelope remains 2048 MiB RSS from the prior
investigation; this is an emergency cap, not a standard browser acceptance limit.
Abort on a proposed feature delta over 128 MiB, sustained unexplained growth,
worker instability or any existing safety guard. Record the failure before normal
Stop/close, with exact-identity emergency containment only if necessary.
Do not grant the historical 256 MiB experimental exception to standard support.

Retain raw JSONL, stage/action timing, process transitions, displayed FPS,
observer costs, source/image/recipe identities, all failures and cleanup receipts.
No active viewer memory optimization is justified by pre-Start browser overhead
alone. Formal threshold approval and remaining qualification stay separate.
