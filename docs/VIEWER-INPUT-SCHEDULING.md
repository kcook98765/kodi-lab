# Viewer input scheduling — 2026-11-13

**Broker scheduling corrected; overall reliability remains PARTIAL.**
Issue [#214](https://github.com/kcook98765/kodi-lab/issues/214) retains the separate
Web binding-check, helper startup and historical Nexus acceptance gaps. This work
does not repeat the adopted [Piers RC2 qualification](PIERS-RC2-QUALIFICATION.md).

## Baseline and correction

Fetched main and installed Claw began at
`a8ac577cc5b11209dd3447b41f48c1ebf5fd1682`, tree
`227db2503d6a46aa30c5a27979cfd6e8e5af8590`, schema `0014_human_reset_bridge`.
Live Web and broker each matched 1,348 packaged files. Three services were
healthy, four managed profiles stopped, and seven lease/reservation tables empty.
Claw had 22.4 GiB free disk and approximately 12 GiB available RAM. The correction
is source-only; installed Claw and main are unchanged by this investigation.

`DockerViewTunnel.run` drained every complete envelope from one Web socket read
before returning to its selector. Each interactive envelope performs a fresh,
synchronous authority check. Many individually short checks could therefore
delay helper reads/writes, helper renewals and cancellation for several seconds.
The existing 1 MiB backlog bound limits bytes, not accumulated validation time.

Process one buffered envelope per outer loop turn. Return to helper I/O and
existing deadline/cancellation checks between envelopes, including when no new
Web data arrives. Preserve FIFO input, fresh authorization for every interactive
envelope, trusted-only heartbeats, all bounds/deadlines and native cleanup proof.
No helper, runtime recipe, Bridge, watchdog or lease timeout changes.

The regression uses forty coalesced envelopes and 30 ms checks. On the exact old
transport, first output and bounded cancellation fail; on corrected source all
three cases pass, including revocation before the third input. Slow individual
checks and excess queued traffic can still exhaust existing leases or backlog
bounds and must fail closed. This change does not guarantee arbitrary input rates.

## Two bounded native sessions

This native comparison used source head
`859a8579969b6446c5ac6425efd0e9f665224b3c`. The buffered-heartbeat follow-up below
has socket-level validation only; these native results are not a rerun on it.

One independently owned, network-none disposable Omega 21.3 container reused
cached approved image
`sha256:7f779626cd56fb3bf86e14e39657fee5e8c5299a8f6f7abf59449a1163e7aa8e`.
The actual sealed helper matched
`be90f0c00e36bcbe4e7d905c2a7c3d0ab6645cca42fa5b5ef830e67d338775db`.
No runtime acquisition or original profile/application-state mount occurred.

A native RFB test client supplied fifty coalesced pointer envelopes with
interleaved trusted heartbeats and injected 90 ms into each exact-container
authority check. It saved decoded native PNGs, never RFB/input payload logs.
This deliberately stressed broker scheduling; it did not reproduce the complete
authenticated Web/browser/ownership workflow or ordinary human input rate.

| Evidence | Old transport | Corrected transport |
| --- | --- | --- |
| Actual native pixels | Yes, Kodi startup screen | Yes, native Home, subsequent frames and visible menu movement |
| Longest individual validation | 95 ms | 94 ms |
| Longest broker loop interval | 4,783 ms | 185 ms |
| After burst | Helper EOF, exit 0; next framebuffer request timed out | All input drained, native framebuffer received after 6,499 ms; fresh key changed native pixels |
| Ending | Native helper proof initially unconfirmed by controller; container subsequently stopped, exit 0 | Explicit disconnect, helper exit 0, native lock/input cleanup proof passed; container stopped, exit 0 |

The first controller configured its proof exec without attached stdout and failed
an immediate completion assertion. Preserve that failure; its precise probe
failure was not captured. The second and final session used the production
probe's attached-stdout behavior. It reused the same owned QA container after a
stopped state; no third connection, automatic reconnect or production write.
The QA profile's native Web server startup warning is outside this transport-only
test; no native JSON-RPC, Bridge or full managed readiness PASS is inferred.

## Causal status and remaining evidence

| Finding | Status |
| --- | --- |
| Input-batch starvation | **Confirmed**, deterministic sockets and native before/after evidence; narrow correction validated |
| Prior Omega 5,488 ms loop / 501 ms maximum validation / helper exit 0 | **Probable mechanism**, consistent with repeated checks in one batch; historical input/check count was not recorded, so attribution remains unproven |
| Web `binding_check` watchdog outliers | **Unproven cause**. Prior 924 ms ownership commit/`fdatasync` evidence concerns a different phase. Binding spans capability validation, private broker lookup and catalog authorization; terminal evidence does not isolate the slow subphase |
| Historical broker `web_lease_expired` | **Unproven attribution**. Independent of the Web watchdog in prior observations; this correction does not establish the cause of every lease expiration |
| Omega exit 1 before pixels | **Unproven cause**. Current broker aborts on the first stderr chunk; it can discard the actual exception after a traceback header |
| Unix socket pathname before listen | **Confirmed fixture race**, unproven connection to either native startup incident; no sealed asset patched or repinned |
| Original Nexus first viewer after Create | **Unproven**, not repaired by later successful Nexus/Piers viewing |

The smallest next diagnostics are terminal timing for the binding subphases and a
bounded teardown-only stderr classifier retaining only fixed exception/errno
categories, including incomplete/truncated status. Do not persist raw stderr,
paths or exception messages, forward further input, or renew a failed helper to
collect evidence. A helper correction requires a separately reviewed immutable
runtime identity and qualification; the fixture race alone does not attribute
the original incident. No binding-check or helper-startup correction is justified
by this campaign.

## Final review: buffered heartbeat freshness

The Web watchdog writes a trusted heartbeat only after authorization and durable
ownership renewal, then sleeps 250 ms. Browser RFB bytes cannot create that
envelope. This producer cadence does not make processing time equivalent to
receipt time: a coalesced read can contain several earlier heartbeats. Independent
durable ownership validation remains enforced; no authorization bypass is claimed.

Review of `859a857` confirmed a transport liveness defect. Eight buffered
heartbeats on otherwise quiet real socket pairs expired after 2.35 seconds of
controlled-clock time from receipt, beyond the unchanged two-second lease and
one 50 ms detection tick. Removing the selector wait fixed quiet-buffer progress
but still allowed 3.6 seconds when input checks delayed heartbeat processing.
These are deterministic socket regressions, not native timing observations.

Use a nonblocking selector turn while a complete buffered envelope remains, still
servicing helper I/O and one separately authorized input per turn. Record receipt
times and end offsets for pending read segments; retire metadata with consumed
bytes. A heartbeat renews from its first-byte receipt time, including across
fragmented reads. Unrelated later input cannot freshen old heartbeats, while an
actual newer heartbeat retains its own receipt time behind older queued input.
Partial envelopes keep the normal blocking wait. The protocol, backlog bounds,
lease durations, FIFO, cancellation and ownership/cleanup fences are unchanged.
These are broker receipt timestamps, not inferred sender-generation timestamps.
An overloaded stream can still expire if fresh trusted heartbeats cannot be
processed before their existing deadline. The earlier 6,499 ms native stress
observation cannot be credited as final-head acceptance under this tighter
accounting policy.

Nine real-socket cases cover quiet progress, silent peer expiry, delayed input,
unrelated later reads, genuinely fresh heartbeats, split heartbeats, disconnect,
cancellation and backlog refusal. Original PR head: six fail, three pass.
Nonblocking-wait-only candidate: four fail, five pass. Final correction: all nine
pass. **FOCUSED PASS: 28 cases; INTEGRATION PASS: 103 additional cases**, with
affected Ruff/strict mypy. No new native sessions, runtime changes, binding-check
instrumentation, stderr diagnostics, deployment or complete suite were needed.

## Validation and release boundary

Initial scheduling validation: **FOCUSED PASS:** 19 cases, affected Ruff
format/lint and strict mypy.
**INTEGRATION PASS:** 98 additional distinct cases covering real Unix sockets,
lookup capacity, state-owner transactions, ownership/recovery fences, Web
authentication/Origin/CSRF/tickets, human priority and catalog/mutation interaction.
The final regression was separately run against the exact baseline transport:
two expected failures and one passing revocation control. Repeated cases across
lanes are not additional coverage. No complete suite was run; the historical
8,799-test gate is not a regression result for this source.

Original managed profiles, credentials/authority, private source, uncertain Create
history, runtime/helper/Bridge identities, unrelated Docker resources and mounted
NAS remain preserved. The one new QA container is retained stopped for review;
no production deployment, merge, release, tag or issue closure occurred.

Viewer reliability is **not yet sufficiently qualified for initial release**.
Review this correction, then address the binding/startup diagnostic gaps in a
separate bounded task before another native attempt. Issue #305 remains separate:
after viewer qualification, use one inert original ZIP delivery, actual native
Install from zip file, exact installed payload hashes and enabled ID/version
readback. Delivery/replay alone cannot satisfy installation acceptance. No #305
installation was attempted here.

Private evidence, exact attempt IDs, PNGs, logs and preservation inventories:
`/home/kyle/kodi-lab-deployment/viewer-targeted-20261113/`.
