# Independent Linux installation from a clean GitHub clone

This is the integration/testing path before the first public release. It builds
Kodi Lab's application images locally, then uses the existing verified bundle
installer. Kodi executables are not in Git, the wheel or the deployment bundle.
They are acquired only when you explicitly install a reviewed runtime in Web.

## Prerequisites and capacity

The qualified development baseline is Ubuntu 24.04, Linux x86_64/amd64, Python
3.12, native rootful Docker Engine and Compose V2. Windows, macOS, ARM64 and
rootless Docker are not qualified hosts for this managed-runtime path. Install
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

Clone the `main` development/testing checkpoint. For a
private repository, authenticate Git using your normal GitHub mechanism; do not
put credentials in the URL. Record the resulting commit for issue reports.

```sh
git clone https://github.com/kcook98765/kodi-lab.git
cd kodi-lab
git rev-parse HEAD
git status --short
uv sync --locked --dev
./scripts/stage-linux-v1-bundle ../kodi-lab-test-bundle
```

Staging requires a clean committed checkout and an empty output directory
outside it. It builds Web/broker images and saves them into a local bundle; it
does not publish them, download Kodi, or initialize an existing database. Do not
use an existing production bundle/state directory for an independent test.

The [README Quick Start](../README.md#quick-start--install-kodi-lab) includes the
complete fresh-install commands and starts Web, repository and the managed
broker together. Alternatively, follow
[Linux V1 installation](LINUX-V1-INSTALL.md#install-kodi-lab-with-no-kodi-image)
from the new bundle directory. That procedure verifies checksums, loads the
application images, configures site-specific repository settings, initializes
the isolated state and prompts for an administrator password. Preserve the
bundle, its trusted `BUNDLE.json` hash and generated private configuration.
The ordinary startup command refuses an uninitialized or mismatched database;
initialization/upgrade are explicit administrator operations.

Open `http://127.0.0.1:18100/login` on the new host and sign in. For access from
another computer, use a deliberate authenticated/tunneled Web configuration;
the default loopback binding is intentional. No test credentials are provided.

## Install, create and inspect managed Kodi

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
Switch Web to **Development** and open **Managed Kodi runtimes**. Stable Matrix
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

Once Ready, use **Create managed instance**, then the ordinary provision/start
controls. Additional instances reuse the installed runtime image rather than
downloading Kodi again. Check healthy status, **View screen** and UI inspection;
navigate to another window and verify that its screenshot changes. For RC1,
**Install reviewed Bridge** installs the exact development candidate 1.1.16.
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

In Web **Agents**, create the appropriate client preset and explicitly grant
your managed target. Start with inspection permissions; grant bounded control
only if navigation is intended. Copy the credential shown once into the client's
private environment/configuration, never into a command argument or issue log.

For OpenCode running on the same computer, use the
[README HTTP example](../README.md#connect-your-ai-agent). It reads the token
from the agent's environment and connects to `http://127.0.0.1:18100/mcp` without
requiring a separately enabled LAN endpoint. The retained HTTP qualification is
OpenCode 1.14.39; client configuration syntax and protocol support vary.

For a local stdio client supporting MCP `2026-07-28`, install the application in
its own Python 3.12 environment (`uv sync --locked` already supplies one for a
checkout). Configure
command `kodi-lab-mcp-stdio` from that environment with:

```text
KODI_LAB_MCP_URL=http://127.0.0.1:18100/mcp
KODI_LAB_MCP_TOKEN=<the credential shown once in Agents>
```

The explicit URL matches the bundle port; the proxy's host-process default is
8100. The stdio proxy does not support older initialization handshakes; consult
the [client compatibility matrix](PHASE11-MCP.md#final-client-and-conformance-matrix)
before choosing it. For a client on another computer, follow [native LAN MCP
setup](NATIVE-LAN-MCP.md); opening Web does not enable LAN MCP automatically.
Do not copy another installation's tokens, grants or pairing records.

Verify `target_list`, then use the discovered explicit target UUID for
`target_status`, `inspect_ui_state` and `screenshot_capture`. An unauthorized
target must remain undiscoverable/denied. If granted, test a bounded
`remote_action` and observe the changed UI. MCP does not own managed lifecycle;
stop/reset/remove remain Web administrator actions.

## Upgrade, rollback and issue evidence

Keep the exact clone commit, bundle/image identities and recipe digests. Review
[known checkpoint test results](RC1-TESTING-CHECKPOINT.md) when reporting
independent-system regression results. Follow
[upgrade/rollback](LINUX-V1-UPGRADE-ROLLBACK.md), especially its RC1 provenance
boundary: schema 0071 alone does not make older eligibility code safe after RC1
approvals exist. Retain consistent private-state backups and runtime images;
never use a Docker prune, volume deletion or direct SQLite edit as rollback.
Report bounded errors and versions without credentials, pairing codes, database
copies or personal screenshot contents.
