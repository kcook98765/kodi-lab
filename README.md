# Kodi Lab

Kodi Lab lets AI agents inspect, control, test and develop against Kodi through
the Model Context Protocol (MCP). Its Web administrator can manage isolated Kodi
instances for experiments or connect to Kodi installations you already use.

## Features

- Run multiple Kodi versions side by side.
- Connect existing Kodi installations without replacing them.
- Inspect the Kodi UI and capture screenshots.
- Test and debug add-ons in disposable development instances.
- Give MCP-compatible AI assistants access to the Kodi targets you choose.
- Manage runtimes, instances and agent permissions from a Web interface.

## Requirements

- **Ubuntu 24.04 on 64-bit Intel/AMD (x86_64/amd64)** is the qualified host baseline.
- **Docker Engine and Compose V2**, with Docker access for your user. Follow
  [Docker's Ubuntu installation guide](https://docs.docker.com/engine/install/ubuntu/).
  Rootless Docker is not qualified for managed runtimes.
- **Python 3.12, Git, sha256sum and [uv](https://docs.astral.sh/uv/getting-started/installation/)**.
- Internet access for Python packages, container images and upstream Kodi downloads.
- Capacity for application images and each Kodi instance. RC1 requires at least
  **8 GiB free staging space**, plus space for Docker images and profiles. There
  is no qualified universal RAM minimum; begin with one running instance.

Setup installs Python build/test packages automatically; editors and AI clients
are optional.

Windows/WSL2, macOS and ARM64 hosts are not yet qualified. An existing Kodi target
can be on another computer; these requirements apply to the Kodi Lab server.

## Quick Start — Install Kodi Lab

These steps are for a fresh installation. For an existing installation, use the
[upgrade and rollback guide](docs/LINUX-V1-UPGRADE-ROLLBACK.md).

**1. Clone and build the application bundle.** Run these commands in Bash as
your ordinary user, with the prerequisites above installed:

```sh
git clone https://github.com/kcook98765/kodi-lab.git
cd kodi-lab
uv sync --locked --dev
./scripts/stage-linux-v1-bundle ../kodi-lab-bundle
cd ../kodi-lab-bundle
sha256sum -c SHA256SUMS
python3 linux-v1-bundle verify .
docker image load --input kodi-lab-application-images.tar
```

Keep your clone unedited and use a new, empty bundle folder. It contains the Kodi
Lab installation, not Kodi. GitHub access is required.

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

**4. Open <http://127.0.0.1:18100/login> and sign in with your password.** Web
access defaults to the server's own computer. If your browser is elsewhere, run
this SSH tunnel on the browser's computer, replacing the login with your own:

```sh
ssh -N -L 18100:127.0.0.1:18100 your-user@your-server
```

Leave the tunnel running and open the same local URL. For ports, capacity planning
and troubleshooting, see the [full Linux installation guide](docs/LINUX-SOURCE-INSTALL.md).

## First-Time Setup

1. In Web, switch to **Development** and open **Managed Kodi runtimes**.
2. Choose a version and click **Install**. RC1 asks you to acknowledge that exact
   prerelease. Only your chosen runtime is downloaded, on demand. Kodi Lab verifies
   its binaries, assembles the local image and validates it before **Ready**.
   No Kodi compilation or manual Flatpak setup is needed; Kodi executables are
   not bundled with Kodi Lab.
3. Allow several minutes for installation. RC1's provisional estimate is
   **5–15 minutes**. The page updates automatically, and installation continues
   if you leave or close your browser. Return later to check progress.
4. Once Ready, create a managed instance, then use its **Provision** and **Start**
   controls. Same-version instances reuse this image without downloading Kodi again.
5. Check healthy status, open **View screen** and try UI inspection. For richer
   managed RC1 inspection, use **Install reviewed Bridge** on the instance page.

Stop, restart, reset and removal use the same instance controls. Save anything
you need before resetting or removing a disposable instance; see
[state recovery](docs/MANAGED-STATE-RECOVERY.md).

To connect an existing Kodi instead, open **Add Kodi** and follow the Bridge and
pairing instructions. See [existing-Kodi setup](docs/LINUX-V1-INSTALL.md#install-and-pair-a-remote-kodi-bridge).

## Connect Your AI Agent

Open **Agents** in Web, create a client and grant it access to your chosen Kodi
target. Start with **Read-only Inspection**; choose additional permissions only
for the actions you want. Save the token shown once in your agent's private
environment. Connection details and configuration examples are on **Agents**.

For **OpenCode on the same computer**, add this to
`~/.config/opencode/opencode.json`, merging it with any existing settings:

```json
{
  "mcp": {
    "kodi-lab": {
      "type": "remote",
      "url": "http://127.0.0.1:18100/mcp",
      "oauth": false,
      "headers": {
        "Authorization": "Bearer {env:KODI_LAB_MCP_TOKEN}"
      }
    }
  }
}
```

Set the token without putting it in a command argument, then launch OpenCode:

```sh
read -r -s -p "Token from Agents: " KODI_LAB_MCP_TOKEN
export KODI_LAB_MCP_TOKEN
opencode
```

Ask the agent to list its Kodi targets, then inspect one or capture a screenshot.
Each tool uses the selected target's ID. Configuration syntax and compatibility
vary by client.
See [OpenCode's MCP instructions](https://opencode.ai/docs/mcp-servers/),
the [client compatibility notes](docs/PHASE11-MCP.md#final-client-and-conformance-matrix)
and [setup for agents on another computer](docs/NATIVE-LAN-MCP.md).

## Supported Kodi Versions

| Managed runtime | Status |
| --- | --- |
| Kodi 19 Matrix **19.4** | Stable runtime |
| Kodi 20 Nexus **20.5** | Stable runtime |
| Kodi 21 Omega **21.3** | Stable runtime |
| Kodi 22 Piers **22.0 RC1** | **Preview / Prerelease — Development only** |

These are the reviewed Linux amd64 choices. RC1 is not a stable Home
recommendation. Kodi Lab itself is a prerelease development/testing checkpoint;
see [qualification notes](docs/RC1-TESTING-CHECKPOINT.md).

## Documentation

- [Linux installation and capacity planning](docs/LINUX-SOURCE-INSTALL.md)
- [Existing Kodi, add-on repository and local source setup](docs/LINUX-V1-INSTALL.md)
- [Managed RC1 workflow and known limitations](docs/RC1-WEB-WORKFLOW.md)
- [Agent add-on development](docs/PHASE12-MCP-DEVELOPMENT.md)
- [Screenshots and UI inspection](docs/UI-OBSERVATION.md)
- [Backup, upgrade and rollback](docs/LINUX-V1-UPGRADE-ROLLBACK.md)
- [Contributing](CONTRIBUTING.md)

Agents act only on targets and permissions you grant. Web and Kodi containers
do not receive the Docker socket; the broker manages container lifecycle.
See [architecture and security boundaries](docs/ARCHITECTURE.md).

## Support

Report problems through [GitHub Issues](https://github.com/kcook98765/kodi-lab/issues).
Support development through [GitHub Sponsors](https://github.com/sponsors/kcook98765).

## License

Kodi Lab and its Bridge are [MIT licensed](LICENSE). Upstream Kodi and other
components retain their own licenses; see [third-party notices](THIRD_PARTY_NOTICES.md).
Kodi Lab is independent of the Kodi project and is not an official Kodi product.
