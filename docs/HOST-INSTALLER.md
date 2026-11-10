# Install Kodi Lab with one entry point

After installing the prerequisites, run a downloaded installer or use a clean Git
checkout. It builds the existing verified application bundle and runs the same
initialization commands as the manual installation. It does not install Docker,
change firewall rules, download Kodi automatically, create agent credentials or
enable Windows sign-in startup.

Native Ubuntu 24.04 amd64 remains the qualified baseline. Windows has prior
owner-accepted installation results through the
[WSL2/Docker Desktop path](WINDOWS-WSL2-INSTALL.md); the current managed development
workflow has [separate qualification in #275](https://github.com/kcook98765/kodi-lab/issues/275).
Native Windows server execution, macOS, ARM64, WSL1, rootless and remote Docker
engines are outside this installer path.

## Prerequisites

On Ubuntu 24.04 x86_64, install Git, Python 3.12, Bash, `iproute2`, `sha256sum`,
[uv](https://docs.astral.sh/uv/getting-started/installation/) and rootful
[Docker Engine with Compose](https://docs.docker.com/engine/install/ubuntu/).
Compose must be **2.24.4 or newer**. Configure Docker access for your ordinary
user and reopen the terminal if group membership changed. Do not run the
installer with `sudo`. Windows users install Docker Desktop with WSL integration
instead of a second Docker daemon in Ubuntu.

```sh
git --version
python3 --version
uv --version
docker context inspect --format '{{.Endpoints.docker.Host}}'
docker info --format '{{.OSType}} {{.Architecture}}'
docker compose version --short
```

The Docker endpoint must be `unix:///var/run/docker.sock`; the server must be
Linux amd64 and rootful. The installer records its engine ID so later commands
cannot silently manage a different daemon. The CLI must see the same daemon
whose fixed socket the broker mounts.

Reserve at least **8 GiB free** in the destination filesystem before starting,
plus additional space in Docker's storage for application/runtime images and
profiles. This check is a starting floor, not a total capacity estimate. There
is no qualified universal RAM minimum; begin with one Kodi instance. See
[capacity planning](LINUX-SOURCE-INSTALL.md#prerequisites-and-capacity).
Internet access is required for locked Python dependencies, container images
and later reviewed Kodi acquisition. Node is needed for development tests, not
for installation.

## Fresh Ubuntu installation

Download [the Python installer](https://raw.githubusercontent.com/kcook98765/kodi-lab/main/scripts/install.py),
then run it from an ordinary-user terminal:

```sh
python3 install.py bootstrap
```

No source checkout or path arguments are required. `bootstrap` defaults to
`~/kodi-lab-install`, obtains a fresh detached checkout under
`${XDG_DATA_HOME:-~/.local/share}/kodi-lab/sources`, and runs the existing installer
from it. By default it resolves `main` once and records the exact source commit.
For reproducible selection, pass `--revision` with a full lowercase 40-character
commit ID. Windows' downloaded PowerShell launcher resolves that commit before
retrieving the Python script, so both downloads use the same revision.
Downloaded scripts require the same host prerequisites as checkout installation.
Source checkouts are retained for diagnosis; failures do not delete state or
silently reuse an existing installation directory.

Choose a private-LAN IPv4 address on this server, preferably reserved in DHCP.
Ensure `18100` and `18120` are unused and `10.250.0.0/24` does not overlap your
LAN, VPN or Docker networks. The installer checks visible Linux routes and
Docker networks. Windows users must check Windows routes too.

```sh
python3 install.py bootstrap --directory "$HOME/kodi-lab-install"
```

Enter this server's private LAN address when prompted, then choose the Web
administrator password at the existing password prompt. No password or token
belongs in a command argument. To supply network choices explicitly:

```sh
python3 install.py bootstrap --directory "$HOME/kodi-lab-install" \
  --repository-bind 192.168.1.50 --repository-port 18120 --web-port 18100
```

Replace the example address. Default repository HTTP needs no certificates or
proxy. For optional owner-managed HTTPS, see [repository networking](REPOSITORY-NETWORKING.md).
`--repository-base-url` accepts an explicit
credential-free origin if devices use a durable hostname or configured HTTPS
proxy; otherwise it defaults to `http://ADDRESS:18120`. It must identify the
actual reachable repository. The installer does not create DNS or TLS services.
Prefer a stable LAN DNS name or DHCP reservation before generating packages.
See [repository address recovery](REPOSITORY-ADDRESS-RECOVERY.md) before changing
an existing origin; regenerating with a new address is not a migration.

Use a new destination outside the checkout, with an existing parent. Checkout
and destination must be on the Linux filesystem, not under `/mnt`. The installer
refuses an existing managed network rather than attaching a fresh database to
another lab's runtimes. Do not delete a network to bypass this refusal; inspect
its ownership and choose an independent installation environment.

The installer builds and verifies the binary-free bundle, loads its exact
application images, configures them by image ID, initializes/verifies the
repository, initializes the database, prompts for your password, ensures the
managed network and starts Web, broker and repository with health checks. It
also verifies that the login page responds and anonymous MCP access returns
`401 Bearer` on the retained loopback MCP endpoint in every mode. LAN HTTP
also checks its browser-only listener rejects MCP. Authorized inspection is a
separate first-run check below.

## First run and authorized inspection

The hidden administrator password step allows three total attempts. A mismatch
or a policy rejection lets you enter both fields again in the same install run.
Ctrl+C, EOF, unavailable hidden input or three unsuccessful attempts stop setup
without changing credentials. The installer preserves the database and its
`database_initialized` receipt stage; follow the partial-install guidance below
rather than deleting state or rerunning initialization.

Open **http://127.0.0.1:18100/login** on the installation computer and use your
chosen password. Fresh installation records `local-http`; no certificate or
proxy is needed. Standard noVNC works over HTTP/WebSocket. Cookies retain
HttpOnly and SameSite=Strict; HTTP does not encrypt network traffic.

1. From **Overview** (`/kodi`), open **Managed Kodi** (`/managed-kodi`). For a first
   setup, choose **Create Managed Kodi for this version…** on Omega's card.
2. Review the checks and select **Install and prepare** once. Downloads and
   preparation can take several minutes. Setup leaves Kodi stopped by default;
   select **Return to Managed Kodi versions**, then **Start Kodi**.
3. Wait for **Running** and **View Screen** to become available, then select
   **View Screen → Start Viewing**. Viewing starts view-only and needs no agent,
   Bridge pairing or development workspace. **Take Control** is explicit.

If that version is already prepared, use **Start Kodi** directly. **Stop Kodi**
keeps its profile; **Reset Kodi** deliberately discards it. See the
[managed walkthrough](MANAGED-KODI-WEB.md) for retention and recovery details.

Refresh or return to the page to find the same saved setup. If it is paused,
use **Check status**, then **Resume setup**; do not start a second installation.

Connecting an agent is optional. Follow the
[README read-only agent setup](../README.md#connect-your-ai-agent) to choose the
correct external-production or managed-Kodi policy, grant that specific Kodi and
protect the once-shown token. Production Inspection is External Production Kodi
only; Managed Kodi also needs an explicitly assigned compatible policy. Verify
`target_list`, `target_status` and `addon_list` through the existing localhost HTTP MCP endpoint.
Local MCP remains available when LAN HTTP Web access is enabled; remote MCP
requires its own explicit configuration.
Unauthorized targets must remain absent or denied. Record the client version,
source commit, runtime version and sanitized result; do not share tokens.

To connect an existing Kodi instead, choose **External Production Kodi** for a
household device or **External Development Kodi** for another development device
from Overview, then use its **Connect** action and normal pairing process. This
includes native Windows Kodi and is separate from the Lab host platform. See
[external setup](LINUX-V1-INSTALL.md#install-and-pair-a-remote-kodi-bridge).

### Optional Web networking

For Ubuntu access from another computer on a trusted LAN, add
`--web-lan-ip YOUR_PRIVATE_IPV4` to `bootstrap` or `install`. The installer warns
about unencrypted passwords, sessions and screen/control traffic, then asks you
to type that exact IP to confirm. It binds only that address, shows the LAN login
URL and preserves local MCP at `http://127.0.0.1:18100/mcp`. A separate socket
serves LAN browsers and rejects MCP independently of request headers. This
option is unavailable on WSL2; its existing Windows networking path remains separate. See
[advanced networking](WEB-NETWORKING.md) for examples and limitations.

To require a user-managed HTTPS proxy, select `--web-access-mode https` when
installing. Windows users can instead opt into
[local Caddy HTTPS/WSS](WINDOWS-WSL2-INSTALL.md#optional-local-httpswss-ingress),
which selects HTTPS automatically. Configure and trust the proxy before login;
direct HTTP login is disabled in HTTPS mode. Restart reads the saved mode/bind
and refuses drift. Compatibility defaults are documented in the
[operator reference](LINUX-V1-UPGRADE-ROLLBACK.md#preserve-web-security-mode).
Do not rerun fresh initialization or edit historical receipts to change modes.

## Start, stop and diagnose

The destination contains an unchanged verified `bundle/`, a private
`INSTALLATION.json` receipt, and a copy of the management helper, `kodi-lab.py`.
The receipt pins the Compose project, Docker engine and bundle identity. Named
Docker volumes hold durable application/repository state; deleting the checkout
does not constitute a backup. Keep the receipt and destination together.

```sh
python3 "$HOME/kodi-lab-install/kodi-lab.py" status --directory "$HOME/kodi-lab-install"
python3 "$HOME/kodi-lab-install/kodi-lab.py" stop --directory "$HOME/kodi-lab-install"
python3 "$HOME/kodi-lab-install/kodi-lab.py" start --directory "$HOME/kodi-lab-install"
python3 "$HOME/kodi-lab-install/kodi-lab.py" compose --directory "$HOME/kodi-lab-install" \
  -- logs --tail 100 web broker repository
```

**Stop** stops application services; stop managed Kodi instances in Web first
if you need the whole lab quiescent. It does not delete containers, volumes or
networks. **Start** reuses existing state without initialization, password reset,
image builds or updates. Ambient `COMPOSE_*` and `KODI_LAB_*` variables cannot
override the helper's explicit project/configuration. The helper permits the
service commands `config`, `exec`, `logs`, `ps`, `run`, `stop` and `up`; global
project/file/profile overrides are refused.

Use **Managed Kodi**, **External Production Kodi** or **External Development Kodi**
for the relevant environment, and the existing
[Kodi log tool](KODI-LOGS.md) for supported target diagnostics. Application logs
and Kodi logs may contain private information; sanitize excerpts before sharing.

## Failure, upgrade and recovery

Failures leave the destination and `completed_stage` receipt intact. A rerun of
`install` refuses that directory. The last completed stage does not prove a later
command had no effect: an interruption may occur after its mutation and before
the receipt update. Inspect the error, current state and receipt before manually
continuing. No automatic cleanup, migration retry or rollback runs.

After the `configured` stage, the helper's `compose` action can run the normal
explicit administrator commands, for example `-- ps` or `-- logs --tail 100`.
`start` requires a fully completed installation. Before configuration is complete,
inspect the preserved files and follow the equivalent manual steps. Do not
delete volumes or rerun `initialize` to bypass a partially initialized database.

For compatible-state backups, restore and future application transitions, use
[backup and rollback](LINUX-V1-UPGRADE-ROLLBACK.md) and
[state recovery](MANAGED-STATE-RECOVERY.md). The initial schema is
`0001_initial_release`; unsupported prerelease state is rejected and has no
conversion path. Keep the original receipt, bundle, private state, runtime
images and snapshot artifacts. Never edit identity receipts, delete volumes or
use Docker prune as recovery.

The [manual Linux source steps](LINUX-SOURCE-INSTALL.md) remain available for
operators who want to perform each action themselves.
