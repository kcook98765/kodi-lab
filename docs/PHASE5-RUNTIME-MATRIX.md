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
