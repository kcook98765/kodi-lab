# Kodi Lab

**Agent-operated Kodi add-on development and testing, with a simple operator Web UI.**

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
  Broker admission prevents a second writable profile for the same version;
  conflicting older profiles require administrator review without automatic deletion.
- Grant **Trusted Managed Lab** once so an agent can create or import its own
  projects, edit, deploy, launch, inspect and Reset installed managed versions.
- Inspect Kodi UI state and capture screenshots. View supported managed Kodi
  through [noVNC](docs/STANDARD-NOVNC.md) (about 2 FPS, view-only by default,
  with optional human control; no audio).
- Keep external Kodi under independent per-device permissions, including for
  agents that already have Trusted Managed Lab authority.
- Use the Web UI to install managed versions, connect external Kodi, authorize
  agents, view screens and Take Control. Recovery and legacy project controls
  remain under **Advanced**.

## Architecture at a glance

![Kodi Lab architecture and data flow](docs/images/kodi-lab-architecture.svg)

Web and MCP share Kodi Lab's control and permissions layer. The restricted
broker manages containers; external Kodi uses its authorized interfaces.
The diagram is a conceptual overview. See [architecture](docs/ARCHITECTURE.md)
and [security](SECURITY.md) for the implementation boundaries.

## Managed and external Kodi

After signing in, **Overview** (`/kodi`) links to three choices:

| Choice | Use it to |
| --- | --- |
| **Managed Kodi** (`/managed-kodi`) | Run Kodi hosted by Kodi Lab for testing, with a separate profile per version. |
| **External Kodi** (`/external-kodi`) | Connect an existing household or development Kodi, with independent permissions. |
| **Agents** (`/mcp-clients`) | Connect an agent and deliberately grant its managed or external authority. |

Managed setup downloads and validates a reviewed runtime when needed. Starting
and viewing Managed Kodi needs no agent, workspace or Bridge pairing. Trusted
agents manage their own projects through MCP without Web workspace selection.
Legacy selected-project workflows remain under **Advanced → Development tools**;
see the [walkthrough](docs/DEVELOPMENT-SESSION.md#everyday-walkthrough).

The external pages offer **Connect External Production Kodi** or **Connect External
Development Kodi** through the existing connection and pairing workflow. Kodi Lab
does not host or own those installations and never grants automatic production access.
Screenshots use ordinary inspection authorization; each external Kodi also
requires Allow screenshots set to On (default Off). Control and development
have separate permission requirements. Kodi Lab provides no general-purpose shell or OS access to that
device; see [external setup](docs/LINUX-V1-INSTALL.md#install-and-pair-a-remote-kodi-bridge).

## Requirements

- **Ubuntu 24.04 on 64-bit Intel/AMD (x86_64/amd64)** is the qualified host baseline.
- **Docker Engine and Compose V2 (2.24.4 or newer)**, with Docker access for
  your user. Follow
  [Docker's Ubuntu installation guide](https://docs.docker.com/engine/install/ubuntu/).
  Rootless Docker is not qualified for managed runtimes.
- **Python 3.12, Git, Bash, iproute2, sha256sum and [uv](https://docs.astral.sh/uv/getting-started/installation/)**.
  The Ubuntu download example also uses `curl`; a browser download works too.
- Internet access for Python packages, container images and upstream Kodi downloads.
- Capacity for application images and each Kodi instance. RC1 requires at least
  **8 GiB free staging space**, plus space for Docker images and profiles. There
  is no qualified universal RAM minimum; begin with one running instance.

Setup installs Python build/test packages automatically; editors and AI clients
are optional.

Windows 11 with WSL2, Ubuntu 24.04 and Docker Desktop has an owner-accepted
installation and native Omega result using the maintained
[Windows installation path](docs/WINDOWS-WSL2-INSTALL.md). The
[password-prompt](https://github.com/kcook98765/kodi-lab/issues/222) and
[Firefox setup](https://github.com/kcook98765/kodi-lab/issues/226) checks are also
accepted and closed. The current Windows managed development workflow remains
under [separate qualification in #275](https://github.com/kcook98765/kodi-lab/issues/275).
macOS and ARM64/Raspberry Pi hosts are not qualified. An existing Kodi target can
be on another computer; these requirements apply to the Kodi Lab server.

## Quick Start — Install Kodi Lab

These steps install Kodi Lab for the first time. Kodi is acquired separately
when you use **Create Managed Kodi** after signing in.

Choose how to access the Web UI **before installing**:

| Where you will use Kodi Lab | Access choice |
| --- | --- |
| Only on the installation computer | **Default local HTTP:** `http://127.0.0.1:18100/login`. Simplest option; no certificate or proxy required. |
| From other devices on a trusted private network | **Optional Ubuntu LAN HTTP:** choose `--web-lan-ip` during a fresh installation, as shown below. No certificate required. |
| With encrypted browser access | **Optional HTTPS:** requires the [supported proxy configuration](docs/WEB-NETWORKING.md#https-with-a-user-managed-proxy) and certificate trust before login. |
| On Windows/WSL2 | Follow the separate [Windows networking instructions](docs/WINDOWS-WSL2-INSTALL.md); Ubuntu's LAN HTTP flag is unavailable there. |

With the prerequisites installed, download and run one installer script.
On **Ubuntu**, use Bash:

```sh
curl -fL https://raw.githubusercontent.com/kcook98765/kodi-lab/main/scripts/install.py -o install-kodi-lab.py
python3 install-kodi-lab.py bootstrap
```

For **LAN Web access on a fresh Ubuntu installation**, replace the second command with:

```sh
python3 install-kodi-lab.py bootstrap --web-lan-ip YOUR_PRIVATE_IPV4
```

Replace `YOUR_PRIVATE_IPV4` with a private IPv4 address assigned to the Ubuntu
server. After installation, open `http://YOUR_PRIVATE_IPV4:18100/login` from
another LAN device.
LAN HTTP sends passwords, sessions and screen/control traffic **unencrypted**.
Use it only on a trusted private network; never add router port forwarding or
expose it publicly. See [Web networking](docs/WEB-NETWORKING.md) for details.

On **Windows**, use PowerShell after enabling Ubuntu's Docker Desktop/WSL2 integration:

```powershell
Invoke-WebRequest -UseBasicParsing https://raw.githubusercontent.com/kcook98765/kodi-lab/main/scripts/install-windows.ps1 -OutFile install-kodi-lab.ps1
.\install-kodi-lab.ps1
```

The scripts obtain a clean source checkout in the Linux user's data directory
and default to `~/kodi-lab-install`. Windows discovers that user's Linux home
automatically. No manual checkout path is needed. The selected source commit is
recorded; an exact commit can be selected with `--revision` / `-Revision`.

The scripts ask for your private LAN repository address, build/verify the
application bundle and initialize fresh state. After you choose the Web
administrator password, they start services. Open
**http://127.0.0.1:18100/login** on this computer,
sign in with your chosen password and continue
with [First-Time Setup](#first-time-setup). See the
[installer guide](docs/HOST-INSTALLER.md) for prerequisites, configuration,
start/stop and recovery. Windows users follow the
[WSL2 guide](docs/WINDOWS-WSL2-INSTALL.md), including its explicit repository
forwarding setup.

The prompted **Kodi add-on repository address** is separate from Web access.
Local agents use `http://127.0.0.1:18100/mcp` with separate bearer-token
authentication; Ubuntu's LAN Web listener rejects MCP.

For an existing installation, follow the documented
[mode-preservation and upgrade procedure](docs/WEB-NETWORKING.md#restarts-and-upgrades).
Do not rerun `bootstrap` or add installation flags to a restart to change
its saved access mode.

### Manual installation

Operators who want to run each bundle step themselves can use the
[manual Linux installation guide](docs/LINUX-SOURCE-INSTALL.md#clone-and-stage-the-application).

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
In HTTPS mode, login and viewing require a trusted HTTPS/WSS proxy; see
[HTTPS configuration](docs/WEB-NETWORKING.md#https-with-a-user-managed-proxy).
The Kodi add-on repository has its own address and network configuration.

## First-Time Setup

To use Kodi hosted by Kodi Lab:

1. Sign in. From **Overview**, open **Managed Kodi** and choose Matrix, Nexus,
   Omega or Piers (preview).
2. If installation is needed, choose **Create Managed Kodi for this version…**,
   then **Install and prepare**. Setup leaves Kodi stopped by default. Return
   using **Return to Managed Kodi versions**.
3. Select **Start Kodi** for the installed version. An empty slot requires
   **Create Managed Kodi for this version…** first. Wait for **Running** and **View Screen** to
   become available, then choose **View Screen → Start Viewing**.
   Viewing starts view-only; **Take Control** is a separate explicit action.
4. Select **Stop Kodi** to preserve that version's settings and installed add-ons.

This manual **Install and prepare → Start Kodi → View Screen → Stop Kodi** workflow
is available now. Only one managed Kodi runs at a time: deliberately Stop one
version before starting another. Agents can compose the same explicit Stop and
Start operations; Kodi Lab does not evict a running version automatically.
Fresh Web installation also prepares the compatible reviewed Narrow Bridge.
Native readiness and stored Bridge trust remain separate from live inspection
and successful add-on execution. In **Agents**, choose **Managed Kodi agent** and
deliberately enable **Trusted Managed Lab** once. The agent can create/import
its projects and use installed versions without Web workspace selection.
External devices still require separate explicit permissions.

**Reset Kodi**, under the version's **Advanced options**, requires confirmation
and intentionally discards its writable Kodi profile, settings and installed
add-ons, preparing a clean stopped replacement. Independent sources, workspaces
and Git data have separate retention and survive this Reset. Save anything
important first; see [managed state recovery](docs/MANAGED-STATE-RECOVERY.md).
After replacement, use **Set up for add-on testing** to prepare Bridge and
deployment again. Installing a newer reviewed runtime recipe does not
automatically migrate existing profiles. See the
[managed Kodi Web walkthrough](docs/MANAGED-KODI-WEB.md) for recovery and advanced
administration, and the [agent lifecycle](docs/MANAGED-VERSION-LIFECYCLE.md) and
[clean Reset](docs/MANAGED-CLEAN-RESET.md) contracts for separately authorized agents.

To connect Kodi on another device, open **External Kodi**, then choose **External Production Kodi** or
**External Development Kodi** from Overview and follow its connection workflow.
See [external Kodi setup](docs/LINUX-V1-INSTALL.md#install-and-pair-a-remote-kodi-bridge),
[reconnection](docs/KODI-RECONNECT.md), and
[repository address recovery](docs/REPOSITORY-ADDRESS-RECOVERY.md).

## Develop an add-on

**Choose add-on project → Choose Kodi version → Develop and test.**

Install the desired managed versions in the Web UI. In **Agents**, choose
**Managed Kodi agent**, explicitly enable **Trusted Managed Lab**, and issue
the agent a credential. The agent creates a project or imports a bounded reviewed
source package, edits and validates its private workspace, then selects an
installed managed version for deployment, launch and inspection. It can Stop one
version and Start another, use supported GUI actions, or Reset an idle profile
with reviewed Bridge preparation. No individual source registration or Web
workspace selection is needed for this workflow.

See [agent-owned projects](docs/AGENT-PROJECTS.md),
[Trusted Managed Lab](docs/TRUSTED-MANAGED-LAB.md) and
[agent Reset](docs/MANAGED-AGENT-RESET.md) for supported inputs, exact operation
identity and recovery. Source implementation and fixture success are distinct
from the integrated native qualification tracked in
[#310](https://github.com/kcook98765/kodi-lab/issues/310).

The existing manually selected workflow remains under **Advanced → Development
tools → Current Managed Kodi**. Its [local development root](docs/LINUX-V1-INSTALL.md#optional-local-add-on-source-development),
[project onboarding](docs/LOCAL-PROJECT-ONBOARDING.md) and
[walkthrough](docs/DEVELOPMENT-SESSION.md#everyday-walkthrough) retain their
separate source, workspace and client permissions.

## Connect Your AI Agent

Open **Agents** (`/mcp-clients`) and use a separate client for each agent. Select
its purpose and deliberately grant the required authority:

- **Managed Kodi agent:** explicitly enable **Trusted Managed Lab** for autonomous
  development across installed managed versions. It grants no external access.

- **External Production Kodi:** choose **Production Inspection**, grant
  that Kodi, then **Grant current inspect scope**. This preset is production-only.
- **Limited managed inspection:** open **Advanced → Managed Kodi — Read-only
  inspection** in the connection guide. Select your existing client and exact Kodi; the next-step guidance opens
  the existing controls for an inspect-only custom preset, Kodi policy and target
  grant. Reuse a suitable preset or explicitly save one with only `target.inspect`.
  Supported screenshots use this inspection permission. Each change requires an
  explicit save; no client, grant or credential is created while browsing setup.

Issue a token and save it in the agent's private environment. A token does not
replace the role or independent external Kodi policy and grant. Keep a suitable client and
unexpired credential rather than creating another for each connection.

### MCP tools and permissions

Tools are listed according to the client's permissions; a listed tool still needs
permission for the exact Kodi, source or workspace on each call. The complete
reference below covers both modern and legacy MCP transports. **Yes** means
supported with the stated role and prerequisites; **Conditional** needs per-Kodi
policy and grants; **Off** needs explicit administrator enablement; **No** is a
technical prohibition; **N/A** means the tool does not act on a Kodi target.

<details><summary>MCP tools and permissions — complete reference</summary>

<!-- mcp-tool-matrix:start -->

**Read the columns with these authority rules:**

- **Client and credential:** create a client in **Agents**, choose its preset,
  and issue an unexpired credential. No connection or preset grants access to
  every Kodi. Disablement, revocation and permission changes are rechecked.
- **Managed:** **Yes** assumes the explicit **Trusted Managed Lab — Full
  Development** role, covering installed, approved disposable managed versions,
  including versions installed later. It does not install missing versions,
  bypass active-use/resource guards, or authorize external Kodi. Limited managed
  inspection instead needs `target.inspect` in the client and exact Kodi policy,
  plus a target grant. Other legacy/custom clients need the relevant permissions
  in both policies and exact grants; additional opt-ins are shown below.
- **External Production:** the normal **Production Inspection** starting point
  allows inspection only after the exact target grant and **Grant current inspect
  scope**, bound to the confirmed connection. Native identity, production
  designation, enabled/verified state and consent remain required. Add-on
  inventory is an inspection read; screenshots and navigation are independently
  Off. Production built-in presets have fixed permissions; custom presets cannot
  expand production eligibility. No development or managed lifecycle permission
  can override a **No**.
- **External Development:** connecting/pairing grants no agent authority. The
  simple connection flow starts with inspection and screenshot capability only;
  **Allow screenshots** remains Off. Choose a suitable client preset, assign the
  exact Kodi policy and grant that Kodi separately. **Advanced → Amend development
  capability maximum** can enable inventory, semantic remote control, deployment
  and fixed execution only with matching native/Bridge support. The label
  “Development” does not grant write access.
- **Sources and workspaces:** **N/A** tools use project ownership, workspace
  ownership and exact source grants, not an external Kodi grant. Owned-project
  intake establishes the owner's private source/workspace; manually registered
  sources need explicit grants in **Agents → Advanced**. Deployment later binds
  an exact target and adds its independent policy, grant and runtime checks.
- **Runtime and Web controls:** capabilities, reconciled state, readiness,
  reviewed runtime support and current native/Bridge identity can still deny an
  authorized call. **Allow screenshots** on each external Kodi defaults Off;
  HTTP pixels also need transport-risk consent. Managed capture has no such
  toggle. Production **Allow remote navigation** defaults Off; it is separate
  from External Development's semantic-remote capability maximum. Web toggles
  cannot manufacture support or bypass global/per-target production vetoes.

#### Inspect and interact with Kodi

| MCP tool | Purpose | Managed | External Production | External Development | Permission / Web override |
| --- | --- | --- | --- | --- | --- |
| `target_list` | List accessible Kodi targets | Yes | Conditional | Conditional | `target.inspect`; filters by current access, not all registered Kodi. |
| `target_status` | Read target health and identity | Yes | Conditional | Conditional | `target.inspect`; exact target access; status can describe unavailable Kodi. |
| `addon_list` | Read installed add-on inventory | Yes | Conditional | Off | `target.inspect`; production inspection includes this read; development needs verified `addon_inventory` maximum. |
| `inspect_ui_state` | Read supported GUI state | Yes | Conditional | Conditional | `target.inspect`; reviewed Bridge enriches observations; bounded native fallback for external Kodi. |
| `log_read` | Read bounded Kodi log output | Yes | Conditional | Conditional | `target.inspect`; supported managed collector or trusted external Bridge log capability; no caller-selected file. |
| `screenshot_capture` | Capture current GUI image | Yes | Off | Off | `target.inspect`; external **Allow screenshots**, verified capture capability, paired/reviewed Bridge and transport consent. No separate visual preset required. |
| `screenshot_retrieve` | Retrieve an exact recent capture | Yes | No | No | `target.inspect`; only the existing managed capture returned by an observed remote action; never recaptures. |
| `remote_action` | Send a closed semantic GUI action | Yes | Off | Off | Managed/development: `target.control`; development needs verified `semantic_remote` maximum. Production uses the action rules below. |

For production `remote_action`, **Allow remote navigation** permits only Up,
Down, Left, Right, Back, Home and Info with inspection access and a trusted
Bridge; no control preset or navigation scope is required. Playback/volume need
**Production Remote Kodi**, `request_transient_control` scope, exact one-shot
Web approval and native-control HTTP consent when applicable. Global emergency
blocking and target suspension override both paths. **Select and Context menu
are always prohibited on External Production Kodi.** A listed `remote_action`
does not imply those actions are authorized.

#### Managed lifecycle and target-bound development

| MCP tool | Purpose | Managed | External Production | External Development | Permission / Web override |
| --- | --- | --- | --- | --- | --- |
| `managed_status` | Summarize installed managed versions | N/A | N/A | N/A | `managed.versions`; managed-only inventory, no target argument. Role or explicit custom-preset opt-in. |
| `managed_start` | Start an installed version | Yes | No | No | `managed.versions`; role or explicit client/Kodi opt-in and target grant; missing installation needs a human. |
| `managed_stop` | Stop an installed version | Yes | No | No | `managed.versions`; same authority and active-use guards. |
| `managed_reset` | Reset an idle disposable profile | Yes | No | No | `managed.versions` + `managed.reset`; role or explicit opt-ins; durable Reset guards and reviewed Bridge preparation. |
| `managed_ensure_ready` | Acquire readiness for managed work | Yes | No | No | `managed.lifecycle`; role or explicit custom-preset opt-in; installed target and lifecycle guards. |
| `managed_release` | Release managed lifecycle ownership | Yes | No | No | `managed.lifecycle`; releases only eligible agent-owned lifecycle state. |
| `managed_selection_preview` | Preview workspace/version binding | Yes | No | No | `managed.lifecycle` + `workspace.inspect`; exact owned workspace; no implicit switch. |
| `managed_select` | Bind a workspace and managed version | Yes | No | No | `managed.lifecycle` + `workspace.inspect`; exact ownership and selection/lifecycle guards. |
| `workspace_deploy_preflight` | Explain exact deployment readiness | Yes | No | Conditional | `workspace.deploy`; workspace/source ownership plus target policies/grants; reports blockers without deploying. |
| `workspace_deploy` | Deploy an exact workspace generation | Yes | No | Off | `workspace.deploy`; ready target; development needs reviewed `workspace_deployment` maximum and Bridge. |
| `workspace_execute` | Run a fixed development workflow | Yes | No | Off | `development.execute`; exact deployment; development needs `fixed_execution` maximum and reviewed Bridge support. |
| `workspace_launch` | Launch workspace with durable receipt | Yes | No | No | `development.execute` + `target.control`; exact managed workspace/deployment and ownership. |
| `development_launch` | Launch the declared development entry | Yes | No | No | `development.launch` + `workspace.inspect`; role or explicit opt-in; owned project or exact selected session. |
| `workspace_execution_recover` | Recover uncertain managed execution | Yes | No | No | `development.execute` + `target.control`; exact retained operation and supported recovery guards. |
| `addon_settings_set` | Apply declared add-on settings | Yes | No | No | `development.settings.write` + `workspace.inspect`; role or explicit opt-in; owned project or selected managed session. |
| `addon_settings_result` | Read exact settings receipt | Yes | No | No | Same settings/workspace permissions; exact operation ownership. |
| `promotion_request` | Prepare evidence for human review | Conditional | No | Off | `development.request_promotion`; eligible deployed workspace, target execution/deployment support; agent-owned projects excluded. Never publishes. |
| `package_install` | Deliver a retained original ZIP | Conditional | No | No | Trusted managed role (includes `addon.install`), owned package and target checks; delivery/status do not prove native GUI consumption. |
| `package_install_status` | Read or verify a ZIP-delivery receipt | Conditional | No | No | `workspace.inspect` + Trusted Managed Lab; exact owned operation binds its managed target; optional verification inspects that Kodi. |

#### Target-independent projects, packages and workspaces

All three target columns are **N/A** here: these tools manipulate or inspect Lab
project state, not the filesystem or runtime of an external Kodi. Source grants
and ownership still apply; “N/A” is not unrestricted authority.

| MCP tool | Purpose | Managed | External Production | External Development | Permission / Web override |
| --- | --- | --- | --- | --- | --- |
| `project_create` | Create a private add-on project | N/A | N/A | N/A | `workspace.edit` + Trusted Managed Lab; owner-bound intake. |
| `project_transfer` | Transfer bounded source-package chunks | N/A | N/A | N/A | `workspace.edit` + Trusted Managed Lab; exact transfer identity and checksum. |
| `project_import` | Import reviewed source into a project | N/A | N/A | N/A | `workspace.edit` + Trusted Managed Lab; supported bounded package and provenance. |
| `project_status` | Inspect an owned project | N/A | N/A | N/A | `workspace.inspect` + Trusted Managed Lab; exact owner. |
| `package_import` | Retain an original installable ZIP | N/A | N/A | N/A | `workspace.edit` + Trusted Managed Lab; package intake checks; no installation. |
| `package_status` | Inspect a retained original ZIP | N/A | N/A | N/A | `workspace.inspect` + Trusted Managed Lab; owned package only. |
| `development_source_list` | List accessible source registrations | N/A | N/A | N/A | `workspace.inspect`; exact source grants; no host directory browsing. |
| `workspace_create` | Create a workspace from a source | N/A | N/A | N/A | `workspace.edit`; granted registered source; isolated agent ownership. |
| `workspace_import_preview` | Preview source import differences | N/A | N/A | N/A | `workspace.edit` + `development_source.inspect`; owned workspace and source grant. |
| `workspace_import` | Import a reviewed source generation | N/A | N/A | N/A | Same import permissions; exact preview/generation and ownership. |
| `workspace_source_scan` | Scan bounded registered source state | N/A | N/A | N/A | `development_source.inspect`; role or explicit opt-in; source grant and workspace ownership. |
| `workspace_capture` | Capture source into a workspace | N/A | N/A | N/A | `workspace.capture`; role or explicit opt-in; exact source/generation; no target capture. |
| `workspace_status` | Read workspace state and receipts | N/A | N/A | N/A | `workspace.inspect`; owned workspace and source grant. |
| `workspace_tree` | List bounded workspace files | N/A | N/A | N/A | `workspace.inspect`; present, ready workspace; no arbitrary host path. |
| `workspace_read` | Read a bounded workspace file | N/A | N/A | N/A | `workspace.inspect`; present, ready workspace and safe relative path. |
| `workspace_edit` | Edit an exact workspace generation | N/A | N/A | N/A | `workspace.edit`; present, ready workspace; generation and path checks. |
| `workspace_validate` | Run fixed structural validation | N/A | N/A | N/A | `workspace.inspect`; present, ready workspace; no arbitrary shell/test command. |

The [offline matrix check](tests/web/test_readme_mcp_matrix.py) compares these
rows with the registry and authenticated modern/legacy `tools/list` catalogs.
It detects missing, renamed or duplicate tools; it does not replace the deeper
[permission reference](docs/PHASE11-MCP.md),
[development contracts](docs/PHASE12-MCP-DEVELOPMENT.md),
[Trusted Managed Lab](docs/TRUSTED-MANAGED-LAB.md) or
[production safeguards](docs/PRODUCTION-OPERATIONS.md).
Tool results retain their typed success, denial, blocked, pending and uncertain
outcomes; receipt creation is not proof of execution or native GUI acceptance.

<!-- mcp-tool-matrix:end -->

</details>

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

Ask the agent to list its Kodi targets, inspect the chosen Kodi's health, then
list its add-ons. Native managed status and add-on inspection need no Bridge
setup. Screenshots use ordinary inspection permission; external Kodi also needs
Allow screenshots set to On and verified Bridge support. Richer UI/log inspection
can require optional Bridge setup. Each tool uses the selected target's ID.
Configuration syntax and compatibility vary by client.
See [OpenCode's MCP instructions](https://opencode.ai/docs/mcp-servers/),
the [client compatibility notes](docs/PHASE11-MCP.md#final-client-and-conformance-matrix)
and [setup for agents on another computer](docs/NATIVE-LAN-MCP.md).

## Supported Kodi Versions

| Managed runtime | Status |
| --- | --- |
| Kodi 19 Matrix **19.4** | Stable runtime |
| Kodi 20 Nexus **20.5** | Stable runtime |
| Kodi 21 Omega **21.3** | Stable runtime |
| Kodi 22 Piers **22.0 RC2** | **Preview / Prerelease — Development only** |

These are the reviewed Linux amd64 runtime options; Kodi executables are acquired
on demand, not included in Kodi Lab. Piers RC2 is prerelease development software.
Retained RC1 profiles keep their original version and provenance; selecting RC2
does not replace an occupied Piers slot. See the [RC2 qualification](docs/PIERS-RC2-QUALIFICATION.md).
Standard noVNC viewing is qualified only for
[exact reviewed recipe revisions](docs/STANDARD-NOVNC.md), not automatically for
older profiles. Kodi Lab remains a prerelease development/testing project.
Known prerelease concerns include the
[Nexus first-viewer disconnect](docs/STANDARD-NOVNC.md#known-first-connection-limitation)
and the Windows managed development qualification above. Earlier
[RC1 test checkpoints](docs/RC1-TESTING-CHECKPOINT.md) are historical records.
See the [agent-first qualification report](docs/AGENT-FIRST-QUALIFICATION.md)
for historical native Nexus/Omega evidence, and the
[Matrix/Piers native follow-up](docs/MATRIX-PIERS-AGENT-QUALIFICATION.md)
for the newer application checkpoint, both remaining native development paths,
and the retained viewer/ZIP qualification limits.

## Documentation

- **Getting started:** [Installer and everyday management](docs/HOST-INSTALLER.md),
  [Linux manual installation and capacity](docs/LINUX-SOURCE-INSTALL.md),
  [Windows/WSL2 setup](docs/WINDOWS-WSL2-INSTALL.md), and
  [Web access and optional HTTPS](docs/WEB-NETWORKING.md).
- **Kodi and add-on development:** [Current Managed Kodi walkthrough](docs/DEVELOPMENT-SESSION.md),
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
  [managed runtime updates](docs/MANAGED-RUNTIME-UPDATES.md),
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
