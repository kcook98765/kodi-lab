# Linux amd64 V1 installation from a local release bundle

This candidate installs Kodi Lab from bundled files. Managed Kodi runtimes are
acquired later, one generation at a time, through the installed reviewed recipe
worker. The candidate is local and unpublished. Obtain the `BUNDLE.json` digest
through a trusted publication channel before treating its hashes as authentic.

## Platform and contents

Use native Linux amd64 Docker Engine with Compose V2, a Docker administrator,
Python 3.12 or newer, and `sha256sum`. Check that `10.250.0.0/24` conflicts with
neither host routes nor another Docker network before enabling the managed lab.
The Web port defaults to `127.0.0.1:18100`.

The bundle contains Web/broker application images, source-free Compose files,
configuration example, bundle verifier, manifest and checksums, three reviewed
recipe descriptions in `runtime-recipes/`, license and notices, and recovery
documents. The same recipes are wheel resources inside both installed images.
No Kodi binary, runtime image export, `.deb`, or package cache is distributed.

Kodi Lab itself installs from the bundle. First Kodi acquisition needs network
access to the reviewed Ubuntu package provider and pinned base image unless
the required Docker/package cache is already present. If exact reviewed inputs
are unavailable, installation stops. There is no alternate Kodi version,
provider, private archive, or source build fallback.

## Install Kodi Lab with no Kodi image

```sh
cd /path/to/unpacked-bundle
sha256sum -c SHA256SUMS
python3 linux-v1-bundle verify .
docker image load --input kodi-lab-application-images.tar
python3 linux-v1-bundle configure .
docker compose -f compose.release.yaml config --quiet
docker compose -f compose.release.yaml run --rm --no-deps \
  --entrypoint kodi-lab-admin web initialize
docker compose -f compose.release.yaml run --rm --no-deps \
  --entrypoint kodi-lab-admin web set-password
docker compose -f compose.release.yaml up -d --wait --no-build web
```

`configure` writes `.env` with exact local image IDs and refuses to replace an
existing file. `initialize` creates the private master key, migrates SQLite to
the installed head, and binds the state marker. The password command prompts
without echo. Open `http://127.0.0.1:18100/login` and sign in. Web has no Docker
socket. Its remote-target functions need no managed Kodi image. The runtime
matrix shows three supported but unacquired generations at this point.

To start the Lab broker before any Kodi acquisition, create or verify its fixed
network and attach Web to it. This still downloads no Kodi image:

```sh
docker compose -f compose.release.yaml --profile lab run --rm --no-deps \
  --entrypoint kodi-lab-runtime broker network-ensure
docker compose -f compose.release.yaml stop web
docker compose -f compose.release.yaml -f compose.lab.yaml --profile lab \
  up -d --wait --no-build web broker
```

Pass `--port PORT` to `configure` for another loopback port. Direct LAN/WAN Web
binding requires a separate Host, cookie, and TLS review.

## Acquire only the generation needed

These commands run the installed wheel worker in the broker image. It receives
Docker authority for fixed reviewed recipe inputs and exposes no generic build
API to Web or MCP. Temporary `SETGID`/`SETUID` grants let its root worker fork
a state-owner child; the long-running broker does not receive them. Run one or
more, in any order:

```sh
docker compose -f compose.release.yaml --profile lab run --rm --no-deps \
  --entrypoint kodi-lab-runtime broker recipes
docker compose -f compose.release.yaml --profile lab run --rm --no-deps \
  --cap-add SETGID --cap-add SETUID \
  --entrypoint kodi-lab-runtime broker install matrix
docker compose -f compose.release.yaml --profile lab run --rm --no-deps \
  --cap-add SETGID --cap-add SETUID \
  --entrypoint kodi-lab-runtime broker install nexus
docker compose -f compose.release.yaml --profile lab run --rm --no-deps \
  --cap-add SETGID --cap-add SETUID \
  --entrypoint kodi-lab-runtime broker install omega
```

Choose only the generation(s) needed. Each install verifies signed Ubuntu APT
metadata and critical package hashes, assembles locally, validates, HMAC-binds
evidence, and registers an exact eligible revision. A ready image makes a
repeated install a verified no-op. Missing images cannot be provisioned; use
an explicit reinstall to reacquire them. An existing managed instance keeps its
old exact revision pin until an administrator explicitly selects the new
revision with `kodi-lab-lab repin`. Application backup includes records, recipe
provenance, revisions, and keys, but not Docker image bytes.

The first install also creates or verifies the fixed managed Docker network.
If the broker is not already running, start it and attach Web to that network:

```sh
docker compose -f compose.release.yaml stop web
docker compose -f compose.release.yaml -f compose.lab.yaml --profile lab \
  up -d --wait --no-build web broker
docker compose -f compose.release.yaml -f compose.lab.yaml --profile lab \
  exec -T web kodi-lab-lab status
```

Start or stop an acquired generation with `kodi-lab-lab start matrix`,
`start nexus`, `start omega`, and matching `stop` commands in the Web
container. A generation not acquired stays supported but unavailable. The
Docker socket is mounted only in the broker container.

## Restart and recovery

Use `docker compose -f compose.release.yaml -f compose.lab.yaml --profile lab
up -d --wait --no-build web broker` for a restart. This does not rebuild or
reacquire Kodi. Preserve the named state volumes and exact application images.
Read `UPGRADE-ROLLBACK.md` and `MANAGED-STATE-RECOVERY.md` before backup,
upgrade, or restore. A restored record does not recreate a missing Docker
image; explicitly reacquire that generation.

After `recover-clean`, a restored instance keeps its old exact revision pin.
When its image is missing, provisioning fails `runtime_image_unavailable`.
Run the reviewed installer again, record the new revision UUID it prints, then
stop Web and explicitly change only that generation's unprovisioned pin:

```sh
docker compose -f compose.release.yaml --profile lab run --rm --no-deps \
  --cap-add SETGID --cap-add SETUID \
  --entrypoint kodi-lab-runtime broker install matrix
docker compose -f compose.release.yaml -f compose.lab.yaml --profile lab stop web
docker compose -f compose.release.yaml run --rm --no-deps \
  --entrypoint kodi-lab-lab web repin matrix NEW_REVISION_UUID
docker compose -f compose.release.yaml -f compose.lab.yaml --profile lab \
  up -d --wait --no-build web broker
docker compose -f compose.release.yaml -f compose.lab.yaml --profile lab \
  exec -T web kodi-lab-lab start matrix
```

Use the generation and UUID printed by its own successful `install` command.
`repin` requires stopped Web and an unprovisioned managed instance. It accepts
only the latest ready, HMAC-eligible revision of that exact reviewed recipe.
Repeat explicitly for Nexus or Omega if their restored images are missing.

## Candidate staging

From a reviewed checkout with every candidate change staged in Git's index,
run `scripts/stage-linux-v1-bundle EMPTY_OUTPUT_DIRECTORY`. The script refuses
unstaged or untracked source changes and records the exact staged Git tree in
the bundle and image labels. It does not publish or upload anything.
