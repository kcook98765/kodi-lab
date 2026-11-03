# Kodi Lab

**Self-hosted Kodi development, testing, and MCP integration for AI agents.**

Kodi Lab provides a Web interface and a permission-controlled Model Context
Protocol (MCP) server for working with Kodi. Connect installations you already
use, or install isolated Kodi runtimes on demand to develop, test, and debug
add-ons.

Bring any compatible AI client and model you choose. Kodi Lab does not run an AI
model or require cloud AI credentials of its own. It is intended for Kodi add-on
developers, Kodi users who want diagnostics or controlled automation, and people
using MCP-compatible coding agents.

## Features

- Connect existing Kodi installations using JSON-RPC and the optional Narrow
  Bridge add-on, without OS-level access to those hosts.
- Install reviewed Kodi versions on demand and switch between isolated profiles;
  **only one managed Kodi instance runs at a time**.
- Develop and test one add-on workspace in **Current Kodi**, using disposable
  profiles or explicitly enabled retained profiles for each version.
- Inspect Kodi UI state and capture screenshots. View supported managed Kodi
  through [noVNC](docs/STANDARD-NOVNC.md) (about 2 FPS, view-only by default,
  with optional human control; no audio).
- Grant MCP-compatible agents access only to selected Kodi targets and
  explicitly authorized operations.
- Manage runtime installation, connections, permissions, and development
  workflows from the Web UI.

## Architecture at a glance

![Kodi Lab architecture and data flow](docs/images/kodi-lab-architecture.svg)

The diagram illustrates how the **Web UI** and **MCP-compatible agents** use
Kodi Lab's common control and permissions layer to work with both existing
external Kodi installations and isolated, on-demand managed Kodi runtimes.
Managed instances provide browser viewing through noVNC and lightweight
screen previews. External targets use Kodi JSON-RPC and, where installed,
the optional Narrow Bridge add-on; Kodi Lab does not obtain OS-level access
to external Kodi hosts.

This is a conceptual overview, not a network-port inventory or a security
boundary specification. See [architecture](docs/ARCHITECTURE.md),
[security](SECURITY.md) and [standard noVNC](docs/STANDARD-NOVNC.md)
for implementation details.

## Requirements

- **Ubuntu 24.04 on 64-bit Intel/AMD (x86_64/amd64)** is the qualified host baseline.
- **Docker Engine and Compose V2**, with Docker access for your user. Follow
  [Docker's Ubuntu installation guide](https://docs.docker.com/engine/install/ubuntu/).
  Rootless Docker is not qualified for managed runtimes.
- **Python 3.12, Git, sha256sum and [uv](https://docs.astral.sh/uv/getting-started/installation/)**.
  The Ubuntu download example also uses `curl`; a browser download works too.
- Internet access for Python packages, container images and upstream Kodi downloads.
- Capacity for application images and each Kodi instance. RC1 requires at least
  **8 GiB free staging space**, plus space for Docker images and profiles. There
  is no qualified universal RAM minimum; begin with one running instance.

Setup installs Python build/test packages automatically; editors and AI clients
are optional.

Windows 11 with WSL2, Ubuntu 24.04 and Docker Desktop has a reported working
configuration, including managed Nexus/Omega and MCP. Follow the maintained
[Windows installation path](docs/WINDOWS-WSL2-INSTALL.md); fresh-host qualification
remains pending under [issue #8](https://github.com/kcook98765/kodi-lab/issues/8).
macOS and ARM64 hosts are not qualified. An existing Kodi target can be on another
computer; these requirements apply to the Kodi Lab server.

For daily add-on development, follow the
[Current Kodi walkthrough](docs/DEVELOPMENT-SESSION.md#everyday-walkthrough).
Existing installations may need to explicitly enable **Retained Kodi versions**;
profile reuse and runtime/viewer eligibility depend on the installed version.
See [standard noVNC viewing](docs/STANDARD-NOVNC.md) for the exact supported
runtime revisions and older-profile limitations.

## Quick Start — Install Kodi Lab

These steps are for a fresh installation. For an existing installation, use the
[upgrade and rollback guide](docs/LINUX-V1-UPGRADE-ROLLBACK.md).

With the prerequisites installed, download and run one installer script.
On **Ubuntu**, use Bash:

```sh
curl -fL https://raw.githubusercontent.com/kcook98765/kodi-lab/main/scripts/install.py -o install-kodi-lab.py
python3 install-kodi-lab.py bootstrap
```

On **Windows**, use PowerShell after enabling Ubuntu's Docker Desktop/WSL2 integration:

```powershell
Invoke-WebRequest -UseBasicParsing https://raw.githubusercontent.com/kcook98765/kodi-lab/main/scripts/install-windows.ps1 -OutFile install-kodi-lab.ps1
.\install-kodi-lab.ps1
```

The scripts obtain a clean source checkout in the Linux user's data directory
and default to `~/kodi-lab-install`. Windows discovers that user's Linux home
automatically. No manual checkout path is needed. The selected source commit is
recorded; an exact commit can be selected with `--revision` / `-Revision`.

It prompts for your private LAN repository address and Web administrator
password, then builds/verifies the application bundle, initializes fresh state
and starts services. Open **http://127.0.0.1:18100/login** on this computer,
sign in with your chosen password and continue
with [First-Time Setup](#first-time-setup). See the
[installer guide](docs/HOST-INSTALLER.md) for prerequisites, configuration,
start/stop and recovery. Windows users follow the
[WSL2 guide](docs/WINDOWS-WSL2-INSTALL.md), including its explicit repository
forwarding setup.

### Manual installation

These steps are for operators who prefer manual installation. Most new users
should use the installer above. Both paths use the same verified bundle and
initialization commands.

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

**4. Open http://127.0.0.1:18100/login on this computer and sign in.**
For ports, capacity planning and troubleshooting, see the
[full Linux installation guide](docs/LINUX-SOURCE-INSTALL.md).

### Web access and optional HTTPS

**Local Web access works immediately after a fresh installation.** Open
[http://127.0.0.1:18100/login](http://127.0.0.1:18100/login) on the installation
computer. No certificates or reverse proxy are needed for local login or
standard noVNC viewing. The local MCP endpoint uses separate bearer-token
authentication.

For browsers on **another computer**, configure
[Web networking](docs/WEB-NETWORKING.md). Ubuntu offers an explicit private-LAN
HTTP option; HTTPS is also supported through a configured proxy. LAN HTTP is
**unencrypted**, including passwords, session cookies, screenshots, and control
traffic. Never expose it to the public Internet. Windows/WSL2 uses its separate
[networking guidance](docs/WINDOWS-WSL2-INSTALL.md).

<a id="web-https-prerequisite"></a>
Existing HTTPS installations continue to require trusted HTTPS/WSS after upgrade;
they do not automatically switch to local HTTP. Preserve their proxy and
certificate configuration. See
[HTTPS configuration](docs/WEB-NETWORKING.md#https-with-a-user-managed-proxy).
The separately configured Kodi add-on repository address is not the Web login
address.

## First-Time Setup

1. In Web, open **Development → Managed Kodi runtimes**, choose a reviewed Kodi
   version, and select **Install**. Kodi executables are not bundled with Kodi
   Lab: only the chosen runtime is downloaded, verified, and assembled locally.
   Piers 22 RC1 requires explicit prerelease acknowledgement.
2. Wait for **Ready**. Installation continues after you close the browser.
   The provisional first-install estimate for Piers RC1 is **5–15 minutes**;
   actual time depends on downloads and the host.
3. For add-on development, go to **Kodi → Current Kodi**. Choose the registered
   source/workspace and runtime, then follow the **Prepare Kodi**, source-setup,
   **Start Kodi**, and native/Bridge readiness steps as applicable. See the
   [everyday walkthrough](docs/DEVELOPMENT-SESSION.md#everyday-walkthrough).
   Retained version profiles are opt-in on existing installations; only one
   managed Kodi runs at a time.
4. For manual instance administration instead, use the Advanced instance
   controls to create, **Provision**, and **Start** an instance. These are not
   required steps for every Current Kodi workflow.
5. For eligible managed runtimes, use **Start Viewing** in Current Kodi or
   capture a screenshot. Viewing starts in read-only mode; **Take Control**
   explicitly transfers input to the browser. More detailed UI inspection can
   require the reviewed Narrow Bridge setup.

Save anything important before resetting or removing a disposable profile; see
[managed state recovery](docs/MANAGED-STATE-RECOVERY.md). Installing a newer
reviewed runtime recipe does not automatically migrate existing profiles.

To connect an existing Kodi installation instead, use **Add Kodi** and its
pairing workflow. See [external Kodi setup](docs/LINUX-V1-INSTALL.md#install-and-pair-a-remote-kodi-bridge),
[reconnection](docs/KODI-RECONNECT.md), and
[repository address recovery](docs/REPOSITORY-ADDRESS-RECOVERY.md).

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

These are the reviewed Linux amd64 runtime options; Kodi executables are acquired
on demand, not included in Kodi Lab. Piers RC1 is not a stable Home recommendation.
Standard noVNC viewing is qualified only for
[exact reviewed recipe revisions](docs/STANDARD-NOVNC.md), not automatically for
older profiles. Kodi Lab remains a prerelease development/testing project.
Earlier [RC1 test checkpoints](docs/RC1-TESTING-CHECKPOINT.md) are historical
qualification records, not a live test-status report.

## Documentation

- **Getting started:** [Installer and everyday management](docs/HOST-INSTALLER.md),
  [Linux manual installation and capacity](docs/LINUX-SOURCE-INSTALL.md),
  [Windows/WSL2 setup](docs/WINDOWS-WSL2-INSTALL.md), and
  [Web access and optional HTTPS](docs/WEB-NETWORKING.md).
- **Kodi and add-on development:** [Current Kodi walkthrough](docs/DEVELOPMENT-SESSION.md),
  [external Kodi and repository setup](docs/LINUX-V1-INSTALL.md),
  [managed development workflow](docs/MANAGED-AGENT-LOOP.md), and
  [UI inspection](docs/UI-OBSERVATION.md).
- **Agents and architecture:** [MCP development reference](docs/PHASE12-MCP-DEVELOPMENT.md),
  [remote-agent networking](docs/NATIVE-LAN-MCP.md), and
  [architecture and security boundaries](docs/ARCHITECTURE.md).
- **Operations:** [Managed state recovery](docs/MANAGED-STATE-RECOVERY.md),
  [backup, upgrade and rollback](docs/LINUX-V1-UPGRADE-ROLLBACK.md),
  and [optional repository HTTPS](docs/REPOSITORY-NETWORKING.md).
- **Project:** [Contributing](CONTRIBUTING.md),
  [Code of Conduct](CODE_OF_CONDUCT.md), [security reporting](SECURITY.md),
  and [accessibility](ACCESSIBILITY.md).

Agents can act only on targets and operations you authorize. Web and Kodi
containers do not receive the Docker socket; the restricted broker manages
container lifecycle.

## Support

Report problems through [GitHub Issues](https://github.com/kcook98765/kodi-lab/issues).
Support development through [GitHub Sponsors](https://github.com/sponsors/kcook98765).

## License

Kodi Lab and its Bridge are [MIT licensed](LICENSE). Upstream Kodi and other
components retain their own licenses; see [third-party notices](THIRD_PARTY_NOTICES.md).
Kodi Lab is independent of the Kodi project and is not an official Kodi product.
