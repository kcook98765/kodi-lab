# Issue #380 validation

## Phase A: native diagnostics

Baseline: current main `3b7f1e423de878e8e7c5030fd3709a25133143a8`.
Implementation is in an isolated issue-specific worktree; the original checkout
and retained runtime profiles are unchanged.

**FOCUSED PASS**: explicit individual-file lanes cover the new parser, managed
MCP routing in both HTTP protocols, production/external adapters and authorization,
existing MCP/stdio catalogs, documentation inventory, production helper and
UI-observation regressions. Ruff formatting/lint and affected strict mypy pass.
The final broad focused lane completed in 139.9 seconds; the review-correction
lane (production cadence and helper catalog) completed in 39.3 seconds.
No full/release suite was run.

Native collector/transport verification passed on Linux Kodi Omega **21.3**,
using cached reviewed image
`sha256:7f779626cd56fb3bf86e14e39657fee5e8c5299a8f6f7abf59449a1163e7aa8e`
in a fresh disposable container/profile with no published ports, no mounted
retained profiles, and an isolated internal Docker network. Exact Kodi identity
was checked before and after collection, and the MCP response-size guard passed.
This is native transport/collector evidence; it is not deployment or end-to-end
acceptance against an existing registered managed or external target.

The runtime returned 51 available fields out of 54. GPU temperature, HDR types
and DHCP address were explicitly not reported. CPU text normalized to indexed
core readings; native storage/memory, default-drive scope, network, uptime and
power data remained available. Source compatibility was reviewed against Matrix
19.5, Nexus 20.5, Omega 21.3 and Piers 22 RC2; additional native versions were
not acquired or claimed accepted.

The initial disposable harness attempts used incomplete Kodi settings; the
successful harness uses the project's version-1 native-settings format. These
setup failures did not change implementation policy or any existing target.

Private evidence is retained on Claw under
`/home/kyle/kodi-lab-deployment/issue380-20261118/`, including focused logs,
`phase-a-native.json` and `phase-a-native-receipt.json`. Metrics may contain
private addresses, so raw snapshots are excluded from the repository and PR.

Review confirmed fixed server-owned requests, no caller-selected labels/paths,
no Bridge dependency, bounded raw fields/core maps, no aggregate inference from
per-core CPU text, production rate limiting, fresh native identity, exact-target
grants, result suppression on revocation and no changes to list/status polling.

Independent review corrections: publication now rechecks grants, client state
and current credentials for managed and External Development diagnostics as well
as production. Previously only production diagnostics received the final durable
authority check. Managed transport outages now retain `target_unreachable`, and
malformed JSON-RPC error codes remain typed `invalid_kodi_response` errors.
Regression tests reproduced these failures before correction. Independent
**FOCUSED PASS** covers 67 cases across individually run publication, transport,
parser, application and MCP files, plus affected Ruff and strict mypy.
Private independent evidence is in
`/home/kyle/kodi-lab-review-evidence/380-independent-20261118/`.

## Phase B: optional reviewed Bridge enrichment

Phase A remains independently reviewable in PR #381, commit
`8e9f93bfea6c3fb9686eef7630d4bcc0aa4e967b`. Phase B uses a dependent branch/PR.

**INTEGRATION PASS**: final explicit lane completed in 29.5 seconds, with
59 individually focused cases and 34 combined application/storage/MCP interaction
cases. Across the affected individual-file runs, **820 passed / 9 existing gated
skips** in 40 selected files. Current-candidate/package correction checks also
passed on the final Bridge bytes. Final Ruff formatting/lint covers 64 affected
Python/script files; strict mypy covers 62 Python modules. No full/release suite
was run; these are source/PR qualification results, not release qualification.

Covered behavior includes exact integer bytes above 2^53, partial/missing/invalid
facts, Windows direct-API fallback without command execution, fixed profile
translation, empty-input/authentication/replay policy, no cached metric content,
closed response shape, old Bridge capability fallback, managed binding and
revocation, production reviewed-build retirement/identity/transport refusal,
publication reauthorization, both MCP protocols, audit privacy, and migration
row/trigger preservation with refused lossy downgrade. `system_snapshot` adds
only one read-only item to the existing owner-opted reviewed-update allowlist;
`system_info` itself never invokes adoption.

Review found and corrected an inconsistent Windows branch shape, a fixed future
version used by the reviewed-update test, and a current-Bridge migration fixture
that needed the new capability schema. The historical direct-predecessor checks
remain. Snapshot transport now checks a six-second total budget between chunks,
with socket reads capped at one second; other Bridge operations keep their
existing transport policy.

Final native verification passed on the same cached Linux Omega **21.3** image,
in a second fresh private profile/container using candidate **1.1.35** ZIP:
`cf7bc763e8c5c15c82233d1904abd34d528ee65eee147317b288ccf3325a5a79`.
Real Kodi service discovery/explicit enablement, `otp-1` bootstrap/commit,
authenticated `system_snapshot`, exact approved build identity, all eleven
installed inventory hashes, pre/post native identity and the MCP size guard
passed. All seven supplemental facts were present; native metrics were preserved.
The exact-release preview **1.1.36** archive is reproducible with SHA-256
`e64fe6d857ff50e3d1788d675065465f67798ce38114b680d50e9982c36bae82`;
Piers native acceptance was not run. Windows behavior is unit-tested, not native
Windows acceptance.

The immutable 1.1.33 and 1.1.34 predecessor archives retain their original SHA-256
values `ab0738d30f11bdaf1f458d9608a832686b1cd723c0bfa80ae70ec8717efdee71`
and `a77f9aa00ff94dbf1e3d9bc983beb25e923393f3b9ad1551458d16c2b3264867`.
Existing historical inventories are preserved, with additive predecessor pins.

Private final evidence includes `phase-b-integration-settled.log`,
`phase-b-candidate-settled.log`, `final-static-checks.log`,
`phase-b-native-final.json` and `phase-b-native-final-receipt.json` in the evidence
root above. Earlier candidate/test failures remain as development evidence and
are not pass claims. No existing registered-target grants, credentials, profiles,
production services, external installations or public Bridge artifacts changed.

Before distribution/deployment, independently run the appropriate release gate
on the settled integration tree, build clean committed-source provenance, review
migration/recovery for the destination and explicitly authorize publication or
installation. Source preparation/native disposable-runtime checks do not establish
acceptance on existing registered managed, external-development or production Kodi.

## Independent review and qualification

Independent Phase A corrections remain on PR #381 and are incorporated into the
dependent Phase B tree without rewriting the original implementation commits.
The review also corrected six stale current-head migration assertions and the
packaged migration inventory. Direct-predecessor populated production checks
preserve every application table, pairing, reviewed build, policy, adoption
receipt and trigger through upgrade/downgrade/upgrade, with integrity and foreign
key checks. Both pairing capability columns independently prevent a lossy
downgrade. These migration/packaging paths passed 17 individually focused cases;
the existing migration implementation required no correction.

The combined migration/MCP lane also exposed audit-test logger state inherited
from Alembic logging configuration. The new MCP test now explicitly enables its
captured logger, following existing audit tests; diagnostic content stays out of
audit records. The failure and corrected interaction run are retained separately.

All 54 native requests were independently compared with the four cited upstream
versions. Unmapped HDR, DHCP and webOS observations retain the documented
partial/unsupported-platform limits. Retained native Omega snapshots and all
eleven installed candidate file hashes were verified against the unchanged
Bridge release inventory. No additional native runtime or acquisition was needed.

Independent focused, combined integration, clean-source candidate provenance and
the single settled-tree release-gate evidence are retained under
`/home/kyle/kodi-lab-review-evidence/380-independent-20261118/`.
The final review report there records the exact qualified commits/tree and gate
outcome; this document does not imply a release pass before that gate completes.
