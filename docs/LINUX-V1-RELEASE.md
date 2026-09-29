# Linux amd64 V1 local deployment candidate contract

The V1 bundle distributes Kodi Lab application images, the reviewed bridge
1.1.13 ZIP, and three reviewed runtime recipes. It distributes no Kodi binaries.
The candidate is local and unpublished. `BUNDLE.json` records the exact source
Git commit/tree, application image identities, bridge artifact identity, recipe
metadata, and every distributed file. `SHA256SUMS`
covers each distributed file except itself. A checksum from the same untrusted
download checks integrity but does not establish publisher identity.

| Class | Distributed files | Authority |
| --- | --- | --- |
| Application | `kodi-lab-application-images.tar` | Saved image IDs, OCI config IDs, linux/amd64 platform, installed package identity |
| Deployment | `compose.release.yaml`, `compose.lab.yaml`, `compose.development.yaml`, `.env.example`, `linux-v1-bundle` | Exact file hashes; generated `.env` pins local image IDs and may record one host source directory |
| Reviewed runtime inputs | `runtime-recipes/matrix-19.4-r1.json`, `nexus-20.5-r1.json`, `omega-21.3-r1.json` | File hashes and installed wheel recipe digests; provider, suite, version |
| Remote bridge | `bridge/service.kodi_lab_bridge-1.1.13.zip`, `bridge/BRIDGE.json` | Exact ZIP hash, closed source inventory and Git bridge tree, source commit/tree, protocol, builder and MIT license |
| Verification | `BUNDLE.json`, `SHA256SUMS` | Canonical inventory and byte hashes |
| Guidance | `LICENSE`, `THIRD_PARTY_NOTICES.md`, `INSTALL.md`, `RELEASE.md`, `PRODUCTION-OPERATIONS.md`, `UPGRADE-ROLLBACK.md`, `MANAGED-STATE-RECOVERY.md` | Exact file hashes |

The wheel is built inside the pinned-base Docker multi-stage build and installed
with locked dependencies. The bundle contains no source checkout at install
time. The recorded Git tree, Dockerfile and lockfile digests, application export
hash, and immutable image IDs identify the candidate; a reproducible-build
claim is not made. The local candidate's commit contains exactly the recorded
Git tree. Staging verifies that copied recipe bytes match the
installed wheel's reviewed recipe digests.

The V1 set is Matrix 19.4 on Ubuntu Jammy, Nexus 20.5 on Ubuntu Noble, and
Omega 21.3 on Ubuntu Resolute. Kodi 22 Piers has no accepted binary recipe in
this candidate and is outside the stable V1 installable set. A later optional
preview would require its own review. Windows/macOS hosts and ARM64/Apple
Silicon are post-V1.
The historical private archive bootstrap and four-runtime catalog remain in
the development checkout for compatibility; neither is a release prerequisite.

Kodi Lab-created V1 managed instances are disposable. Provisioning creates a
stopped container from an exact pinned runtime revision; stopping retains that
container and its writable layer. Deprovisioning removes the container and
layer while retaining the managed record and durable Kodi Lab state. Reset
returns it to the assigned deterministic fixture, and Snapshots provide
bounded preservation, including the existing confirmed deletion/preservation
policy. Persistent managed-container storage is deferred under D-0381.
External/production Kodi targets may be long-lived installations and remain
under their separate registration, onboarding, and bridge policies.

Kodi Lab installs from the bundle offline. First acquisition of a managed Kodi
runtime needs the reviewed upstream packages and pinned base image unless
already cached. The installed worker verifies signed APT metadata, exact
critical package versions/hashes, image identity, behavior, and HMAC-bound
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

Local add-on source access is disabled in the base Compose deployment. An
administrator may use the bundled development overlay to bind one validated
host directory read/write at `/mnt/kodi-lab-development` in Web only. The
overlay fixes `KODI_LAB_DEVELOPMENT_ROOT`; only the host source is configured
through `KODI_LAB_DEVELOPMENT_SOURCE` in local `.env`, which is excluded from
the staged bundle. The release helper rejects non-canonical, relative, empty,
missing, or symlinked source directories; Compose rejects an unset or empty
variable and never creates a missing bind source. Web/API registration remains
root-relative and no-follow. The writable mount preserves reviewed patch-back;
the Web service account receives write access only where the administrator
separately grants filesystem permissions. No broker protocol or schema change
is involved. Remote-only and uploaded ZIP paths need no source mount.

Application backup contains SQLite, keys, recipe installation records,
revisions, and HMAC evidence. Docker image bytes and broker snapshot artifacts
are separate. A missing image after restore is unavailable for provisioning.
Explicit reacquisition creates a new exact revision; an existing instance is
reassigned only through the stopped-Web `kodi-lab-lab repin` administrator
command. Read `MANAGED-STATE-RECOVERY.md` for the snapshot dependency contract.

No earlier public V1 exists. Future publication requires separate approval,
an authenticated channel for the accepted manifest digest, and verification of
downloaded bytes. No independent signature or GitHub attestation is claimed
for this local candidate.
This candidate is for extended real server testing before any RC decision.
