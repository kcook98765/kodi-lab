# Independent Linux installation from a clean GitHub clone

This is the integration/testing path before the first public release. It builds
Kodi Lab's application images locally, then uses the existing verified bundle
installer. Kodi executables are not in Git, the wheel or the deployment bundle.
They are acquired only when you explicitly install a reviewed runtime in Web.

For a fresh installation with prerequisites already installed, the
[shared host installer](HOST-INSTALLER.md) automates the same bundle steps.
The manual procedure below remains available. Windows-hosted Linux containers
use the working [WSL2/Docker Desktop path](WINDOWS-WSL2-INSTALL.md);
validation of new installer revisions is separate from host-platform operation.

## Prerequisites and capacity

The qualified baseline is Ubuntu 24.04, Linux x86_64/amd64, Python
3.12, native rootful Docker Engine and Compose V2 (2.24.4 or newer).
Windows uses the maintained WSL2/Docker Desktop path linked above; broader
native qualification remains separate. macOS, ARM64 and rootless Docker are
not qualified for this managed-runtime path. Install
Git, uv, Python 3.12, `sha256sum`, Docker Engine and the Compose plugin using
their upstream installation instructions. Run development commands as your
ordinary user, with separately configured Docker-administrator access. Docker
administration is privileged; do not expose its socket to Web or Kodi.

```sh
git --version
python3 --version
uv --version
docker version
docker compose version
df -h .
free -h
ip -4 route show table all
docker network ls
```

The application requires `>=3.12,<3.13`; Kodi RC1's Python 3.14 is inside the
managed image. There is no qualified universal host RAM/disk minimum. RC1
acquisition checks for at least **8 GiB free staging space** and bounds its
staging data to 6 GiB, but Docker base images, image layers, build contexts and
application/test environments require additional space. Reserve capacity for
those as well as each instance's writable profile. Software-rendered instances
also consume RAM/CPU; start with one instance and measure your host. The source
build and runtime installation should run without competing heavy workloads.

Network access is required to GitHub, the locked Python package sources,
container registries, signed Ubuntu archives and, for RC1, `dl.flathub.org`.
Unavailable exact upstream inputs fail installation; Kodi Lab never substitutes
a newer beta, RC2 or final. No host Flatpak setup or Kodi compilation is needed.

Web defaults to **127.0.0.1:18100**. Choose an unused private-LAN bind, port and
durable URL for the add-on repository. Check these ports and the managed subnet
`10.250.0.0/24` against existing services, LAN/VPN routes and Docker networks.
See the bundle guide's temporary-network-capacity section if an explicitly
reserved installation subnet pool is needed. Managed Kodi's HTTP port 8080 is
internal; it is not published on the host.

## Clone and stage the application

**1. Clone and build the application bundle.** Run these commands in Bash as
your ordinary user, with the prerequisites above installed:

```sh
git clone https://github.com/kcook98765/kodi-lab.git
cd kodi-lab
git rev-parse HEAD
uv sync --locked --dev
./scripts/stage-linux-v1-bundle ../kodi-lab-bundle
cd ../kodi-lab-bundle
sha256sum -c SHA256SUMS
python3 linux-v1-bundle verify .
docker image load --input kodi-lab-application-images.tar
```

Keep your clone unedited and use a new, empty bundle folder. It contains the Kodi
Lab installation, not Kodi. GitHub access is required; use your normal GitHub
authentication if the repository is private, without credentials in the URL.
Record the source commit and preserve a trusted copy of the BUNDLE.json hash.

**2. Configure the add-on repository.** Kodi devices download the Bridge and
other Kodi Lab add-ons from this address. Enter this server's reachable private
LAN IPv4 address (on your home network), not `127.0.0.1`. Choose an unused port;
this example uses `18120`. It does not serve Kodi executables.
Check that ports `18100` and `18120` are available and that Kodi Lab's private
container network (`10.250.0.0/24`) does not conflict with your LAN, VPN or Docker
networks.

```sh
read -r -p "This server's private LAN IPv4 address: " repository_ip
python3 linux-v1-bundle configure . \
  --repository-bind "$repository_ip" --repository-port 18120 \
  --repository-base-url "http://${repository_ip}:18120"
docker compose -f compose.release.yaml config --quiet
python3 linux-v1-bundle preflight .
```

**3. Initialize data, set your password and start services.** Initialization
creates Kodi Lab's private data store. The password command prompts for your Web
administrator password; there is no default password.

```sh
docker compose -f compose.release.yaml --profile repository-init \
  run --rm --no-deps repository-init
docker compose -f compose.release.yaml run --rm --no-deps \
  --entrypoint kodi-lab-repository repository verify
docker compose -f compose.release.yaml run --rm --no-deps \
  --entrypoint kodi-lab-admin web initialize
docker compose -f compose.release.yaml run --rm --no-deps \
  --entrypoint kodi-lab-admin web set-password
docker compose -f compose.release.yaml --profile lab run --rm --no-deps \
  --entrypoint kodi-lab-runtime broker network-ensure
docker compose -f compose.release.yaml -f compose.lab.yaml --profile lab \
  up -d --wait --no-build web broker repository
```

Keep the bundle and its generated configuration. The last command starts services
in the background; reuse it from that directory for future restarts.

**4. Open http://127.0.0.1:18100/login on this computer and sign in.**
For ports, capacity planning and troubleshooting, see the
[capacity guidance](#prerequisites-and-capacity).

For a remote-only installation or detailed bundle administration, see
[Linux V1 installation](LINUX-V1-INSTALL.md). Ordinary startup refuses an
uninitialized or mismatched database; initialization is an explicit
administrator operation.

## Create your first managed Kodi

From **Overview** (`/kodi`), open **Managed Kodi** (`/managed-kodi`). On Omega's
card, select **Create Managed Kodi for this version…**, review the checks and
select **Install and prepare** once. Setup downloads and prepares Kodi, leaving
it stopped by default. Select **Return to Managed Kodi versions**, then **Start
Kodi**. When **Running** and **View Screen** are available, choose **View Screen →
Start Viewing**. Viewing starts view-only and needs no agent, Bridge pairing or
development workspace. See the [managed walkthrough](MANAGED-KODI-WEB.md) for
Stop, Reset and recovery. If you return to a paused setup,
choose **Check status**, then **Resume setup** for that same operation.

### Advanced runtime administration

If you followed the remote-only startup in the bundle guide, enable the existing
managed network and broker overlay from the configured bundle directory. The
README Quick Start already performs this step:

```sh
docker compose -f compose.release.yaml --profile lab run --rm --no-deps \
  --entrypoint kodi-lab-runtime broker network-ensure
docker compose -f compose.release.yaml stop web
docker compose -f compose.release.yaml -f compose.lab.yaml --profile lab \
  up -d --wait --no-build web broker
```
Open **Managed Kodi → Advanced managed administration → Runtime approval and
installation**. Stable Matrix
19.4, Nexus 20.5 and Omega 21.3 remain available. Kodi **22 Piers RC1** is
**Preview / Prerelease**, Development-only, Linux amd64, and is not the default
Home/stable recommendation.

Select **Install** on the chosen runtime. RC1 requires acknowledgement of that
exact prerelease recipe/revision. The worker acquires and verifies pinned
upstream artifacts, assembles the local image and validates it before Ready.
RC1 has a provisional **5–15 minute** estimate, supported by two cold runs of
**8m10.45s** and **9m3.02s**, not a guaranteed range. Progress updates
automatically; browser refresh, navigation or closure does not cancel the
installation. Return to the runtime page later. A failure displays a bounded
reason and offers retry only when the operation state permits it.

Once Ready, return to **Managed Kodi** and select **Start Kodi** for that version.
Starting an empty slot uses the approved cached runtime; a stopped profile is
reused. Only one managed Kodi runs at a time. Check native readiness and use
**View Screen → Start Viewing**; viewing and inspection have separate requirements. For RC1,
**Install reviewed Bridge** installs the exact reviewed development build
for that Kodi version (currently Bridge 1.1.32 for Piers).
Native health/screenshots remain available independently of rich Bridge trust.
Stock TagLib 2.3.1 has the documented Matroska fast-scan limitation; it is not
claimed equivalent to Kodi's patched library.

Stop/restart use the ordinary managed lifecycle. RC1 supports reviewed
clean-profile reset; unqualified historical Beta fixtures are not inherited.
Reset retires old Bridge trust. Start the clean instance and select **Install
reviewed Bridge** again to establish fresh trust. Remove/deprovision only your
disposable test instance after deciding what profile/snapshot state to preserve.
See [RC1 workflow](RC1-WEB-WORKFLOW.md) and [managed state
recovery](MANAGED-STATE-RECOVERY.md) for the precise preservation boundaries.

## Configure an MCP client

Follow the [read-only agent setup](../README.md#connect-your-ai-agent) in Web
**Agents & Permissions** (`/mcp-clients`). Production Inspection applies only to
External Production Kodi and does not authorize Managed Kodi.
For inspection-only managed access, assign the same custom `target.inspect`
preset to the client and Kodi policy, then grant that exact Kodi. Protect the
once-shown credential in the client's private environment/configuration, never
in a command argument or issue log.

For OpenCode running on the same computer, use the
[README HTTP example](../README.md#connect-your-ai-agent). It reads the token
from the agent's environment and connects to `http://127.0.0.1:18100/mcp` without
requiring a separately enabled LAN endpoint. Fresh localhost HTTP inspection
passed with OpenCode 1.19.3 on Ubuntu 24.04; client configuration syntax and
protocol support vary.

For a local stdio client supporting MCP `2026-07-28`, install the application in
its own Python 3.12 environment (`uv sync --locked` already supplies one for a
checkout). Configure
command `kodi-lab-mcp-stdio` from that environment with:

```text
KODI_LAB_MCP_URL=http://127.0.0.1:18100/mcp
KODI_LAB_MCP_TOKEN=<the credential shown once in Agents & Permissions>
```

The explicit URL matches the bundle port; the proxy's host-process default is
8100. The stdio proxy does not support older initialization handshakes; consult
the [client compatibility matrix](PHASE11-MCP.md#final-client-and-conformance-matrix)
before choosing it. For a client on another computer, follow [native LAN MCP
setup](NATIVE-LAN-MCP.md); opening Web does not enable LAN MCP automatically.
Do not copy another installation's tokens, grants or pairing records.

Verify `target_list`, then use the discovered target UUID for `target_status`
and `addon_list`. An unauthorized target must remain undiscoverable/denied.
Screenshots use the same inspection permission; external Kodi additionally needs
Allow screenshots set to On and verified Bridge support. Richer UI/log inspection
can require a Bridge, and control has separate authority. These are not needed
for this first read-only check.

## Upgrade, rollback and issue evidence

Keep the exact clone commit, bundle/image identities and recipe digests. Review
[known checkpoint test results](RC1-TESTING-CHECKPOINT.md) when reporting
independent-system regression results. Follow
[backup and recovery](LINUX-V1-UPGRADE-ROLLBACK.md) for the supported baseline
and future application transitions. Unsupported prerelease databases are
refused; do not stamp them or attempt a prerelease conversion. Retain consistent private-state backups and runtime images;
never use a Docker prune, volume deletion or direct SQLite edit as rollback.
Report bounded errors and versions without credentials, pairing codes, database
copies or personal screenshot contents.
