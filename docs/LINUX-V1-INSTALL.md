# Linux amd64 V1 installation from a local release bundle

This procedure installs the wheel-based Web/API/MCP image and, optionally, the
four-version managed Kodi lab. It uses only files in the staged bundle at
installation time. The current candidate is local and unpublished; the
publication step must provide authenticated provenance for the bundle and its
checksum list. Do not treat a checksum list from an untrusted source as a
signature.

## Platform and bundle

Use a native Linux amd64 Docker Engine with Compose V2, a Docker administrator
account, `sha256sum`, and sufficient free disk for roughly 1.2 GiB of runtime
archives plus loaded images, state, and snapshots. The clean-host acceptance
used Docker Engine 29.8.1 with its containerd image store. Other image stores
must produce one of the two exact IDs recorded in each wheel manifest or the
runtime verifier refuses the image. Check that `10.250.0.0/24` conflicts with
neither host routes nor another Docker network before enabling the lab. The
default browser publication is host loopback port 18100.

The candidate bundle contains:

| File | Purpose |
| --- | --- |
| `kodi-lab-application-images.tar`, `APPLICATION-IMAGE-IDS` | Installed Web and broker images plus their expected local IDs/platform |
| `compose.release.yaml`, `compose.lab.yaml`, `.env.example` | Source-free remote profile, optional managed-network overlay, nonsecret settings |
| `kodi-19.5.tar`, `kodi-20.5.tar`, `kodi-21.3.tar`, `kodi-22.0-beta2.tar` | Exact reviewed runtime exports |
| `SHA256SUMS` | Exact byte hashes for every other bundle file |
| `INSTALL.md`, `KODI-VERSION-EXTENSIBILITY.md`, `CURATED-IMAGE-DISTRIBUTION.md`, `UPGRADE-ROLLBACK.md`, `MANAGED-STATE-RECOVERY.md` | Installation, version policy, distribution, and recovery guidance |

The application images install a built Kodi Lab wheel and locked Python
dependencies. The small runtime manifests and validation evidence are wheel
resources; the four large archives are separate. Compose has no build context
and never needs a source checkout. Check the input before using the Docker
administrator interface:

```sh
cd /path/to/unpacked-bundle
sha256sum -c SHA256SUMS
cp .env.example .env
# Edit only the image references or loopback published port if your reviewed
# bundle metadata requires different values. Never put passwords in .env.
docker image load --input kodi-lab-application-images.tar
docker image inspect --format '{{.Id}} {{.Os}}/{{.Architecture}}' \
  kodi-lab-rc-web:local kodi-lab-rc-broker:local | cmp APPLICATION-IMAGE-IDS -
docker compose -f compose.release.yaml config --quiet
```

The `.env` image references select the verified application images. The
published port defaults to `127.0.0.1:18100`; the application listens on 8100
inside its container. Choose another unused loopback port with
`KODI_LAB_PUBLISHED_PORT`. An externally reachable browser deployment needs a
separately reviewed TLS/Host/cookie configuration.

## Remote-only first run

This profile needs neither the broker nor a Kodi runtime archive. Compose
creates the private state and broker-socket volumes. The empty socket volume has
no listener until the lab profile starts. Web has no Docker socket.

```sh
docker compose -f compose.release.yaml run --rm --no-deps \
  --entrypoint kodi-lab-admin web version
docker compose -f compose.release.yaml run --rm --no-deps \
  --entrypoint kodi-lab-admin web initialize
docker compose -f compose.release.yaml run --rm --no-deps \
  --entrypoint kodi-lab-admin web set-password
docker compose -f compose.release.yaml up -d --wait --no-build web
```

Set a strong administrator password at the hidden interactive prompt. This is
the only user-supplied secret required for local first run. `initialize`
generates a random master key in the private state volume, migrates SQLite to
the installed head, and binds the key and state marker. Repeating it on a
complete installation is a verified no-op. Web startup validates the marker
and schema and does not migrate. An incomplete first run fails with an
instruction to initialize; a mismatched or partial state needs explicit
recovery. Open `http://127.0.0.1:18100/login` and sign in.

MCP uses the same installed Web service; configure a client and its bearer
credential through the authenticated Web administration flow if needed. It
requires no broker or runtime image for remote targets. Remote targets and
their allowed ports are optional and configured through the existing Web
onboarding policy. No physical target is contacted by these install steps.

## Add the managed lab

Verify/load the exact four archives into the **same Docker Engine** used by
Compose. The prior `sha256sum -c` must have passed. The installed verifier
checks exact reviewed or approved archive-derived IDs and linux/amd64;
filenames and tags are not authority.

```sh
docker image load --input kodi-19.5.tar
docker image load --input kodi-20.5.tar
docker image load --input kodi-21.3.tar
docker image load --input kodi-22.0-beta2.tar
docker compose -f compose.release.yaml --profile lab run --rm --no-deps \
  --entrypoint kodi-lab-runtime broker verify
docker compose -f compose.release.yaml stop web
docker compose -f compose.release.yaml --profile lab run --rm --no-deps \
  --cap-add SETGID --cap-add SETUID \
  -e KODI_LAB_TRUSTED_CUSTOM_BUILDS_ENABLED=true \
  --entrypoint kodi-lab-lab broker bootstrap --ack-preview-kodi22
docker compose -f compose.release.yaml -f compose.lab.yaml --profile lab \
  up -d --wait --no-build web broker
docker compose -f compose.release.yaml -f compose.lab.yaml --profile lab \
  exec -T web kodi-lab-lab status
```

The one-shot bootstrap verifies initialized state and exact images, creates or
verifies `kodi-lab-managed-v1` with its product labels and fixed IPAM, seeds the
wheel-owned catalog, confirms only the reviewed Kodi 22 Preview revision, and
creates four stopped canonical instances. The Preview flag and invocation-only
Trusted Custom Build opt-in are both mandatory; neither belongs in `.env`.
Repeat bootstrap with Web stopped to prove or repair an interrupted partial
bootstrap. An exact repeat creates zero catalog rows. The ordinary broker has
only its restricted operation set and never receives SETUID/SETGID.

Start and inspect the lab from the installed Web image:

```sh
docker compose -f compose.release.yaml -f compose.lab.yaml --profile lab \
  exec -T web kodi-lab-lab start
docker compose -f compose.release.yaml -f compose.lab.yaml --profile lab \
  exec -T web kodi-lab-lab status
docker compose -f compose.release.yaml -f compose.lab.yaml --profile lab \
  exec -T web kodi-lab-lab stop 19 22
```

No selector means all four; explicit selectors can be `19`, `20`, `21`, or
`22`. A compatible fixture can be registered and assigned by selector through
the installed CLI, then reset from the authenticated instance page:

```sh
docker compose -f compose.release.yaml -f compose.lab.yaml --profile lab \
  exec -T web kodi-lab-lab fixture assign 19 small-mixed-v4
```

The representative mixed fixtures are `small-mixed-v4` for Kodi 19,
`small-mixed-v3` for 20, `small-mixed-v2` for 21, and `small-mixed-v5` for 22.
The CLI checks package integrity and exact release compatibility before
assignment; no SQLite edits or UUID calculation are needed. Reset finishes
with that instance stopped; start it normally afterward. Snapshot create and
restore, screenshots, add-on ZIP upload/install, and Kodi 21 mediated display
are authenticated Web operations.

## Backup, restart, and recovery

Stop the managed runtimes if a quiescent lab is desired, then stop Web and
broker before application backup:

```sh
docker compose -f compose.release.yaml -f compose.lab.yaml --profile lab \
  exec -T web kodi-lab-lab stop
docker compose -f compose.release.yaml -f compose.lab.yaml --profile lab \
  stop web broker
docker compose -f compose.release.yaml run --rm --no-deps \
  --entrypoint kodi-lab-admin web backup
docker compose -f compose.release.yaml -f compose.lab.yaml --profile lab \
  up -d --wait --no-build web broker
```

The backup command verifies its own application artifact. It records hashes of
published managed snapshots as external dependencies; preserve the private
snapshot tree separately for complete disaster recovery. See the bundled
`UPGRADE-ROLLBACK.md` and `MANAGED-STATE-RECOVERY.md` before upgrades or
restores. Keep the prior image and matching backup. `docker compose down`
removes only the control plane and default network; it does not stop or delete
broker-owned Kodi containers. Preserve the named volumes and managed network
across control-plane recreation.

For deliberate cleanup, deprovision each managed instance through the
authenticated Web lifecycle flow, verify no managed container remains, then
stop the control plane. Remove this installation's named volumes and the
product-labeled managed network only after exporting wanted backups and
snapshot artifacts. Removing a state volume is never a substitute for
deprovisioning broker-owned containers.

## Security and current limits

Web/API/MCP runs as UID/GID 10001, with a read-only root filesystem, private
state volume, and read-only view of the broker socket volume. Broker alone
receives `docker.sock` and accepts the exact Web UID through Unix peer
credentials. The socket directory is `0:10001` mode `0750`, the socket mode
`0660`, and state/key/database remain private to UID 10001. Managed Kodi
containers use UID/GID 1000, the product bridge, no published ports or mounts,
and no automatic restart policy. Broker Docker access remains effectively
host-root authority and is restricted by the trusted deployment boundary.

The local candidate has no public artifact provenance/signature, registry
distribution, or retention policy yet. Tested image identity translation is
limited to the recorded Docker IDs. The acceptance used an isolated nested
Docker Engine, so native host packaging, non-containerd image stores, LAN/TLS
access, and external physical Kodi pairing need their own gates. Docker image
bytes are outside the application backup. Kodi 21.3 is the only reviewed
mediated-display runtime; Kodi 22 requires Preview acknowledgement.

## Local candidate staging for release engineering

From a reviewed development checkout, run
`scripts/stage-linux-v1-bundle REVIEWED_ARCHIVE_DIRECTORY EMPTY_OUTPUT_DIRECTORY`.
It verifies all four exact archives against the wheel-owned manifests, builds
the installed Web/broker images, copies all required deployment/docs files,
and writes `APPLICATION-IMAGE-IDS` and `SHA256SUMS`. Set
`KODI_LAB_BUNDLE_BUILD_NETWORK=host` only if the release-build host's default
Docker build network is unavailable. This is a recorded release-engineering
step, not a user installation step. The publication phase must establish
authenticated provenance, immutable storage, signatures, and retention before
any public release.
