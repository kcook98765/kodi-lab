# Linux amd64 V1 release candidate contract

The V1 bundle distributes Kodi Lab application images and three reviewed
runtime recipes. It distributes no Kodi binaries. The candidate is local and
unpublished. `BUNDLE.json` records the exact staged Git tree, application
image identities, recipe metadata, and every distributed file. `SHA256SUMS`
covers each distributed file except itself. A checksum from the same untrusted
download checks integrity but does not establish publisher identity.

| Class | Distributed files | Authority |
| --- | --- | --- |
| Application | `kodi-lab-application-images.tar` | Saved image IDs, OCI config IDs, linux/amd64 platform, installed package identity |
| Deployment | `compose.release.yaml`, `compose.lab.yaml`, `.env.example`, `linux-v1-bundle` | Exact file hashes; generated `.env` pins local image IDs |
| Reviewed runtime inputs | `runtime-recipes/matrix-19.4-r1.json`, `nexus-20.5-r1.json`, `omega-21.3-r1.json` | File hashes and installed wheel recipe digests; provider, suite, version |
| Verification | `BUNDLE.json`, `SHA256SUMS` | Canonical inventory and byte hashes |
| Guidance | `LICENSE`, `THIRD_PARTY_NOTICES.md`, `INSTALL.md`, `RELEASE.md`, `UPGRADE-ROLLBACK.md`, `MANAGED-STATE-RECOVERY.md` | Exact file hashes |

The wheel is built inside the pinned-base Docker multi-stage build and installed
with locked dependencies. The bundle contains no source checkout at install
time. The recorded Git tree, Dockerfile and lockfile digests, application export
hash, and immutable image IDs identify the candidate; a reproducible-build
claim is not made. The final publication commit must contain exactly the
recorded Git tree. Release staging verifies that copied recipe bytes match the
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
