# Issue 375 validation and operator handoff

This source change adds `log_scan`; it does not install a Bridge or change a
running Lab or Kodi target. See [the interface and consistency contract](KODI-LOGS.md)
and decision D-0502 in [DECISIONS.md](DECISIONS.md).

## Large-log fixture evidence

The generated fixture in `tests/kodi/test_kodi_logs.py` contains **77,201,718
bytes** across two files, with 77,827 logical records per file. Each file has
known ERROR records at the beginning and end and a FATAL record in the middle.
Creation writes a reused small block; test teardown removes the generated files.

On Claw / Ubuntu 24.04, the final parser scanned both files in **10 calls** and
about **1.4 seconds** of Bridge-engine work (excluding MCP/network and
authorization overhead), returning exact counts of three matching records per file
(two ERROR, one FATAL). With four samples requested, the largest cumulative scan
payload was **3,188 bytes**. Samples were explicitly truncated while counts
remained exact. These are local fixture measurements, not native performance
guarantees for an external Kodi device.

A separate isolated run with Python allocation tracing also took 10 calls:
**7.239 seconds** with **239,957 bytes** of peak traced scan allocations. That
measurement starts after fixture creation/imports, includes response models and
retained page results, and measures Python allocations rather than process RSS
or OS file cache. The fixture was removed afterward. The scanner never requests
more than 64 KiB in a read; per-call scan/verification work is capped at 8 MiB.

## Validation scope

**FOCUSED PASS:** 1,048 passed and nine normally gated root-only cases skipped
across 55 explicitly selected test files, each run individually. Ruff formatting
and lint, strict mypy for changed application/test/migration paths, and wheel
build/resource verification passed. This is not a complete release-gate result.

The focused checks exercise the real authenticated Bridge handler/client over
loopback, managed trust consumption, external development/production routing,
MCP schemas/discovery/dispatch, grant revocation, identity mismatch, closed input,
and exclusion of log bodies from audits and replay storage. They also cover
zero/dense matches, serialized escaping limits, cumulative continuation, UTF-8,
oversized lines, tracebacks, local timestamps with space and `T` separators,
unknown dates, unsorted records, appends and digest verification, rotation,
truncation, replacement, inode reuse, writes during reads, descriptor disposal,
idle expiry, shutdown and bounded admission.

The migration tests use the direct predecessor `0014_human_reset_bridge`, retain
existing pairing/reviewed rows and exact trigger SQL, exercise current real
Bridge pairing on migrated SQL, and prove refusal of a lossy downgrade.

Packaging checks cover deterministic stable/overlay ZIPs, exact release
inventories, source-free bundle verification, repository transitions and official
build recognition. Historical 1.1.31/1.1.32 ZIPs are retained byte-for-byte, with
their old digest pins and full inventories. Explicit managed replacement tests
prove both the successor installation and preservation of the prior tree.

Independent review found the initially omitted 1.1.32 replacement inventory;
it was corrected and rechecked. Additional partition checks exercised 2,000
input splits, and timestamp review covered separator/fraction/thread variants.
Review also reproduced inode reuse after unlink/recreation. The final scanner
retains bounded identity descriptors, including idle and shutdown cleanup.
Independent checks covered 100 completed scans without descriptor growth, 50
unlink/recreate attempts, and disposal of active anchors on shutdown. The final
review found no remaining actionable implementation defect.

The built wheel includes the scanner, new migration and retained manifests.
Both reviewed source-resource loaders were exercised from an isolated extracted
wheel and verified all eleven stable/overlay files against their pins.

## Acceptance still requiring an operator

No actual managed or external Kodi was used for native acceptance. No production
services, profiles, Bridge installations, credentials or grants were modified.
The complete release gate has not been run; the implementation is submitted for
review without claiming merge/release qualification. Root-only packaged ownership
transition tests remain normally gated; no elevated test run was attempted.

Before enabling scans on production targets, qualify the settled release, deploy
the updated Lab with migration `0015_log_scan_capability`, then use the existing
operator-controlled installation and trust-verification procedure for reviewed
Bridge **1.1.33** (exact-release overlay **1.1.34** where applicable). Production
still requires exact reviewed-build admission, current provenance and transport
acknowledgement, and the existing target inspection grant. Managed installation
remains an explicit lifecycle action. An older Bridge keeps `log_read` and returns
capability-unavailable for scans; updating Lab alone does not enable this feature.

Record native current/previous-log scans and append/change behavior separately
for each tested runtime and target type. Leave issue #375 open until its native
acceptance criteria are genuinely satisfied.
