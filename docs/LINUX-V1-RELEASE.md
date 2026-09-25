# Linux amd64 V1 release candidate contract

This is the authoritative inventory for the Linux amd64 V1 bundle. The local
`1.0.0rc1` candidate is unpublished. `BUNDLE.json` is the machine-readable
instance of this contract; `SHA256SUMS` covers every file except itself. The
bundle verifier checks the complete file inventory, every size and SHA-256,
the application archive's OCI config identities, the four runtime archive
config identities, and consistency with the copied runtime manifests and
reviewed validation evidence. A checksum from the same untrusted download is
only an integrity check, not proof of who made it.

## Exact artifact set

| Class | Files or state | Authority |
| --- | --- | --- |
| Application | `kodi-lab-application-images.tar` with Web and broker images | `BUNDLE.json` hashes, per-role saved image ID and OCI config ID, linux/amd64, installed `kodi-lab-admin version` |
| Source | `kodi-lab-source.tar` from the exact committed tree | `BUNDLE.json` source commit and archive hash; useful for review, not required at install |
| Deployment | `compose.release.yaml`, `compose.lab.yaml`, `.env.example`, `linux-v1-bundle` | `BUNDLE.json` file inventory; generated `.env` pins actual local image IDs |
| Curated runtimes | `kodi-19.5.tar`, `kodi-20.5.tar`, `kodi-21.3.tar`, `kodi-22.0-beta2.tar` | Exact archive SHA-256/size in each runtime manifest and `BUNDLE.json` |
| Verification | `BUNDLE.json`, `SHA256SUMS`, `runtime-manifests/*.json`, `runtime-evidence/*.json` | The manifest links version, source commit, build inputs, artifacts, revisions, tiers and identities; copied runtime metadata must equal the wheel resources |
| Documentation | `INSTALL.md`, `RELEASE.md`, `MANAGED-LAB-WORKFLOW.md`, `BROKER-SECURITY.md`, `CURATED-IMAGE-DISTRIBUTION.md`, `KODI-VERSION-EXTENSIBILITY.md`, `UPGRADE-ROLLBACK.md`, `MANAGED-STATE-RECOVERY.md` | Hashed files in the bundle |
| Generated at install | Private master key, administrator password hash/state, Docker named volumes, broker Unix socket and managed network | Local initialization/Compose/broker bootstrap; never release artifacts |

The Python wheel is built inside the pinned-base Docker multistage build and
installed into both application images. No standalone wheel is distributed in
this bundle. `kodi-lab-admin version` reports the installed package version and
package-content SHA-256. The source commit in `BUNDLE.json` is the committed,
clean checkout used to stage this candidate. The build record identifies the
Dockerfile and lockfile digests; the application archive and each image identity
identify the resulting bytes. The local builder disables its default ephemeral
provenance attestation, whose timestamp otherwise changes the saved OCI index
on an identical second build. The explicit bundle record is the reviewable
provenance evidence. No reproducible-build claim is made. A Docker
tag is only a loading hint. `linux-v1-bundle configure .` selects an exact
saved or archive-derived config ID after Docker load and writes it to `.env`.

Each copied runtime manifest records the exact Kodi release, support tier,
runtime revision UUID/number, source or package inputs, reviewed build ID,
archive size/hash, OCI config ID, platform and validation-report digest. The
saved OCI index identity and config identity are distinct. The installed
runtime verifier accepts only the two exact manifest-approved identities for
each reviewed archive and pins the locally present one. The reports record
bounded two-cycle validation, not a promise that rebuilding the Dockerfile
will reproduce the archive. The bundle verification contract never uses a
mutable tag to authorize a runtime.

## Authenticity decision

| Option | V1 decision |
| --- | --- |
| SHA-256 inventory | Required for every release asset and the complete bundle; detects byte changes after the expected manifest digest is obtained through a trusted channel. |
| GitHub release asset integrity | Intended publication channel. GitHub transport/account access supplies the initial trust anchor; release assets can be replaced by account holders, so record the asset and manifest digests in the publication record. |
| GitHub artifact attestations | Deferred until a controlled GitHub build workflow can attest the exact reviewed multi-gigabyte artifacts. A local build cannot truthfully claim GitHub OIDC provenance. |
| Sigstore/cosign | Deferred; no reviewed registry digest, identity policy or public signing workflow exists. |
| GPG/minisign detached signature | Deferred; no established public verification key or protected signing ceremony exists. |
| Independent signing | None for V1 RC. A later signing scheme requires a real trust root and separately reviewed key custody. |

At publication, use the authorized GitHub release channel to upload the exact
accepted artifacts, publish `SHA256SUMS` and `BUNDLE.json`, record the final
bundle manifest SHA-256 and asset digests alongside the immutable source commit,
and verify downloads against those recorded values. Publication needs a
separate authorization. Do not describe hashes alone as signatures or release
assets as immutable. Keep the accepted local bundle until the published assets
have been downloaded and compared byte for byte. Do not rebuild runtime
archives from Docker tags.

## Deployment and recovery boundaries

The release Compose binds only `127.0.0.1` and allows only the loopback Host.
The intended installation is a trusted local Docker administrator host. A
trusted LAN operator can use a separately controlled local forward. Public
Internet or direct LAN Web exposure is outside this release candidate; TLS,
Host policy and secure cookie deployment need their own review if added. Remote
Kodi pairing is an optional Web-admin flow and does not require the managed
broker. The broker alone receives `/var/run/docker.sock`, which gives effective
Docker daemon and host-root authority. The private broker socket volume and
UID check are a security boundary. Web and Kodi have no Docker socket. Only
trusted administrators may mount either socket volume or load executable
runtime archives.

Do not put passwords, master keys or bearer tokens in `.env`. Initialization
generates the key in private state and prompts separately for the administrator
password. Backups contain sensitive database/secret material and do not include
the managed snapshot bytes or Docker images; preserve the snapshot tree and
reviewed image artifacts separately. `UPGRADE-ROLLBACK.md` gives the stopped
state, matching-image and receipt-bound restore procedure. No earlier public
V1 exists; upgrade/rollback claims rest on the accepted lifecycle tests and
local candidate evidence. Kodi 22 Beta 2 is Preview: bootstrap requires the
explicit Preview flag and one-shot Trusted Custom Build opt-in.
