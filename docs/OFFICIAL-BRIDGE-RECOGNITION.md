# Official Bridge recognition — local implementation and verification

The user-facing path is one repository and one Bridge: add the configured
`http://192.168.2.191:18120/install/` source, install
`repository.kodi-lab-1.0.0.zip`, then install **Kodi Lab Narrow Bridge** from
**Install from repository → Kodi Lab → Services**. Enable the add-on and its
Enable bridge setting, restart Kodi locally, open Configure → Pair Kodi Lab,
obtain the four-character OTP and use Add Kodi. Home and Development share these
instructions and retain separate onboarding coordinators.

## Verified mismatch

The live repository and remote Web containers are pinned to the October 4
candidate with source commit `fd6f99b73bdae113aa21651bf2cf30cf5710f41a` and bundle
digest `d3e0ebd0bbfe362c7a3b9f434c3700042152cf1a52e70c4806e2b0009434a519`.
Its actual ZIP, BRIDGE.json, STABLE.json and live index agree on **1.1.14**,
ZIP SHA-256 `bf0b4f441c310f0621458792ccf4d05227576086186c65843fb66e5da6811832`.
The copied production database contains no reviewed-build rows. It was inspected
only through a disposable DB/WAL/SHM copy; no live records were changed.

The checkout at `3ff38c45d5ac3bb99e75a7736122b8f88e3810d2` consistently declares
**1.1.15** in the add-on, protocol source, deterministic builder, stable manifest,
repository release pins and backend production compatibility policy. The preview
runs this checkout. This is an older deployed release, not a mislabelled ZIP or an
inconsistent source version. Removing manual review alone cannot make 1.1.14
compatible with the current Home backend.

## Existing release provenance and automatic recognition

`scripts/build_narrow_bridge.py` uses a closed ten-file inventory and deterministic
ZIP metadata. Provenance generation requires clean committed source and binds
source commit/tree/Bridge tree, builder digest, per-file inventory, semantic
fingerprint, version, protocol, bootstrap and exact package digest in BRIDGE.json.
`scripts/stage-linux-v1-bundle` copies STABLE.json and builds that artifact before
finalizing and verifying the existing deployment bundle. The stable generator
cross-checks those inputs, emits per-install repository ZIP/index/checksums and
installed PROVENANCE.json, and selects a generation atomically. Publishing is an
operator-authorized deployment action; no new distribution mechanism is added.

At Web startup, `recognize_official_bridge` requires the existing repository root,
configured base URL and deployment-pinned `KODI_LAB_REPOSITORY_BUNDLE_SHA256`.
The existing `load_current` verifies generation, index, package bytes/tree,
checksums, bootstrap ZIP, origin and bundle field. The application's compiled
`CURRENT_BRIDGE` exact ZIP hash is the artifact authority, including when an
unofficial feed is internally self-consistent. The fingerprint comes from that
same release pin, and capabilities come from the verified package. Recognition
inserts an existing immutable ReviewedBridgeBuildRow, attributed to
`kodi_lab_official_repository`, binding bundle/generation/ZIP identity. It needs
no schema, API, credential, OTP or permission changes.

Lookup and insertion share a SQLite writer transaction. Repeated/concurrent
startups insert once. Both semantic identity and ZIP uniqueness conflicts are
checked; existing manual, narrower, conflicting and retired records are left
unchanged. Only the build registry receives the insertion. Missing or invalid
artifacts leave existing trust untouched and log an unavailable status. No
fallback approves remote discovery. Page viewing never registers a build.

Future compatible releases update the same existing builder/STABLE/repository/
backend release pins as today. Startup registers the corresponding verified
official release without manually entering a new build record. An older backend
does not accept arbitrary future versions. The native labels are explicitly
bounded to 19.5, 20.5, 21.3, 22.0 and the curated 22.0-beta.2 label; no new wildcard
version policy is introduced.

Production's exact supported Bridge version, protocol, OTP, authenticated full
identity, capabilities, transport consent and provenance checks remain intact.
Existing target authority does not expand when a build is registered. Development
keeps its existing authenticated compatibility/custom-build behavior; selecting
Development grants no deployment permission. Managed lifecycle trust is unchanged.
Advanced manual build registration and retirement remain available.

## Local staging and limits

A clean detached worktree at the expected HEAD produced the staged official
1.1.15 ZIP, SHA-256
`c894ca88c491edc716997aab3d7a91f2ee30722eff8d7a75b68233ddec9c3bf7`, fingerprint
`b125a428b12cb48b3a93d2c9009478803b7bd65c85b2fdc632e0b644f2abc4c2`.
An independent read-only reviewer compared every archive file to Git HEAD and
checked the recorded builder digest. These bytes match the existing release pins.

The local repository-input fixture and isolated generated feed/database demonstrate
existing generation verification and automatic registration. They are explicitly
labelled **validation only**, and are not a complete application deployment bundle.
No Docker images, live feed generation, production service or target was changed.
The new backend implementation remains uncommitted; a complete release candidate
must be built from its subsequently approved committed source, then separately
authorized for backend deployment/repository publication. The live feed still
serves 1.1.14. No compatibility check was bypassed.

The installed provenance's bundle digest is compared to deployment configuration;
startup does not independently hash the actual BUNDLE.json or attest its claimed
source fields. The known exact ZIP digest supplies the artifact trust anchor.
Clean source/build provenance is independently verified during release staging.
The public semantic fingerprint and OTP/HMAC identity are not proof of remote
installed package bytes against a malicious implementation. Stronger remote
attestation would need a separate architectural proposal.

## Files changed in this task

Earlier uncommitted Web work is preserved. This task updates these paths:

| Area | Files |
| --- | --- |
| Automatic recognition | `src/kodi_lab/application/official_bridge_builds.py` (new), `src/kodi_lab/repository/stable.py`, `src/kodi_lab/web/app.py` |
| Existing trust documentation and messages | `src/kodi_lab/storage/models.py`, `src/kodi_lab/application/external_bridges.py`, `src/kodi_lab/application/household_onboarding.py` |
| Normal onboarding presentation | `src/kodi_lab/web/templates/add-kodi.html`, `src/kodi_lab/web/static/add-kodi.js` |
| Focused tests | `tests/application/test_official_bridge_builds.py` (new), `tests/web/test_official_bridge_startup.py` (new), `tests/web/test_add_kodi_installation.py` |
| Browser checks | `tests/web/add_kodi_installation_browser.cjs`, `tests/web/kodi_views_browser.cjs` (optional per-device selector) |
| Documentation | `docs/DECISIONS.md`, `docs/V1-SPEC.md`, `docs/WEB-ADD-KODI-INSTALLATION.md`, `docs/OFFICIAL-BRIDGE-RECOGNITION.md` (new) |

## Verification results

**FOCUSED PASS for the implemented slice:** the three new/materially changed
feature test files run individually: 19 official-build tests, five startup tests
and 14 installation tests. Each has focused Ruff and strict mypy coverage.
The selected Web, household, production, development, managed trust/lifecycle,
repository and Bridge protocol cases total 302 passing cases. The existing
Bridge metadata file also has one stale README assertion; its remaining 53 cases
pass. The historical candidate suite was stopped after two fixture failures;
the broad gate below subsequently covered it until the watchdog stopped that run.

All changed Python source files pass strict mypy. Repository-wide Ruff formatting
and lint pass. Forty JavaScript files pass syntax checks and both existing
onboarding JavaScript harnesses pass. Browser review passes at 1440, 820 and
375 px: 73 screenshots and 66 zero-violation axe checks cover both onboarding
modes, copy/disclosure/keyboard behavior, mocked pairing and recovery, legacy
pairing, and empty/connected/disconnected/attention list/detail fixtures.

**RELEASE validation is incomplete:** `./scripts/check` ran once and reached the
900-second pytest timeout (exit 124) in historical repository transition tests.
It collected 3,791 tests and reached approximately 73 percent. Earlier observed
failures include outdated migration fixtures, a stale Bridge README assertion
and a portable-artifact test still expecting migration 0059 instead of 0071.
Representative failures were reproduced on untouched expected HEAD; the portable
migration actually succeeds before its obsolete revision assertion fails.
The standalone global strict mypy run reports 21 errors in five untouched test
files; the clean HEAD reproduces exactly the same 21 diagnostics. No unrelated
fixtures/lifecycle code were changed and no RELEASE PASS is claimed.

Independent read-only provenance/trust review passes without blocking findings.
It verifies exact staged package/source/builder identity and preservation of
retirement, conflicts, serialization, compatibility, permissions and managed
trust boundaries. The preview's 110 non-session tables have identical before/
after row counts and hashes, with zero targets and zero reviewed-build rows.

Logs, baseline comparisons, browser reports, screenshots, staged inputs and the
independent review are under the evidence directory below. The combined browser
run for Kodi fixtures was interrupted; complete per-device runs subsequently pass
for every required scenario and viewport, recorded in separate reports.

## Preview and evidence

The preview has no repository data/bundle pin, so startup does not create build
records there. Keep its state unchanged; populated pairing and trust tests use
isolated fixtures. Both preview services remain running.

- Preview: `http://192.168.2.191:8180/add-kodi`
- Development selected: `http://192.168.2.191:8180/add-kodi?mode=development`
- Backward-compatible pairing: `http://192.168.2.191:8180/pair-kodi`
- Kodi overview: `http://192.168.2.191:8180/kodi`
- Evidence: `/home/kyle/kodi-lab-web-preview/evidence/official-bridge-recognition/`

Sign in and expand **Install Kodi Lab Bridge**. Review both modes, copy the source
URL and expand **Advanced compatibility details**. For this read-only visual
review, leave connection/recovery forms unsubmitted and perform no Kodi operation.
