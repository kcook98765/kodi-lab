# Phase 5 curated Kodi runtime matrix contract

This executable supplement defines Phase 5 Step 1. `V1-SPEC.md` remains
authoritative. No second Kodi image or runtime is built or run by this step.

## Exact release and support identity

One RuntimeDefinition represents exactly one Kodi release:

- `19.5` — stable
- `20.5` — stable
- `21.3` — stable
- `22.0-beta.2` — Beta 2

Stable releases have no prerelease ordinal. Alpha, beta, and RC releases require
a positive ordinal, so Beta 1, Beta 2, RC 1, and stable are distinct. Rebuilds of
the same release are monotonically numbered RuntimeRevisions beneath the same
definition.

Support is independent catalog policy. Current is normally visible/selectable;
Legacy requires Include Legacy; Preview requires Include Pre-release; Retired
cannot supply new pins. Current may move to Legacy or Retired, Legacy to Retired,
and Preview to Retired. Retired is terminal in this contract. Channel-invalid or
reverse transitions fail. Kodi 21.3 and 20.5 are Current, 19.5 is Legacy, and
22.0 Beta 2 is Preview. When a Kodi 22 stable definition is later promoted under
the latest-two-stable-major rule, Kodi 20 moves to Legacy through a separate
explicit operation.

## Validation and eligibility

Validation evidence is append-only privileged metadata for one immutable
revision. `kodi-runtime-v1` requires registered artifact/profile, provision,
running container/process, JSON-RPC ping, exact application/release identity,
two independent successful start/readiness/clean-stop cycles without forced
fallback, unchanged protected legacy resources, safe deprovision, and verified
absence. Evidence registration does not perform those actions.

The newest record for the required contract determines effective state:
unvalidated when absent, validated when passed, and failed when failed. Other
contract versions remain historical and do not satisfy the current requirement.
Approval, Custom Build trust, validation, and support tier remain independent.

A new or reassigned pin is eligible only when:

1. Target and RuntimeDefinition exact releases match;
2. trusted selection options permit the definition tier;
3. the revision is approved;
4. Custom Build opt-in/confirmation is satisfied when required;
5. immutable artifact exists;
6. immutable execution profile exists and matches the definition release;
7. latest `kodi-runtime-v1` evidence passed.

Exact selection cannot bypass a gate. Newest-eligible examines only the requested
definition in descending revision order and chooses the first eligible revision.
There is no closest-version or cross-definition fallback.

## Historical pins and workflows

Existing ManagedInstances keep exact pins despite later approval, validation, or
support changes. They do not auto-upgrade and remain operable unless a future
quarantine policy explicitly says otherwise. Retirement deletes nothing.

Adding a release means: create its exact definition and initial tier, create an
immutable revision, register artifact and profile, approve, satisfy Custom Build
trust if applicable, execute validation, append passing evidence, explicitly set
the appropriate support tier, then expose it through an application-owned DTO.
Prereleases begin Preview. A stable candidate may be registered Current before
validation because the remaining eligibility gates keep it unselectable; no
fifth staging tier is needed. A stable release is a new definition, never a
promoted mutation of its beta/RC predecessor.

Withdrawal is an explicit support transition. New selection observes it
immediately; definition, revisions, provenance, artifact, profile, evidence, and
existing pins remain.

## Build and Web boundaries

Build directories remain version-owned (`runtime/kodi-<exact-release>/`). Shared
assets require later cross-version evidence; Kodi 21.3 is not refactored here.

A future Web DTO may display canonical/display release, channel/ordinal, support
tier, effective validation state, revision number, new-selection eligibility,
and a bounded ineligibility reason. The browser cannot submit image identity,
artifact authority, approval, support, or validation state. Phase 5 Step 1 adds
no matrix UI.

## Kodi 20.5 stable implementation

Phase 5 Step 2 adds only the version-owned `runtime/kodi-20.5/` build. It uses
the official Ubuntu Noble `2:20.5+dfsg-1ubuntu1` packages on the pinned
linux/amd64 Ubuntu 24.04 base and retains the minimal Phase 3 Xvfb, D-Bus,
software-rendering, seeded-profile, and process-health architecture. The image
adds no port, volume, mount, VNC, device, capability, or generic command surface.

Kodi 20.5 receives one local compatibility accommodation: its Kodi media font
path links to Roboto Thin from `fonts-roboto-unhinted`, because Estuary requests
that font while the Noble Kodi package does not own the media-font path. Kodi
21.3 remains unchanged.

The dedicated validation state is
`<validation-state>`. Its exact Current
definition/revision, immutable artifact/profile, approval, and initially absent
evidence must produce `unvalidated` before live work. Completion requires two
independent broker-mediated start/readiness/clean-stop cycles, safe deprovision,
post-removal and legacy-isolation proof, then one passing `kodi-runtime-v1`
evidence record referencing the retained structured report. No result is
inferred from the image build alone.

## Kodi 19.5 stable implementation

Phase 5 Step 3 adds only the version-owned `runtime/kodi-19.5/` build. It uses
the exact Team Kodi old-stable Launchpad PPA packages
`6:19.5+git20221224.1006-f8fdeb6b1b-0~jammy` on the pinned linux/amd64 Ubuntu
22.04 base. The manifest binds the PPA identity and signing fingerprint, both
downloaded package hashes, the exact base digest, copied-file hashes, build log,
and immutable result image. This source exception is local to Kodi 19.5 and
does not broaden package policy for other matrix members.

The image retains the minimal non-root Xvfb, bounded D-Bus,
software-rendering, disposable seeded-profile, and process-health architecture.
Read-only comparison found no Kodi-19-specific font link was needed. The fixed
Kodi JSON-RPC adapter accepts Kodi 19.5's additional `revision` response field
while still requiring exact application, major, minor, and stable channel.

The dedicated validation state is
`<validation-state>`. The exact Legacy
definition/revision, artifact/profile, and approval remain ineligible before
evidence: normal policy excludes Legacy, while Include Legacy reaches the
validation gate. Completion requires two independent broker-mediated
start/readiness/clean-stop cycles, safe deprovision, verified absence and
legacy isolation, followed by one passing `kodi-runtime-v1` evidence record.
After that evidence, normal policy must still reject the Legacy definition and
trusted Include Legacy must select its newest eligible revision without any
cross-version fallback.

## Kodi 22.0 Beta 2 Preview implementation

Phase 5 Step 4 adds only `runtime/kodi-22.0-beta2/`. It builds the exact Team
Kodi `22.0b2-Piers` tag at commit
`e513e0ff4331fc25fd2454659a9dd3e6b7670146` in a digest-pinned Ubuntu 26.04
multi-stage build. The downloaded source archive is hash-verified and the
manifest records the declared toolchain, CMake options, source and base
identities, copied assets, build log, and immutable result. Build tooling stays
outside the final image. Kodi's internal TagLib 2.3.1 is selected so the
source-owned Beta 2 Matroska compatibility patch is applied.

The image retains the bounded non-root Xvfb, D-Bus, software-rendering,
disposable seeded-profile, and process-health architecture. Exact readiness
requires JSON-RPC to report application Kodi, major 22, minor 0, tag `beta`, and
tagversion `2`; a response that omits or changes the ordinal cannot satisfy the
profile. The repository-owned source build uses the existing Trusted Custom
Build path, so Include Pre-release alone cannot bypass global opt-in or
per-revision confirmation.

The dedicated validation state is
`<validation-state>`. Before evidence,
normal policy rejects Preview and Include Pre-release reaches the remaining
trust/validation gates. Completion requires two independent broker-mediated
start/readiness/clean-stop cycles, safe deprovision, verified absence and
protected-resource isolation, followed by one passing `kodi-runtime-v1`
evidence record. Passing evidence must leave normal selection ineligible while
trusted Include Pre-release selects the exact Beta 2 revision; Beta 1, RC,
stable, and all earlier-version targets remain exact and cannot fall through.
