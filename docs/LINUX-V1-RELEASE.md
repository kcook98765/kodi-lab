# Linux amd64 V1 local deployment candidate contract

The V1 bundle distributes Kodi Lab application images, the reviewed bridge
1.1.31 ZIP, and three current reviewed runtime recipes. It distributes no Kodi binaries.
The candidate is local and unpublished. `BUNDLE.json` records the exact source
Git commit/tree, application image identities, bridge artifact identity, recipe
metadata, and every distributed file. `SHA256SUMS`
covers each distributed file except itself. A checksum from the same untrusted
download checks integrity but does not establish publisher identity.

| Class | Distributed files | Authority |
| --- | --- | --- |
| Application | `kodi-lab-application-images.tar` | Saved image IDs, OCI config IDs, linux/amd64 platform, installed package identity |
| Deployment | `compose.release.yaml`, `compose.lab.yaml`, `compose.development.yaml`, `compose.lan-http.yaml`, `.env.example`, `linux-v1-bundle` | Exact file hashes; generated `.env` pins local image IDs and may record one host source directory |
| Reviewed runtime inputs | `runtime-recipes/matrix-19.4-graceful-r4.json`, `nexus-20.5-graceful-r4.json`, `omega-21.3-graceful-r6.json` | File hashes and installed wheel recipe digests; provider, suite, version |
| Remote bridge | `bridge/service.kodi_lab_bridge-1.1.31.zip`, `bridge/BRIDGE.json` | Exact ZIP hash, closed source inventory and Git bridge tree, source commit/tree, protocol, builder and MIT license |
| Kodi feed inputs | `repository/STABLE.json`, `repository/DEVELOPMENT.json`, installed repository generator/server | Reviewed repository/Bridge identities; generated bootstrap and feed bytes have separate per-install provenance |
| Verification | `BUNDLE.json`, `SHA256SUMS` | Canonical inventory and byte hashes |
| Guidance | `LICENSE`, `THIRD_PARTY_NOTICES.md`, `INSTALL.md`, `RELEASE.md`, `PRODUCTION-OPERATIONS.md`, `UPGRADE-ROLLBACK.md`, `MANAGED-STATE-RECOVERY.md` | Exact file hashes |

The wheel is built inside the pinned-base Docker multi-stage build and installed
with locked dependencies. The bundle contains no source checkout at install
time. The recorded Git tree, Dockerfile and lockfile digests, application export
hash, and immutable image IDs identify the candidate; a reproducible-build
claim is not made. The local candidate's clean committed `HEAD` contains exactly
the recorded `HEAD^{tree}`. Staging verifies that copied recipe bytes match the
installed wheel's reviewed recipe digests.

Current reviewed stable viewer/runtime recipes are Matrix 19.4 revision 4,
Nexus 20.5 revision 4 and Omega 21.3 revision 6. Recipe selection does not establish
native qualification for these exact images. Historical standard-viewer acceptance
covers Matrix r3, Nexus r3 and Omega r5. More recent accepted Claw native evidence
covers Omega r6 development, retained Omega r6 ↔ Nexus r4 switching and scoped
installed viewer checks. [Matrix r4 native core](https://github.com/kcook98765/kodi-lab/blob/main/docs/MATRIX-PIERS-AGENT-QUALIFICATION.md)
also passed development, GUI, Reset and switching within its recorded scope.
Acceptance is limited to the exact tested environments and workflows; no evidence
establishes universal browser, platform or runtime qualification. Historical
resources remain in the wheel for exact pins and provenance; acquiring a recipe does not change a
registered instance's image or profile. See the [standard viewer guide](https://github.com/kcook98765/kodi-lab/blob/main/docs/STANDARD-NOVNC.md).

The stable runtime recipes include the [graceful shutdown correction](MANAGED-GRACEFUL-STOP.md). New image installation and native qualification remain separate from application deployment; existing profile pins are preserved.

The stable set is Matrix on Ubuntu Jammy, Nexus on Ubuntu Noble and Omega on
Ubuntu Resolute. Piers 22 RC2 is an additional Development-only Preview option;
its [qualification report](https://github.com/kcook98765/kodi-lab/blob/main/docs/PIERS-RC2-QUALIFICATION.md)
records exact recipe and native limits. Retained RC1 profiles preserve their pins.
Its reviewed OSTree recipe, trust key, support closure and assembly assets are
wheel resources rather than loose `runtime-recipes/` files. No Kodi executable
or managed runtime image is bundled.

Native Ubuntu 24.04 amd64 is the qualified baseline. Windows 11 uses the
maintained Ubuntu 24.04 WSL2/Docker Desktop Linux-engine installation path.
[Windows acceptance #275](https://github.com/kcook98765/kodi-lab/issues/275#issuecomment-6870625793)
passed fresh installation, Omega lifecycle/viewing/control, agent-owned development,
experimental-opt-in Codex MCP and backup/recovery at `23dd1d63dfd49362a9925df470f0f5406f08c6e1`.
A changed successor-upgrade path needs its own Windows verification (#365).
macOS and ARM64 hosts are not qualified. Historical private archive bootstrap
resources are not release prerequisites.

`0001_initial_release` is the initial database baseline. The supported forward
chain runs from `0001_initial_release` through `0014_human_reset_bridge`, the
current head. Both sdist and wheel include this same migration tree; the
[migration policy](https://github.com/kcook98765/kodi-lab/blob/main/migrations/README.md)
describes each intermediate revision. Supported upgrades use the explicit administrator upgrade flow;
ordinary Web startup requires the installed head and never migrates. Unsupported
prerelease databases are refused; there is no conversion or stamping path.
See the [migration policy](https://github.com/kcook98765/kodi-lab/blob/main/migrations/README.md).

Managed profiles may be disposable or explicitly retained for per-version
development. Retention keeps a container's writable profile when switching; it
is not a backup. Provisioning creates a
stopped container from an exact pinned runtime revision; stopping retains that
container and its writable layer. Deprovisioning removes the container and
layer while retaining the managed record and durable Kodi Lab state.
**Reset permanently discards that version's writable Kodi profile**, settings and
installed add-ons. Supported [clean Reset](MANAGED-CLEAN-RESET.md) recreates a
profile from the same approved cached image without applying a fixture. The older
Advanced fixture-reset flow instead reapplies an assigned deterministic fixture.
Snapshots provide bounded preservation, including the existing confirmed
deletion/preservation policy. Retained profiles still use the container writable
layer; deprovisioning removes that layer. There is no automatic profile
reconstruction.
External/production Kodi targets may be long-lived installations and remain
under their separate registration, onboarding, and bridge policies.

Kodi Lab installs from the bundle offline. First acquisition of a managed Kodi
runtime needs the reviewed upstream packages and pinned base image unless
already cached. The installed worker verifies signed APT metadata for stable
generations or exact signed OSTree inputs for Piers, critical package
versions/hashes, image identity, behavior, and HMAC-bound
local evidence before eligibility. It cannot fall back to a source build,
different version, alternate provider, or private artifact. Users acquire only
the generations they choose.

The release Compose binds Web to host loopback. Web has the restricted broker
Unix socket but no Docker socket. The broker alone mounts the Docker socket,
which grants effective host-root authority; only trusted administrators should
control that service or its volumes. Runtime installation is available through
the authenticated reviewed-runtime Web flow and the explicit administrator
command in the installed broker image. The broker owns the bounded installation
worker; its lifecycle protocol enforces exact reviewed images and revisions.

The explicit repository service binds only the chosen private-LAN host address
and port. It shares the installed Web image bytes but runs a separate static
entry point with only the read-only `repository_data` mount, no private state,
broker/Docker socket, credentials or administrative routes. A one-shot offline
generator receives the verified candidate read-only and writes the dedicated
volume. `BUNDLE.json` records its reviewed inputs and capability, while
`PROVENANCE.json` in the volume binds the actual configured URL, generated
repository ZIP/index hashes and package hashes to the BUNDLE digest. HTTP is
unauthenticated; malicious-LAN transport authenticity is not claimed. A later
TLS terminator can preserve the same paths and content.
The repository descriptor selects Kodi's `<dir>` format, verified SHA-256 index
checksum and SHA-256 package hashes. The server supplies both the package
`Content-SHA256` header and immutable `.zip.sha256` fallback sidecar.

Local add-on source access is disabled in the base Compose deployment. An
administrator may use the bundled development overlay to bind one validated
host directory read/write in Web and read-only in broker, both at fixed
`/mnt/kodi-lab-development`. The overlay fixes `KODI_LAB_DEVELOPMENT_ROOT` in
both services; only the host source is configured
through `KODI_LAB_DEVELOPMENT_SOURCE` in local `.env`, which is excluded from
the staged bundle. The release helper rejects non-canonical, relative, empty,
missing, or symlinked source directories; Compose rejects an unset or empty
variable and never creates a missing bind source. Web/API registration remains
root-relative, no-follow, and rejects nested mount crossings. The writable Web
mount preserves reviewed patch-back;
the Web service account receives write access only where the administrator
separately grants filesystem permissions. Broker requires its fixed read-only
mount, independently resolves the registered source, and gains read authority
within the administrator-selected root. Use a dedicated root containing only
intended material. Repository and Kodi containers receive no external source
mount; missing or incorrect broker wiring fails closed. No broker protocol or
schema change is involved. Remote-only and uploaded ZIP paths need no source
mount. Linux amd64 source sharing has its recorded qualification. The accepted
Windows agent-owned project workflow needs no host source mount and does not
qualify arbitrary Windows directory sharing. Host SELinux/AppArmor labels are
not changed automatically.

Application backup contains SQLite, keys, recipe installation records,
revisions, and HMAC evidence. Docker image bytes and broker snapshot artifacts
are separate. A missing image after restore is unavailable for provisioning.
Explicit reacquisition creates a new exact revision; an existing instance is
reassigned only through the stopped-Web `kodi-lab-lab repin` administrator
command. Read `MANAGED-STATE-RECOVERY.md` for the snapshot dependency contract.

Public distribution requires separate owner approval,
an authenticated channel for the accepted manifest digest, and verification of
downloaded bytes. No independent signature or GitHub attestation is claimed
for this local candidate.
Source regression, bundle integrity and native platform acceptance are separate
release-readiness claims.
