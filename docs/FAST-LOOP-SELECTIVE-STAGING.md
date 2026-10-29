# Selective staging for the fast loop (#111, first slice)

Baseline is merged #114 (`c62476c`), measured on the same Linux host. Only materialization changes: the first capture hashes and safely retains bounded `addon.xml`; after complete destination preimage/provenance inspection, the second capture hashes every source member and stages/fsyncs only new/changed files. Full canonical source comparison and both complete destination readbacks remain. Existing capture callers retain their default full staging.

## Simulated pipeline comparison

The unchanged deterministic benchmark runs five samples per source/operation (p95 nearest-rank, hence maximum of five). Source sizes and transport limitations are documented in [phase 1](FAST-LOOP-PHASE1.md). Sequential runs include filesystem variability and simulator tar-generation cost. Initial deployment still stages the whole tree; no general initial-deployment speedup is claimed.

| Source | Operation | Full staging p50 / p95 (s) | Selective staging p50 / p95 (s) |
| --- | --- | ---: | ---: |
| small | initial | 0.357 / 0.387 | 0.096 / 0.130 |
| small | unchanged | 0.363 / 0.378 | 0.020 / 0.021 |
| small | one_file_edit | 0.349 / 0.373 | 0.022 / 0.022 |
| typical | initial | 1.743 / 1.799 | 1.729 / 1.849 |
| typical | unchanged | 1.778 / 1.831 | 0.110 / 0.162 |
| typical | one_file_edit | 1.726 / 1.853 | 0.122 / 0.123 |
| many_files | initial | 5.895 / 6.954 | 6.655 / 7.229 |
| many_files | unchanged | 6.640 / 6.965 | 0.425 / 0.466 |
| many_files | one_file_edit | 7.036 / 7.411 | 0.382 / 0.452 |
| assets | initial | 2.726 / 2.858 | 2.449 / 2.702 |
| assets | unchanged | 2.715 / 2.828 | 0.645 / 0.674 |
| assets | one_file_edit | 2.729 / 2.850 | 0.672 / 0.713 |

100 MiB unchanged: 200 MiB source payload still hashed, 210,124,800 SDK-stream archive bytes still consumed, **0 staged bytes** (previously 100 MiB), 0 transferred payload. One-file asset edit stages/transfers 421,115 B; many-file edit stages/transfers 26,241 B. Counters distinguish planned staging from successfully completed staging; after an interrupted capture, missing completed-stage metrics mean unknown, not zero. A completed second capture with changed source reports its actual selected sizes before canonical comparison rejects mutation.

## Native confirmation

The same approved Omega r4 image, default Estuary, new random disposable target and bounded 1GiB/1.5-CPU/360s procedure from phase 1 passed initial/edit/no-op cycles again. No existing runtime/session was reused or changed.

| Iteration | Copy/sync (s) | Start/readiness (s) | End to end including stop (s) | Payload |
| --- | ---: | ---: | ---: | ---: |
| 0 | 0.349 | 8.275 | 10.332 | 1,008,788 B |
| 1 | 0.124 | 6.717 | 8.504 | 275 B |
| 2 | 0.080 | 6.707 | 8.361 | 0 B |

Installed bytes/modes/ownership, enabled native inventory, native identity and generated plugin result markers passed. This is video-fixture native adapter/RPC evidence, **not authenticated MCP**, live-update, service/skin or Windows/WSL2 acceptance. The run still performs three Kodi starts/stops and has no measured agent-call count. Readiness dominates these small native iterations; do not apply the simulator speedup factor to native end-to-end time. New profile archived and exact new container retired.

## Safety and remaining scope

Focused regressions cover unselected unsafe members and changed source, bounded descriptor capture, partial materialization preserving the full manifest, nested new files, execute-mode-only edits, exact prior drift and interrupted application. Operation/recovery/application integration remains covered. Independent OpenCode source review approved; its staged-byte finding was corrected with a specific rejected-capture regression.

This slice does **not** remove obsolete destination files or replace types. Destination-only retention remains explicitly tested; source materialization must not be reported as exact mirroring when retained members exist. Full owned-file mirroring remains #111 work. No filesystem/Bridge capability, runtime image, grants, receipt schema or recovery semantics were expanded. The confirming full source scan now falls between destination preimage and apply; Docker provides no atomic filesystem compare-and-swap, and externally privileged Docker writes remain an architectural race to be handled by the unchanged complete post-verification and fail-closed reconciliation.
