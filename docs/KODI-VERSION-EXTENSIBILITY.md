# Managed Kodi version extensibility audit

## Reviewed recipe update (2026-09-28)

Normal selection now resolves a wheel-owned generation recipe. Matrix 19.4 /
Jammy is installed through a closed signed-distro-APT provider, and its exact
local image is registered as a new immutable runtime revision. The existing
four-archive matrix described below remains private historical support.

Nexus 20.5 / Noble and Omega 21.3 / Resolute would add reviewed recipe data,
exact critical package identities, compatible fixture packages, and real
acceptance evidence. They would use the same installation table, catalog
revision/artifact/profile/evidence records, eligibility policy, restricted
broker lifecycle, and generation selector. Piers 22 would follow the same
pattern after a provider is reviewed; the provider field permits another
closed type without a persisted schema change. No Piers provider or runtime
is implemented here. ARM64 needs a new reviewed platform recipe and validation
before the current linux/amd64 constraint can be widened. Windows Docker
Desktop and Intel macOS Docker Desktop can use this linux/amd64 recipe in
Linux-container mode once their Docker access and filesystem boundaries are
tested; acquisition is independent of the physical host distribution.

This is a read-only architectural inventory made before the Linux V1 clean-host
installation work. Kodi 23 was not added and Kodi 19 was not retired. The current
canonical lab remains 19.5, 20.5, 21.3, and 22.0 Beta 2.

## Adding a normal Kodi 23 release

| Class | Required change | Current location and reason |
| --- | --- | --- |
| A: reviewed per-version data | Build and review an amd64 Kodi 23 runtime image, profile, launch/health scripts, exact image ID, immutable `docker save` archive, archive hash/size, and two-cycle validation evidence. | `runtime/kodi-*/`; `src/kodi_lab/runtime/catalog/`; `src/kodi_lab/runtime/evidence/`. The wheel carries manifests and evidence, while the archive ships separately. |
| A: reviewed catalog identity | Allocate a definition UUID and revision UUID/number and declare support tier, exact release, artifact identities, execution profile, and evidence digest. | `runtime/curated_catalog.py` pins reviewed predecessor/suppression lineage; `runtime/distribution.py` lists reviewed names and tiers. These are review gates, not broker lifecycle branches. |
| B: compatibility | Build and audit Kodi 23 fixture seeds and video database schema if the existing packages are incompatible. Add exact fixture manifests, database audit rules, and semantic scan expectations. | `src/kodi_lab/fixtures/{package.py,seed_audit.py,*/manifest.json}`, `kodi/fixtures.py`, and fixture tests. `application/reset_status.py` currently contains fixture-specific display notes. No migration is automatically required; add one only if persisted schema changes. |
| C: canonical matrix | Add selector `23`, a stable human name, default membership, and stable UUIDv5 identity. Add version-specific acceptance and operator documentation. | `runtime/lab.py`, `tests/runtime/test_lab_workflow.py`, `docs/MANAGED-LAB-WORKFLOW.md`, and the Linux installer. `domain/kodi_release.py` already parses arbitrary positive major/minor releases. |
| D: application coupling | Add the exact reviewed revision to navigation and screenshot allowlists after those capabilities have been tested. | `kodi/remote.py` has `REMOTE_RUNTIME_REVISION_IDS`; `kodi/screenshots.py` has `SUPPORTED_SCREENSHOT_RUNTIME_PROFILES`. These are safety gates in application code rather than manifest/catalog capabilities. Their adapters remain generic. |

Also update `tests/runtime/test_distribution.py`, `tests/runtime/test_kodi_image_contract.py`, `tests/kodi/test_remote.py`, `tests/kodi/test_screenshots.py`, fixture tests, relevant application/Web tests, and installed four-version acceptance scripts and documentation. Run the normal focused-to-full gate. A new major release must not be inferred compatible merely from a version number.

The generic broker provision/start/stop/inventory operations, managed network,
readiness coordinator, snapshot capture/restore, screenshot byte conversion,
catalog repositories, add-on ZIP transfer, and lab start/stop/status flow do not
branch on Kodi 19/20/21/22. A normal Kodi 23 addition should not edit them.

Two small order/name assumptions were removed during this phase: curated
Trusted Custom Build validation now follows the declared Preview tier, and
bootstrap selects the explicitly acknowledged Kodi 22 manifest by name instead
of assuming the last runtime is Kodi 22. The explicit
`--ack-preview-kodi22` ceremony remains tied to the current canonical matrix;
a future Preview runtime requires a separately named acknowledgement.

## Retiring Kodi 19 from the default lab

Removing selector `19` from the current `SELECTORS` tuple would conflate the
default matrix with historical identity and would break the positional pairing
with `load_curated_runtimes()`. A future retirement should:

1. Keep the Kodi 19 manifest, evidence, exact archive identity, definition and
   revision UUIDs, `canonical_id("19")` mapping, and historical fixture packages.
   Existing ManagedInstance, Target, Snapshot, and lifecycle records keep their
   original IDs and revision pins; no row is deleted or rewritten.
2. Split **default canonical membership** from **known historical selectors**.
   Make empty `bootstrap`, `start`, `stop`, and `status` use the new default set;
   allow explicit inspection/control of an existing Kodi 19 instance under its
   existing identity. Bootstrap must not recreate Kodi 19 on a fresh install.
3. Decide the support policy explicitly. If Kodi 19 becomes `retired`, use the
   monotonic `RuntimeCatalogAdministrationService.transition_support_tier` path
   and test how existing pins are inspected or stopped. Do not edit the old
   wheel manifest: its digest is historical revision provenance. Current
   `bootstrap_catalog` compares the definition exactly and would reject a
   `retired` stored tier, so its repeat/idempotency rule needs a narrow policy
   update before that transition. Keep image bytes available for any permitted
   historical recovery; backup does not include Docker images.
4. Test upgrades of a populated four-version state, repeat bootstrap, Kodi 19
   snapshot listings/restore eligibility, lifecycle history, and clean three-
   version installs. Never reuse Kodi 19 UUIDs, slugs, or revision numbers.

## Capability declaration inventory

| Capability | Current declaration | Kodi 23 implication |
| --- | --- | --- |
| Execution identity, endpoint, UID/GID, bytecode, process observation, readiness timeouts | Wheel manifest `catalog.profile`, persisted `RuntimeExecutionProfile` | A: declare and validate profile. |
| Preview and Trusted Custom Build | Manifest tier and per-revision confirmation flag, explicit installed bootstrap acknowledgement, persisted confirmation | A/C: stable Kodi 23 needs no Preview acknowledgement; a preview needs a new explicit ceremony. |
| Fixture/reset and schema | Exact packaged fixture manifest plus compatibility release, seed audit, explicit adapter list | B: add compatible package/schema audit only after proof. Generic reset flow is unchanged. |
| Snapshot/restore | Catalog revision/fixture pins and snapshot manifest; generic broker and application flow | A/B: prove compatibility; no version branch expected. |
| Add-on ZIP deployment | Generic validated ZIP and execution profile/readiness | A: acceptance proof, no version branch expected. |
| Navigation/input | Exact revision allowlist in `kodi/remote.py` | D: add reviewed revision after input proof. |
| Managed screenshots | Exact revision/profile allowlist in `kodi/screenshots.py`; generic broker X11 capture and PNG validator | D: add reviewed revision after visual proof. |
| Mediated live display | Exact Kodi 21.3 profile allowlist, authenticated WebSocket, screenshot backend | D only if Kodi 23 display is reviewed; otherwise no change. |

Future cleanup should store exact per-revision navigation, screenshot, and
display claims with reviewed catalog/profile evidence, then have the current
allowlist users read that single authority. It must preserve fail-closed exact
revision checks and should not introduce a second free-form capability system.
The current bounded tables are release work, but not a generic broker or lab
orchestration dependency.
