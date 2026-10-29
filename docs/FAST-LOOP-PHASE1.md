# Fast loop phase 1: measured deployment pipeline (#110)

Qualification date: 2026-10-28. Baseline `82b93a8`; Linux amd64 on the campaign host.

## Admission change

Managed disposable deployment admission changes from 300 to **5 seconds** per client/target. External development remains 300 seconds. The interval starts at admission; completed operations lasting at least five seconds incur no additional interval wait. Durable request replay, target/global-heavy reservations and unresolved-operation fences remain independent prerequisites. There is no caller interval override or automatic retry.

## Deterministic pipeline measurements

Command: `uv run --locked python -m scripts.benchmark_workspace_deployment --samples 5` (optional `--case small`). Exactly four source sizes, each initial deployment, unchanged redeployment and one same-size file edit. New private source/staging trees per sample; one process; no network or Docker daemon. **Simulated Docker transport:** real capture, fsync/staging, manifest, delta and verification code; in-memory tar generation adds simulator cost. These are not native Kodi deployment times.

Five samples per row; p95 is nearest-rank (the maximum with five samples), useful as a bounded observation, not a population latency guarantee. Sequential complete runs show filesystem variability; do not attribute all differences to telemetry.

| Source | Operation | Baseline p50 / p95 (s) | Instrumented p50 / p95 (s) | Transferred payload |
| --- | --- | ---: | ---: | ---: |
| small | initial | 0.115 / 0.136 | 0.357 / 0.387 | 1,048,576 B |
| small | unchanged | 0.119 / 0.121 | 0.363 / 0.378 | 0 B |
| small | one_file_edit | 0.115 / 0.120 | 0.349 / 0.373 | 21,396 B |
| typical | initial | 1.559 / 1.864 | 1.743 / 1.799 | 10,485,760 B |
| typical | unchanged | 1.592 / 1.857 | 1.778 / 1.831 | 0 B |
| typical | one_file_edit | 1.568 / 1.853 | 1.726 / 1.853 | 42,111 B |
| many_files | initial | 6.247 / 9.213 | 5.895 / 6.954 | 26,214,400 B |
| many_files | unchanged | 6.137 / 10.479 | 6.640 / 6.965 | 0 B |
| many_files | one_file_edit | 6.929 / 8.708 | 7.036 / 7.411 | 26,241 B |
| assets | initial | 2.776 / 3.227 | 2.726 / 2.858 | 104,857,600 B |
| assets | unchanged | 2.656 / 2.852 | 2.715 / 2.828 | 0 B |
| assets | one_file_edit | 2.694 / 2.826 | 2.729 / 2.850 | 421,115 B |

Source sizes: small 50 files / 1 MiB; typical 250 / 10 MiB; many files 1,000 / 25 MiB; assets 250 / 100 MiB. Counts include `addon.xml`; source total bytes are exact. One-file edits transfer one file only; unchanged operations transfer no payload.

To investigate the small-case sequential-run difference, a second bounded comparison alternated baseline/instrumented subprocesses for five pairs on the same 50-file case. Median initial times were 0.11579 / 0.11468 s, unchanged 0.09384 / 0.08966 s, edit 0.09049 / 0.09024 s. It found no measurable telemetry slowdown at this resolution; it does not establish a universal overhead ceiling.

Instrumented unchanged 100 MiB samples confirm 200 MiB of source payload hashed (two captures), 100 MiB staged, and 210,124,800 archive bytes consumed across two destination readbacks, with zero upload. No source/destination scan was removed in phase 1. A representative sample spent 1.926 s staging, 0.073 s confirming source, 0.239 s in before readback and 0.237 s in after readback. Phase 2 should measure both staging and redundant no-write readback before optimizing.

## Native Omega acceptance

A new random disposable target reused the exact installed/approved Omega 21.3 r4 image `sha256:8df4cbf1988514ac9713fab7702b0d5f04483f2a167ea95507d9ade66032403b`, recorded recipe digest `sha256:44ff250db8587265c74ecf7114a74f51646f7f25ab4199c29c3181c02d9f2d15`, runtime revision `9ee490cd-8eee-424b-b3b2-06c2757ccaa8`. Historical packaged-recipe resolution, image label, exact container/runtime, no mounts/ports and default `skin.estuary` were verified. Limits: 1 GiB memory, 1.5 CPU cores, 360-second run alarm, three iterations. No runtime acquisition or production service deployment occurred.

| Iteration | Copy/sync (s) | Start/readiness (s) | End to end including stop (s) | Payload |
| --- | ---: | ---: | ---: | ---: |
| 0 | 0.372 | 9.109 | 11.246 | 1,008,788 B |
| 1 | 0.137 | 6.912 | 9.127 | 275 B |
| 2 | 0.129 | 6.673 | 8.329 | 0 B |

All three cycles verified installed bytes/ownership, restarted the same exact container, observed native Kodi 21.3 and enabled inventory, and invoked the generated video plugin. The initial and edited code returned their expected fresh marker through a single directory item; cache-to-disc was disabled. Three Kodi starts, three stops. This proves this fixture execution only. It is **native adapter/RPC acceptance**, not authenticated MCP acceptance, live apply, service/script/skin qualification, Windows/WSL2, or functional acceptance of another add-on. End-to-end agent call counts are unmeasured because no agent client participated.

The first harness attempt stopped before Kodi startup because the installed Docker SDK did not accept the requested CPU-limit keyword. Its new container was archived and exactly retired, then the bounded test used the supported CPU period/quota arguments. This is not a deployment-path failure or a hidden retry of ambiguous addon mutation. Both receipts are retained.

After acceptance the newly created profile was archived and its exact container retired through the canonical adapter. All 339 original containers retained their IDs and runtime states; images, existing sessions/workspaces and production services were preserved. Private JSON measurements, operation-correlated logs, native receipts and before/after manifests remain with the campaign evidence.

## Verification and limits

Focused application cases cover five-second consecutive managed deployment, 300-second default external admission through actual service calls, denial replay, exact request conflict, racing admission, lease conflicts, recovery and identity drift. Broker cases retain two captures/readbacks, changed-only transfer, no-op, drift and interruption refusal. Measurement/logging cases cover failed phases, numeric byte counts, privacy, log-sink failure and packaged Web logging. Independent OpenCode review found no substantive blocker. Integration checks and final campaign release qualification are recorded in the PR.

Phase 1 telemetry is log evidence, not new persistent receipts. Early claim/recheck or staging-creation refusals use existing audit paths. Upload archive size is prepared/attempted size, not a claim about partial network sends; confirmed payload is separate. Readback counters count consumed SDK stream bytes. Failure phases retain available counts without inventing zeros for unobserved work. Source validation/generation, trust, leases and cryptographic destination verification are unchanged.

No native before/after throughput comparison, all-type activation, owned-file removal, crash fault injection, or cross-platform acceptance is claimed here. Umbrella #109 stays open until real managed fast-loop acceptance is complete. ZIP/repository testing remains a final release path.
