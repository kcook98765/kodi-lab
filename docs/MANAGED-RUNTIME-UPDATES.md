# Updating managed Kodi runtimes

Use this procedure for patch/RC updates, RC-to-final transitions, new Kodi majors,
and accompanying reviewed Narrow Bridge changes. Start from current main and the
latest qualification evidence; an upstream release does not upgrade installed profiles.

## Quick-start checklist

Keep a small issue-scoped worktree and private ledger. For each step, retain the
exact identities, result and next supported action before continuing.

| Step | Required evidence / typical failure to catch |
| --- | --- |
| 1. Inspect support and installed profiles | Fetch main, PRs and issues; record source/tree, installed package/schema, live Compose images/mounts, runtime revisions, containers/profiles, occupied slots, leases, protected state and capacity. Deployment pointers can be stale. |
| 2. Verify the official release | Record official tag, resolved source commit, release channel/ordinal and source version. An announcement or Windows binary does not establish Linux availability. |
| 3. Choose Linux acquisition | Find a supported provider for the exact architecture/release. Reuse the reviewed method when suitable; do not substitute a nightly to overcome a missing artifact. |
| 4. Freeze provenance | Retain signed metadata, signer/key identity, immutable artifact/commit, hashes, build manifest and dependencies. A moving ref may still select the old release. |
| 5. Update recipe/locks | Add the reviewed revision and propagate its digest through acquisition, assembly, handoff, attestation and admission. A stale comparison can reject a correctly downloaded release. |
| 6. Verify runtime compatibility | Check native version, Python interpreter, Kodi system API/ABI, libraries, renderer/display and execution profile. Do not infer API versions from the Kodi major. |
| 7. Review Narrow Bridge | Compare reviewed source/build selection and exact native identity; prove resident authentication. Installed files or stored trust alone are insufficient. |
| 8. Run focused source checks | Test changed files and affected contracts, Ruff/mypy as applicable, and negative old-artifact/new-label cases. Fixtures are source evidence. |
| 9. Qualify isolated native behavior | Run the core matrix below on an independently owned profile/environment. Preserve receipts; classify native limits explicitly. |
| 10. Review and reconcile | Review the actual diff/security implications, labels/docs and issues; merge passing work within authorization. Keep historical reports factual; close only proven scopes. |
| 11. Plan production adoption separately | Review occupancy, uncertain receipts, backup/recovery and replacement authority. A source merge or Reset is not an installed-version upgrade. |

## Verify release and Linux artifacts

Record the discovery URLs and verification results once; resolve an identity chain
from Kodi source through packaging to the actual native executable.

| Evidence | What it establishes |
| --- | --- |
| Official Kodi tag/source | Intended version/channel and resolved source revision; record signature verification where available. It does not identify every packaged binary. |
| Official release binary/package | Publisher, supported OS/architecture, exact version and published checksum/signature. A Windows artifact is not Linux evidence. |
| Distribution build | Distro suite, package epoch/version/architecture, signed indexes, dependency closure, hashes and any patches. Its version string can differ from Kodi's canonical release. |
| Immutable signed commit | Verify the object hash, trusted signer and metadata bindings, then embedded source/build manifest and critical payload hashes. A signature alone does not prove the desired Kodi release. |
| Mutable branch/ref | Discovery hint only. Resolve and verify an immutable object; never let a later ref move silently change an approved recipe. |
| Nightly/development snapshot | Not a substitute for the requested release. Mark exact-release acquisition blocked if verified artifacts are unavailable. |

Current providers are closed in [recipes.py](../src/kodi_lab/runtime/recipes.py):
`SignedDistroAptProvider` and `FlathubOstreeProvider`. APT acquisition/assembly uses
[`assemble_reviewed_image()`](../src/kodi_lab/runtime/assembly.py), signed metadata
and recipe-bound critical packages; OSTree uses
[`OstreeAcquirer.acquire()`](../src/kodi_lab/runtime/ostree.py). Choose based on
verified availability, not an assumption that future Kodi versions use Flathub.
If a new provider is required, review that bounded change separately.

**RC2 case study:** the [official tag](https://github.com/xbmc/xbmc/releases/tag/22.0rc2-Piers)
resolved to `d686683a55d50e576dd282f53fb2bf9914931b38`.
[Flathub manifest commit](https://github.com/flathub/tv.kodi.Kodi/commit/0bae0f46fbdd6a0b2600b341c077f09dc58e4bcf)
`0bae0f46…`, [published build 340397](https://hub.flathub.org/status/659036),
identified signed app commit
`83b803ab50c19471377943f35bed0d96247a425834580a420d5842912ce7cbdd`.
The beta ref still returned RC1. Selecting the verified immutable RC2 commit
worked without following that stale ref. Key/subkey, collection/ref bindings,
metadata SHA256, executable, embedded manifest and metainfo were checked.
See [RC2 provenance](PIERS-RC2-QUALIFICATION.md#verified-linux-provenance) for the
full hashes; do not copy them into a future release's recipe.

## Update code and compatibility

Use this map to trace the current implementation before editing. Search old release,
recipe and digest references with `rg`; classify each as current selection,
retained admission or historical evidence. Do not globally replace version strings.

| Concern | Current source to inspect |
| --- | --- |
| Canonical identity and execution | [`KodiReleaseIdentity`](../src/kodi_lab/domain/kodi_release.py), [`RuntimeExecutionProfile`](../src/kodi_lab/domain/runtime_catalog.py); release, tag/ordinal, process/readiness and runtime-user expectations must agree. |
| Recipe selection/history | [runtime/recipes/](../src/kodi_lab/runtime/recipes/) and [recipes.py](../src/kodi_lab/runtime/recipes.py): `_RECIPES`, `_DISPLAY_CANDIDATES`, `load_installation_recipe()`, `load_recorded_reviewed_recipe()`, closed provider/release validators and pinned digests. |
| Acquisition/locks | RC2 [schema-2 input](../src/kodi_lab/runtime/recipes/piers-22.0-rc2-r1.json) versus [schema-3 installation recipe](../src/kodi_lab/runtime/recipes/piers-22.0-rc2-installation-r4.json); [ostree.py](../src/kodi_lab/runtime/ostree.py) signature, critical inventory and `_application_release()` checks; [ubuntu_support.py](../src/kodi_lab/runtime/ubuntu_support.py) `load_support_closure()` and `fetch_support_packages()`. |
| Assembly/worker/handoff | [assembly.py](../src/kodi_lab/runtime/assembly.py), [ostree_worker.py](../src/kodi_lab/runtime/ostree_worker.py) `assemble_in_worker()`, [ostree_context.py](../src/kodi_lab/runtime/ostree_context.py) `verified_context()`, [ostree_assembly.py](../src/kodi_lab/runtime/ostree_assembly.py) `load_assembly_assets()` / `assemble_ostree_image()`; [assembly-assets.json](../src/kodi_lab/runtime/assembly_assets/piers-22.0-rc2-r4/assembly-assets.json) and each asset's hash. |
| Approval/admission | [preview.py](../src/kodi_lab/runtime/preview.py) `PreviewAcknowledgement`, [Create installation](../src/kodi_lab/application/create_kodi_installation.py), [Create registration](../src/kodi_lab/application/create_kodi_registration.py) `require_create_runtime()`, [managed runtime admission](../src/kodi_lab/application/managed_runtime_admission.py) `require_managed_runtime()`, [runtime eligibility](../src/kodi_lab/application/runtime_eligibility.py), [lifecycle](../src/kodi_lab/application/runtime_lifecycle.py) and broker [reviewed installation worker](../src/kodi_lab/broker/reviewed_installations.py). |
| Cache/container identity | [recipes.py](../src/kodi_lab/runtime/recipes.py) `recipe_target_id()` derives a release-specific target; [install.py](../src/kodi_lab/runtime/install.py) `install_reviewed_runtime()` binds registration/ready state to an exact image; [ostree_installation.py](../src/kodi_lab/runtime/ostree_installation.py) checks provider evidence; [attestation_integrity.py](../src/kodi_lab/runtime/attestation_integrity.py) seals/verifies it. Keep definition, revision, image ID and actual container identity distinct; tags are not trust inputs. |
| Native validation | [validation.py](../src/kodi_lab/runtime/validation.py) `validate_reviewed_image()`; [ostree_validation.py](../src/kodi_lab/runtime/ostree_validation.py) `native_identity()`, `validation_matches()`, `validate_ostree_image()`; runtime-specific [healthcheck](../src/kodi_lab/runtime/assembly_assets/piers-22.0-rc2-r4/healthcheck.py), [display capabilities](../src/kodi_lab/runtime/display_capabilities.py) and [reviewed capabilities](../src/kodi_lab/runtime/reviewed_capabilities.py). |
| Python/system APIs | [addon_dependencies.py](../src/kodi_lab/application/addon_dependencies.py) `_PYTHON_API_BY_RELEASE` / `_GUI_API_BY_RELEASE`; inspect upstream and installed `xbmc.python` descriptors for API/ABI and independently probe the interpreter. RC1 API 3.1.0 and RC2 API 3.1.1 both used ABI 3.0.0. |
| Labels/selectors | [runtime_installations.py](../src/kodi_lab/application/runtime_installations.py) `STABLE_RELEASES` / `_view()`, [runtime_matrix.py](../src/kodi_lab/application/runtime_matrix.py), Web [managed slots](../src/kodi_lab/web/managed_slots.py), [runtime-installations.js](../src/kodi_lab/web/static/runtime-installations.js), [runtimes](../src/kodi_lab/web/templates/runtimes.html), [instance detail](../src/kodi_lab/web/templates/instance-detail.html) and [retained development](../src/kodi_lab/web/templates/retained-development.html) templates. |
| Tests/fixtures | [test_piers_rc2.py](../tests/runtime/test_piers_rc2.py) shows exact-pin, stale consent/payload, native ordinal, healthcheck, Bridge and context regressions; also inspect [runtime tests](../tests/runtime/), [Create/Bridge preparation tests](../tests/application/test_managed_bridge_preparation.py), [broker image-operation tests](../tests/broker/test_reviewed_runtime_installation_operation.py) and [Web Create tests](../tests/web/test_create_kodi_api.py). |

Add a reviewed revision for changed immutable inputs; retain old recipe bytes,
digests, reports and recorded-recipe resolution. Recompute hashes from exact files
through the complete chain, including sealed evidence and asset manifests.
Release/recipe identity, source/app pins, target/definition identity where required,
native expectations and consent are version-specific. Base/platform/GL, support
closure, launch/display helpers and Bridge may be reused only when their exact
bytes remain compatible and the loader explicitly binds that reuse. RC2 r4
reused the reviewed r3 support closure and shared helpers; it did not relabel RC1
validation as RC2 evidence. New standalone assets need separate static checks and
the established archived-script mypy treatment in [pyproject.toml](../pyproject.toml).

For **RC → final**, verify the stable native tag and absence of an RC ordinal,
recheck APIs, and review preview-only admission, consent, support-tier and Bridge
selection. A final upstream version does not automatically approve a Kodi Lab
provider for production. Keep old preview receipts resolvable.

A **new major/generation** also crosses closed `Generation`/`Version` types in
[runtime_installations.py](../src/kodi_lab/application/runtime_installations.py)
and [managed_versions.py](../src/kodi_lab/application/managed_versions.py), slot/UI
maps, broker/MCP contracts and persisted constraints such as
[`ManagedVersionOperationRow`](../src/kodi_lab/storage/models.py). Trace these
references; if constraints need migration, review preservation and migration tests
explicitly. Do not assume a recipe-only update or relax an allowlist globally.

For **Bridge compatibility**, inspect stable [source](../bridge/service.kodi_lab_bridge/),
[`approved_bridge_build()`](../src/kodi_lab/broker/managed_bridge_trust.py), stable
[inventory](../src/kodi_lab/kodi/managed_bridge_build.json), preview
[inventory](../src/kodi_lab/kodi/managed_bridge_preview_build.json),
[`packaged_rc1_bridge_sources()` / `build_rc1_bridge_archive()`](../src/kodi_lab/kodi/rc1_bridge.py)
and its [identity fragment](../src/kodi_lab/kodi/rc1_bridge_identity.txt).
The historically named RC1 overlay supplied RC2's compatible 1.1.32 build.
Check advertised capabilities, exact release and live authenticated handshake via
[`ManagedBridgeTrustService`](../src/kodi_lab/application/managed_bridge_trust.py).
If code changes are needed, review source, deterministic ZIP, inventories,
fingerprints and wheel resources as a separate artifact; then qualify it natively.
Do not silently reuse an incompatible overlay or patch a retained installation.
See [Bridge preparation](MANAGED-BRIDGE-PREPARATION.md) and [Bridge reference](BRIDGE.md).

## Minimal native qualification

Qualify the changed runtime on an independently owned disposable environment;
prove exact Engine/volume/network ownership and capacity before creating it.
Use independent state/keys where needed to avoid deterministic target collisions.
Keep original profiles protected and one managed Kodi running at a time. Use inert
add-ons without real backend activity and only deliberately granted QA authority.

| Mandatory core check | Acceptance evidence |
| --- | --- |
| Fresh acquisition/assembly | Exact reviewed inputs, signatures/hashes, image ID and phase timings; executable/JSON-RPC agree on the release and provider-specific source checks pass. |
| Guided Web Create | Actual unchecked preview consent when applicable, immutable approval, one saved parent/child, reviewed setup and requested final state. Stopped is the default. |
| Readiness/Bridge | Native Ready plus reviewed code inventory/enabled state and authenticated current-container identity. Recheck live readiness after Start; stored stopped trust is not live inspection. |
| Start/Stop/retained reuse | Start → Stop → Start retains the expected profile/runtime/image and respects capacity; no unrelated Kodi is evicted. |
| Screenshots/logs/UI | Capture and inspect real native pixels and bounded logs, including startup or Python errors. Fixtures and accepted launch dispatch are not execution. |
| Agent development | One deliberate managed grant; create/edit/validate/deploy a harmless owned project, launch, observe its execution marker/window, then exercise focus/Select/Back. |
| First viewer/Take Control | First real viewer after Create displays pixels before MCP screenshot/deployment; human input reaches native Kodi, agent input is refused while controlling, read-only access stays allowed, release replays no key, and a fresh agent action works. Record width/browser and cleanup. |
| Autonomous Reset/Bridge | On the owned idle profile with exact expectation and verified stopped-native archive: separate Reset/Bridge receipts, stopped completion, fresh authentication, redeployment and observed execution. |
| Replay/interruption | Persist request UUID/body before dispatch. Reopen/read and explicitly resume the original operation, then replay it; prove no duplicate installation/replacement and retain uncertain/terminal histories. Do not manufacture interruption by killing an unowned worker. |
| Closeout | Stop QA, verify no outstanding leases/active use, preserve source and unrelated inventories, independently extract/hash/check native and application backups, revoke temporary grants/credentials and verify refusal. |

Retained invalidated-Bridge repair is required when changing Bridge maintenance or
compatibility; include its original receipt and unchanged project selection.
Optional/version-specific checks include additional architectures/platforms,
physical touch/IME/other browsers, sustained viewer duration, playback/backend
behavior and native ZIP/repository installation. Promote these to required only
when the change/acceptance scope needs them. Do not rerun all four older campaigns
for one changed runtime; reuse versioned evidence and focused unaffected-version
contracts. Broaden native coverage only for a demonstrated shared change.

## Keep investigation and iteration efficient

Estimate disk for downloaded objects **plus** checkouts, support packages,
Docker's unpacked context, intermediate snapshots/layers, image export and recovery
copies. Budget RAM for the sealed in-memory context and any tmpfs staging too.
Measure actual filesystem free space, available RAM
and Engine limits before acquisition and at phase boundaries. Avoid needless
duplicate staging/bundles; preserve verified immutable inputs and supported exact
image caches where admissible. Do not bypass cold acquisition when that provider
or recipe is what needs qualification, or invent an unverified binary cache.

**RC2 measurements, not universal requirements:** acquisition/assembly/validation
took 204.582/366.243/30.617 seconds. A disk-backed attempt needed cancellation;
the successful QA used a bounded 10 GiB tmpfs acquisition/socket volume, separate
persistent profile/receipt storage and a 6 GiB/no-swap/2-CPU Engine. Observed minimum
free disk was about 2.54 GiB. The 8 GiB admission check, 6 GiB staging bound and
OSTree reserve stayed unchanged; a proposed reserve override was withdrawn.
Tmpfs needs measured RAM/cgroup capacity and is an environment choice, not a
general minimum or a reason to make profiles ephemeral.

| Failure class | Next useful check |
| --- | --- |
| Source/admission | Trace old release/digest comparisons through context, worker, image observation, Start and consent before another cold build. RC2 exposed stale RC1 checks. |
| Artifact/packaging | Compare signed object, embedded source, API, libraries, support manifest and asset hashes; preserve the exact failed provenance. A 404 does not authorize a newer dependency. |
| Environment | Check disk peak, actual mount, socket owner/mode, inner versus host Engine, ports/networks and resource limits; fix only owned QA configuration. |
| Fixture/controller | Inspect native logs independently. RC2's first accepted launch failed because its path-launched fixture needed an explicit add-on ID; correction and observed execution established success. Bounded UI polling pause required explicit original-request Resume. |

After each meaningful phase, record source/tree, recipe/digest, image/profile,
original request/child IDs, terminal versus uncertain state, measurements,
backup paths and next supported action. On failure inspect/reconcile that request
before any new admission. Cancel only a proven owned worker, verify cleanup, retain
failed receipts and move to independent work if recovery is blocked. Publish
sanitized evidence; keep credentials/private source in protected local artifacts.
The [RC2 report](PIERS-RC2-QUALIFICATION.md) links historical qualification; its
private `REPORT.md` / `RECOVERY.md` remain at
`/home/kyle/kodi-lab-deployment/piers-rc2-core-20261112/` as examples, not reusable
container names, credentials or permission to resume that environment.

## Plan production adoption separately

Source qualification does not authorize upgrading an occupied installed profile.
Use [application upgrade/rollback](LINUX-V1-UPGRADE-ROLLBACK.md) and
[managed state recovery](MANAGED-STATE-RECOVERY.md) for the exact supported baseline.

1. Re-anchor installed package/schema, live images/mounts, approved runtime and
   container/profile IDs, cached old images, slot occupancy, grants and leases.
2. Inspect historical pending/uncertain Create/Reset/Bridge receipts and their
   supported recovery. New recipe availability does not make those receipts disposable.
3. Check peak disk/RAM and retain a matched application backup **and a separate
   verified stopped-native profile archive**, required keys and immutable images.
   Extract/hash/SQLite-check recovery material; rehearse recovery independently.
4. Determine whether the supported operation retains, recreates or replaces the
   profile, and what requires explicit replacement authorization. **Reset recreates
   the recorded runtime; it is not an upgrade.** RC2 did not qualify a universal
   occupied-RC1-slot transition. If no supported path exists, report it blocked.
5. Before authorized adoption, write exact roll-forward steps, stop conditions and
   rollback/restore identities. Keep old/new managed Kodi from running together;
   preserve unrelated profiles, external targets, private projects, credentials,
   uncertain histories, NAS and host services. Use supported lifecycle operations;
   never edit DB rows or swap containers to bypass an identity/receipt fence.
6. After adoption, verify the installed bytes and native workflow independently,
   then service health/stopped profiles/leases and preservation. An old backup
   restore alone does not establish current Bridge readiness or source equivalence.

## Copyable Codex campaign prompt

```text
Update managed Kodi <CURRENT_KODI_VERSION> to <TARGET_KODI_VERSION>.
Official release reference: <OFFICIAL_RELEASE_REFERENCE>.
Existing reviewed recipe: <EXISTING_REVIEWED_RUNTIME_RECIPE>.
Installation host: <INSTALLATION_HOST>.
Authorized native acceptance scope: <NATIVE_ACCEPTANCE_SCOPE>.

Read docs/MANAGED-RUNTIME-UPDATES.md and current qualification evidence.
Fetch/re-anchor source and installed identities; inventory protected state,
receipts and capacity. Verify exact Linux provenance, APIs and Bridge compatibility.
Implement the smallest coherent recipe/admission/label changes in an isolated
worktree. Preserve historical pins/evidence and unrelated runtime behavior.
Run focused tests, relevant integration/static checks and only authorized isolated
native acceptance; verify actual execution separately from dispatch receipts.
Review the diff and security implications. Merge only passing, authorized work.
Keep a durable ledger with original UUIDs, immutable identities and recovery steps.
Skip blocked operations and continue independent eligible work; never bypass
ownership, capacity, consent, receipt uncertainty or retained-profile protections.
Plan production adoption separately; no occupied-profile replacement without its
explicit authorization. Do not run the full suite merely because the campaign ends.
Report source/native/installed results separately, exact PR/merge identities,
tests, failures, remaining blockers, recovery and final preservation/cleanup.
No public release/tag unless separately requested.
```

## Testing efficiency policy

**Avoid running the entire regression suite until genuinely required.** Follow
[CONTRIBUTING.md](../CONTRIBUTING.md#testing-changes) and
[test-performance guidance](TEST-PERFORMANCE.md). Use `check-focused` on each new
or materially changed test file and affected subsystem paths, applicable Ruff/mypy,
then `check-integration` for actual subsystem interactions and bounded native tests
for the changed runtime. A current RC2 example selection is:

```console
./scripts/check-focused tests/runtime/test_piers_rc2.py \
  --source src/kodi_lab/runtime/recipes.py src/kodi_lab/runtime/ostree_validation.py
```

Adapt paths to the actual change; pass `--source` once with its path list. Run the
complete `./scripts/check` only for material cross-cutting risk, an integration/
release gate or explicit owner direction. Record why focused coverage is
insufficient first. Prefer one run on settled bytes, without concurrent native
acquisition/heavy QA; isolate failures before considering a justified repeat.
Campaign completion, a PR merge or documentation-only work does not itself
require a full run. A new generation or provider may justify broader coverage;
ordinary version changes do not establish that need automatically.

Keep the accepted full-suite baseline separate from new results. **At this guide's
2026-11-12 checkpoint**, the latest accepted baseline is commit
`a92d9229ea61c7c24008e6db3757fe526c301f7a`, tree
`b86fc2d7829957ec1aea039ab90cc6a0fdd5ad69`: 8,799 passed, 49 skipped, 5 warnings,
48m44s. RC2 separately passed focused/native gates, including 16 RC2 regressions,
144 provenance cases/7 opt-in skips and 136 Create/Bridge interaction cases;
overlapping groups must not be summed. No full suite ran for RC2. Refresh the
baseline from the latest accepted report in future work; none of these older
results is a full-gate claim on new source.

For deeper provider history, see [RC1 installation/sealing](RC1-INSTALLATION-BACKEND.md),
[assembly](RC1-IMAGE-ASSEMBLY.md), [fixed-snapshot support recovery](PIERS-RC1-ACQUISITION-REVISION2.md)
and [RC2 qualification](PIERS-RC2-QUALIFICATION.md). Their recorded versions and
limits remain historical; current source and fresh native evidence govern an update.
