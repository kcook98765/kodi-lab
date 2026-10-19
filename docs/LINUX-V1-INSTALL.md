# Linux amd64 V1 installation from a local deployment candidate

This candidate installs Kodi Lab from bundled files. Managed Kodi runtimes are
acquired later, one generation at a time, through the installed reviewed recipe
worker. The candidate is local and unpublished. Preserve the Git commit and a
trusted copy of the `BUNDLE.json` SHA-256 when transferring it to the server;
hashes inside the bundle establish integrity against that trusted copy.

For installation starting with a GitHub clone, first follow
`docs/LINUX-SOURCE-INSTALL.md` in that source checkout to stage this bundle.

## Platform and contents

Use native Linux amd64 Docker Engine with Compose V2, a Docker administrator,
Python 3.12 and `sha256sum`. The application supports Python 3.12
(`>=3.12,<3.13`); its container images provide their own interpreter. Check that `10.250.0.0/24` conflicts with
neither host routes nor another Docker network before enabling the managed lab.
The Web port defaults to `127.0.0.1:18100`.

The private listener bind and advertised base URL serve different purposes.
For a new installation, prefer a site-controlled LAN DNS hostname that every
Kodi device can resolve, or reserve the host IP in DHCP. Moving a listener does
not change the origin embedded in repository ZIPs. See
[address diagnosis and recovery](https://github.com/kcook98765/kodi-lab/blob/main/docs/REPOSITORY-ADDRESS-RECOVERY.md) for DHCP changes,
installed-client recovery and the remaining origin-migration boundary.

The bundle contains Web/broker application images, source-free Compose files,
configuration example, optional development-source Compose overlay, bundle
verifier, manifest and checksums, three reviewed
recipe descriptions in `runtime-recipes/`, license and notices, and recovery
documents. It also contains `bridge/service.kodi_lab_bridge-1.1.15.zip` and its
`bridge/BRIDGE.json` provenance and the reviewed `repository/STABLE.json` and
`repository/DEVELOPMENT.json` feed inputs. The same recipes are wheel resources inside
both installed images. Kodi 22 Piers RC1 is an additional Development-only
Preview runtime; its exact OSTree recipe, public trust key, support closure and
assembly assets are embedded in the application wheel, not supplied as binaries
in `runtime-recipes/`.
No Kodi binary, runtime image export, `.deb`, or package cache is distributed.

Kodi Lab itself installs from the bundle. First Kodi acquisition needs network
access to the reviewed Ubuntu package provider, Flathub for RC1, and pinned
container base images unless
the required Docker/package cache is already present. If exact reviewed inputs
are unavailable, installation stops. There is no alternate Kodi version,
provider, private archive, or source build fallback.

## Temporary runtime network capacity

Reviewed runtime acquisition creates a short-lived, non-internal Docker bridge
for package assembly so the build can reach its reviewed package provider.
Validation uses a different short-lived, internal bridge for the test Kodi
container. Managed Kodi later uses `kodi-lab-managed-v1`; the application and
repository Compose networks are separate. The temporary bridges are named and
labeled for their exact installation operation and removed after normal success
or failure. On a later installation, a terminal operation's abandoned bridge
can be reclaimed only when its ownership labels and identity match and no
container refers to it. Kodi Lab never prunes unrelated Docker networks.

Docker's automatic bridge address pools have finite capacity. If no temporary
pool is configured, Kodi Lab checks available Docker subnets before it pulls a
reviewed runtime when Docker exposes its effective pools, and otherwise reports
Docker's allocation error at network creation.
On a host with many Docker networks, an administrator can reserve a separate
private IPv4 CIDR for Kodi Lab's temporary bridges. Check all host LAN and VPN
routes and existing Docker subnets first:

```sh
ip -4 route show table all
ip -4 address show
docker network ls -q | xargs -r docker network inspect \
  --format '{{.Name}} {{range .IPAM.Config}}{{.Subnet}} {{end}}'
```

Choose a contiguous private range absent from all three outputs and from
other networks the host may connect to later. The range must divide into 2 to
256 child subnets with child prefix `/24` through `/28`; for example, a
**site-verified** `/20` with `/24` children holds 16 temporary networks. The
example is a sizing illustration, not a suggested universal address. When
configuring the bundle, add:

```sh
python3 linux-v1-bundle configure . \
  --repository-bind PRIVATE_LAN_IPV4 --repository-port PORT \
  --repository-base-url http://DURABLE_LAN_HOST:PORT \
  --temporary-network-pool SITE_VERIFIED_PRIVATE_CIDR \
  --temporary-network-child-prefix 24
```

`configure` rechecks host addresses, routes and Docker networks before writing
the local `.env`. The installed broker and its worker receive a fixed read-only
host route view. Each acquisition checks again, skips occupied
child subnets and stops if the configured pool is unusable. A Docker collision
is also checked atomically when the bridge is created. The selected CIDR is a
local deployment setting, never bundled into the generic product or chosen by
Web/MCP. A changed VPN or Docker route may require selecting a new unused pool
before retrying. A creation error is reported by phase; Docker address-pool
exhaustion receives its own code only when Docker reports that condition.

## Install Kodi Lab with no Kodi image

```sh
cd /path/to/unpacked-bundle
sha256sum -c SHA256SUMS
python3 linux-v1-bundle verify .
docker image load --input kodi-lab-application-images.tar
# Choose a durable LAN address and URL controlled by this installation.
python3 linux-v1-bundle configure . \
  --repository-bind PRIVATE_LAN_IPV4 --repository-port PORT \
  --repository-base-url http://DURABLE_LAN_HOST:PORT
docker compose -f compose.release.yaml config --quiet
docker compose -f compose.release.yaml --profile repository-init run --rm --no-deps repository-init
docker compose -f compose.release.yaml run --rm --no-deps \
  --entrypoint kodi-lab-repository repository verify
docker compose -f compose.release.yaml run --rm --no-deps \
  --entrypoint kodi-lab-admin web initialize
docker compose -f compose.release.yaml run --rm --no-deps \
  --entrypoint kodi-lab-admin web set-password
docker compose -f compose.release.yaml up -d --wait --no-build web repository
```

`configure` writes `.env` with exact local image IDs, the explicit private-LAN
bind/port, durable origin URL and BUNDLE digest; it refuses to replace an
existing file. The one-shot repository generator validates the reviewed bridge,
publishes the stable feed atomically in a separate `repository_data` volume, and
records `PROVENANCE.json` for generated bytes. `verify` rechecks that content
without a source checkout. Preserve the generated provenance and the exact
bundle for recovery. `initialize` creates the private master key, migrates SQLite to
the installed head, and binds the state marker. The password command prompts
without echo. Open `http://127.0.0.1:18100/login` and sign in. Web has no Docker
socket. Its remote-target functions need no managed Kodi image. The runtime
matrix shows three supported but unacquired generations at this point.

## Install and pair a remote Kodi bridge

The normal remote setup uses the stable Kodi Lab repository. Sign in to the
loopback-only Web page **Kodi Repository** for its configured LAN URL, bootstrap
ZIP hash, stable package hashes and concise setup instructions. On the Kodi box,
add `BASE/install/` in File Manager, choose **Add-ons → Install from zip file**,
install `repository.kodi-lab-1.0.0.zip`, then use **Install from repository →
Kodi Lab → Kodi Lab Narrow Bridge**. The repository source can be added without
shell access or an already-paired bridge. Kodi may require its normal explicit
permission/warning for HTTP sources; do not disable unrelated protections.
Private-LAN HTTP does not authenticate the server against a malicious LAN actor.

The exact `bridge/service.kodi_lab_bridge-1.1.15.zip` remains a manual fallback.
Verify its SHA-256 against `bridge/BRIDGE.json` and the `BUNDLE.json` file record
before transferring it. The stable feed serves these identical reviewed bytes.

## Optional development repository

`BASE/install/` also lists `repository.kodi-lab-dev-1.0.0.zip`, clearly marked
for development/test Kodi installations. Install it separately through Kodi's
normal **Install from zip file** flow; installing the stable repository does
not install the development repository. Its descriptor points only at
`BASE/development/`. No Kodi Lab administrator credential belongs in this URL.

An authenticated operator opens **Kodi Repository** in the loopback Web UI,
selects a bounded Python video-plugin ZIP and its claimed Matrix/Nexus/Omega
compatibility, then reviews the exact ID, version, import UUID, SHA-256 and
tree digest. The final approval repeats the exact identity/hash. A new update
requires a greater explicit `major.minor.patch` source version; same-version
different bytes fail. The feed is development-only; Kodi Lab production target
authority is unchanged. Publication does not automatically trigger Kodi update
or workspace deployment. The default retained set is the current version plus
two prior versions and a seven-day grace period; count and byte caps can be
lowered through `KODI_LAB_DEVELOPMENT_REPOSITORY_MAX_ARTIFACTS` and
`KODI_LAB_DEVELOPMENT_REPOSITORY_MAX_BYTES` in `.env` (defaults 128 and 268435456).
Stop Web before `kodi-lab-admin backup`; Phase 2 backup includes the exact
development feed bytes and publication records. See `MANAGED-STATE-RECOVERY.md`.

For repository-mode workspace deployment, install and enable
`repository.kodi-lab-dev` on an external-development Kodi first. In Kodi,
check that the feed refreshes from the displayed LAN URL and set **Settings →
System → Add-ons → Updates** to **Notify, but don't install updates**. Confirm
both facts on Kodi Lab Web's **Kodi Repository** page; Kodi Lab also checks the
installed repository's exact identity using native inventory and its paired
bridge. A validated `workspace_deploy` then prepares a versioned package and
waits for an exact Web approval. Publication leaves deployment pending. For a
first install use **Add-ons → Install from repository → Kodi Lab Development**;
for an update use the Add-ons manager's **Check for updates** and **Available
updates**. Select the intended add-on, then return to Kodi Lab and choose
**Recheck installation**. A matching installed version alone is insufficient:
Kodi Lab verifies the complete tree through the authenticated bridge before
reporting deployment success. [Kodi Add-on manager](https://kodi.wiki/view/Add-on_manager)

1. On the remote Kodi box, enable Kodi's HTTP control service and allow the
   Kodi Lab server to reach its native Kodi port and the bridge port (normally
   `8765`) over the intended private network. Set a Kodi HTTP credential if
   that installation requires one.
2. Install the bridge from the stable Kodi Lab repository (or transfer the exact
   fallback ZIP). Enable **Kodi Lab Narrow Bridge** under service add-ons. Use
   Kodi's local add-on settings to open **Pair Kodi Lab**.
   The displayed four-character `otp-1` code lasts five minutes; a replacement
   has a cooldown. Do not retain the code in an issue log.
   If the bridge says it is paired to an obsolete Kodi Lab target, choose
   **Reset pairing** there, confirm locally, then open **Pair Kodi Lab** again
   to generate a new code. This keeps the Kodi profile and bridge identity.
3. For a development target, open Kodi Lab Web **Pair Kodi**. Enter the Kodi
   box's private address, the code, and ports if different from the defaults.
   The Web flow registers and verifies the native target, stages the new
   independent bridge credential, and displays the authenticated bridge
   identity. If the response is uncertain, use **Resume pairing**; do not
   submit the code a second time. Check the observed UUID, add-on version
   `1.1.15`, protocol `1`, Kodi version, and capabilities under **Connected
   Kodi targets**. Advanced external target administration supports explicit
   target approval and capability limits before development deployment.
4. For a production target, use **Production targets** instead. Register and
   verify its native identity, designate it as production, and register the
   exact reviewed bridge build using the ZIP hash, semantic fingerprint and
   source reference in `bridge/BRIDGE.json`. Record required private HTTP risk
   acknowledgements, then enter the code in that target's production pairing
   form. Check the provisional UUID, build fingerprint, protocol and Kodi
   identity against the reviewed record, and explicitly confirm provenance.
   Pairing alone grants no production scope. Follow `PRODUCTION-OPERATIONS.md`
   before authorizing screenshots or semantic remote actions.

The bridge provides authenticated identity and bounded screenshots. An
approved external development target may also use its pathless workspace
deployment capability. Production uses only the bridge screenshot capability;
production remote actions use separate native Kodi calls under explicit Web
policy. Native target registration, verification, status, and read-only Kodi
inspection do not require the bridge. Managed Kodi acquisition and Web
administration do not require a remote bridge either. Bridge HTTP HMAC protects
integrity, while screenshot pixels are cleartext on HTTP; use a trusted private
network or normally validated HTTPS where confidentiality is required.

For reproducible real-world issue reports, record the Git `source_commit` and
`source_tree`, SHA-256 of `BUNDLE.json`, bridge ZIP SHA-256, application
`saved_image_id`/`config_image_id` values, and the three runtime recipe IDs and
digests from `BUNDLE.json`. The local runtime acquisition record adds the exact
installed revision and image identity if a managed Kodi generation is used.
Record bounded error codes and target UUIDs, never pairing codes, credentials,
raw payloads or screenshot pixels.

## Optional local add-on source development

The default deployment exposes no host source directory. Remote Kodi control,
managed runtimes, uploaded ZIP testing, and reviewed runtime acquisition work
without one. To opt in, choose one host directory containing Kodi add-on source
projects. For example, `/srv/kodi-lab/addon-sources` is only an example. Create
and populate it before starting the development overlay. The directory must be
an existing normalized absolute path, not `/` or a symlink at any component.

After `configure` has created `.env`, run this from the unpacked bundle:

```sh
python3 linux-v1-bundle development-source . \
  --source '/srv/kodi-lab/addon-sources'
python3 linux-v1-bundle verify .
docker compose -f compose.release.yaml -f compose.development.yaml config --quiet
docker compose -f compose.release.yaml -f compose.development.yaml \
  up -d --wait --no-build web
```

The command and subsequent bundle verification validate the host directory
without following symlinks. The command writes only
`KODI_LAB_DEVELOPMENT_SOURCE` into the local, unbundled `.env`. It accepts
spaces in paths. Dot, parent, trailing slash, empty, relative, or missing paths
are rejected. Single/double quotes, `$`, `#`, backslashes, and control
characters in the host path are unsupported because the generated Compose
dotenv value cannot represent them safely. An unset or empty variable stops
Compose interpolation; `create_host_path: false` prevents a missing directory
from being created. The staged bundle contains no actual host source path.

The overlay binds that one host directory read/write into Web and read-only
into broker, both at fixed `/mnt/kodi-lab-development`, and sets
`KODI_LAB_DEVELOPMENT_ROOT` in both services to that path. Broker independently
resolves a registered, bounded relative locator; requests never carry a host
root or absolute source path. Broker requires the fixed read-only mount and
fails closed when it is absent, writable, or misconfigured. The shared Linux
source reader rejects symlink and nested-mount crossings beneath the root.
Web accepts only root-relative project locators through its existing registration
form; it does not browse host paths or create mounts. Register each project
beneath the chosen root on **Development Sources**. Registration, copy/sync,
workspace creation, deployment, execution, and MCP development tools continue
through the existing registered-source services. Uploaded ZIPs remain separate.

The bind is read/write because reviewed promotion can patch a registered
original after a Web review and explicit confirmation. Registration and ordinary
source copy need only read permission. The Web container runs as UID/GID
`10001:10001`; give it read/traverse access to the chosen root and source
trees, but keep the root directory itself non-writable to that account. Grant
write permission only within project trees intentionally enabled for patch-back.
Kodi Lab does not change ownership or permissions. A compromise of the Web
service account could modify every mounted file it can write, so select a
dedicated root and keep OS permissions narrow. Broker already holds Docker
authority and runs as root: its read-only bind expands its ability to read
material within the selected host root, even where Web's UID lacks access.
Place only intended development material there. Repository and managed Kodi
containers do not receive this mount. Kodi Lab does not automatically relabel
host files for SELinux or AppArmor. This feature is qualified for Linux amd64
V1; Windows Docker/Desktop source sharing remains separately unqualified.

For a managed Lab with local development, include the overlay after the network
overlay: `docker compose -f compose.release.yaml -f compose.lab.yaml -f
compose.development.yaml --profile lab up -d --wait --no-build web broker`.
Use this three-file command in place of the two-file Web/broker `up` commands
below, and keep the same overlay list on restarts. To return to remote-only
operation, stop Web and start it with only `compose.release.yaml`; the existing
source records remain listed but registration and source-backed operations are
unavailable.

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

## Paired development target and narrow MCP administration

After installing a candidate with migration 0057, sign in to the loopback Web
administrator page. Open **External development targets**, select the paired
target, and expand **Amend development capability maximum**. Review the target,
bridge UUID/version/protocol/fingerprint and current set. Select the exact new
set and confirm. Kodi Lab performs fresh native and authenticated bridge checks
before committing the new maximum and immutable receipt. For a repository-mode
video-plugin development target the set is `target_inspect`, `screenshot`,
`addon_inventory`, `workspace_deployment`, and `fixed_execution`. Reduce the
maximum through the same form after the workflow if those capabilities are no
longer needed. Reduction does not revoke or rotate the bridge credential;
revoke pairing separately if trust itself should end. A bridge identity change
or unsupported capability blocks the amendment and requires review.

Open **MCP clients** and create a custom preset under the **Add-on Development**
ceiling with exactly `workspace.inspect`, `workspace.deploy`, and
`development.execute`. The inspect permission is shared with validation and
read/status of a bound workspace. Create a client with that preset, issue a
short-lived bearer token, and deliver it through the protected
`KODI_LAB_MCP_TOKEN` environment variable; do not put it in command arguments
or logs. Assign the target policy preset, grant only the exact development target
and DevelopmentSource, and bind the Web-created workspace to this client. Do not
grant promotion, remote control, visual, or production permissions. Revoke the
token and exact grants or disable the client when the bounded acceptance ends.
These permissions cannot skip Web publication approval, manual Kodi
install/update, or Web Recheck of the exact installed tree.

## Candidate staging

From a reviewed checkout with every candidate change committed and a clean
worktree and index, run `scripts/stage-linux-v1-bundle EMPTY_OUTPUT_DIRECTORY`.
The scripts reject staged, unstaged, and nonignored untracked changes and record
the exact committed `HEAD` and `HEAD^{tree}` in the bundle and image labels.
Staging changes in the index alone is insufficient. The tooling does not publish
or upload anything.

## Stable repository restart and recovery

The static service verifies the selected generation and package checksums before
serving. Restarting Web or the repository service leaves `repository_data`
unchanged. To check after restart, run `kodi-lab-repository verify` through the
installed repository image and compare its `bundle_sha256`, `base_url`,
`bootstrap_zip_sha256`, `addons_xml_sha256`, `tree_sha256`, and package hashes
with the preserved installation record. The private application backup does not
include this separate volume; stable content is reproducible from the exact
candidate and URL. See `MANAGED-STATE-RECOVERY.md`. The repository service never
receives the private application volume or sockets.
