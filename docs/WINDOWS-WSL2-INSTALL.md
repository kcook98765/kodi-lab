# Windows host installation through WSL2 and Docker Desktop

Kodi Lab has a reported operational Windows-hosted Linux-container deployment.
The maintained installer below is **experimental**: fresh-account/clean-host
Windows acceptance has not yet been recorded. Native Ubuntu 24.04 amd64 remains
the qualified baseline. This guide does not claim native Windows server
execution or formal Windows qualification.

## Observed working configuration

[Issue #8](https://github.com/kcook98765/kodi-lab/issues/8) records this workstation
on **2026-10-17**. These are observed versions, not minimum requirements or proof
that this new installer was used:

| Component | Observed version |
| --- | --- |
| Windows | Windows 11 Home, build 26300, x86_64 |
| WSL2 | 3.0.3.0 |
| Distribution | Ubuntu 24.04.5 LTS |
| Docker Desktop | 4.94.0.241994, Linux containers |
| Docker Engine | 29.8.2, linux/amd64 |
| Kodi Lab | 1.0.0rc1-based local deployment |
| Managed Kodi | Nexus 20.5 and Omega 21.3 |

The report includes authenticated Windows browser access, broker/repository
services, managed lifecycle, JSON-RPC, screenshots, MCP and agent workspace
development. It used private Compose/launcher helpers. The project entry points
below replace that dependency for future installation attempts; their native
Windows acceptance remains pending.

## Prepare Windows and Ubuntu

Install WSL2 and Ubuntu 24.04 using
[Microsoft's WSL instructions](https://learn.microsoft.com/en-us/windows/wsl/install).
For a new host, run **PowerShell as administrator**:

```powershell
wsl --install --distribution Ubuntu-24.04
```

Restart if requested, open Ubuntu and create its ordinary Linux user. In
**PowerShell**, check `wsl --version` and `wsl --list --verbose`; the Ubuntu
distribution must show version **2**, not WSL1. Use the exact distribution name
from that output in later commands.

Install Docker Desktop for Windows, choose **Linux containers** and enable its
**WSL2 engine** and **Settings → Resources → WSL Integration** for this Ubuntu
distribution. Start Docker Desktop before installing Lab. Follow
[Docker's WSL setup](https://docs.docker.com/desktop/features/wsl/) and
[best practices](https://docs.docker.com/desktop/features/wsl/best-practices/).
Do not install a second Docker Engine in Ubuntu or expose an unauthenticated
TCP Docker socket. Only the broker receives the daemon socket.

Inside the **Ubuntu/WSL terminal**, install Git, Python 3.12, `iproute2`, Bash,
coreutils and [uv](https://docs.astral.sh/uv/getting-started/installation/), then
run the [shared prerequisite checks](HOST-INSTALLER.md#prerequisites).

```sh
sudo apt update
sudo apt install git python3.12 python3.12-venv iproute2 coreutils
```

Install uv as the ordinary Linux user using its linked upstream instructions.
Compose must be 2.24.4 or newer. Check capacity in both the Ubuntu filesystem
and Docker Desktop's disk allocation. WSL memory/process limits and Windows
free space also matter; start with one instance. Microsoft documents optional
[WSL resource settings](https://learn.microsoft.com/en-us/windows/wsl/wsl-config).
Do not overwrite an existing `.wslconfig` to apply an example allocation.

Keep checkout and installation in the Linux filesystem, such as
`/home/your-user/kodi-lab` and `/home/your-user/kodi-lab-install`. The installer
rejects `/mnt/c` and other `/mnt` destinations. State uses Linux Docker named
volumes; retain Linux ownership and private socket permissions. Do not replace
them with broad Windows directory shares or `chmod 777`.

`C:\Users\you\project` corresponds to `/mnt/c/Users/you/project`; inside WSL,
`wslpath -u 'C:\Users\you\project'` converts the spelling, but does not make an
NTFS directory a suitable Lab checkout/state root. For opted-in development
source mounts, follow the existing source-directory restrictions and use Linux
paths visible to the integrated daemon. Windows can browse Linux files via
`\\wsl.localhost\Ubuntu-24.04\home\your-user`; do not copy another workstation's
state, grants or credentials.

## Install

In **Ubuntu/WSL**, clone the repository into your Linux home and run:

```sh
git clone https://github.com/kcook98765/kodi-lab.git "$HOME/kodi-lab"
cd "$HOME/kodi-lab"
python3 scripts/install.py install --directory "$HOME/kodi-lab-install"
```

When prompted, enter the **Windows host's private LAN IPv4 address**, not WSL's
transient VM address or `127.0.0.1`. Choose the Web administrator password at the
existing password prompt. The installer detects WSL2 and requires Docker Desktop.

Alternatively, from **PowerShell**, run the maintained wrapper after cloning.
Replace `your-user` with the Linux user name; these parameters are Linux paths:

```powershell
& '\\wsl.localhost\Ubuntu-24.04\home\your-user\kodi-lab\scripts\install-windows.ps1' `
  -Distribution Ubuntu-24.04 `
  -CheckoutPath /home/your-user/kodi-lab `
  -InstallDirectory /home/your-user/kodi-lab-install `
  -RepositoryAddress 192.168.1.50
```

The wrapper passes arguments directly to `wsl.exe --exec`; no private launcher
or shell command-string translation is needed. Use the execution policy approved
for your computer; review the script rather than disabling policy globally.

Open <http://127.0.0.1:18100/login> in the Windows browser and sign in. The
installer checks login readiness and anonymous MCP denial, but authorized MCP
inspection still needs a client and target grant.

## Repository LAN access and external targets

Web/MCP stay on loopback. The WSL installer creates a small, recorded
`compose.windows.yaml` outside the verified bundle. It **replaces** the
repository's host publication with `127.0.0.1:18121`; it does not add a wildcard
listener. The repository advertises the chosen Windows LAN address on `18120`.
The Docker socket, route mount, peer identity checks and managed network rules
are unchanged. A plain Compose ports list would append a second binding;
this override uses the supported `!override` tag.

For Kodi devices to use that advertised address, configure Windows forwarding
deliberately. First check that the selected address belongs to this Windows host,
the front port is unused and no existing portproxy entry conflicts. In
**administrator PowerShell**, the following example binds only the chosen LAN IP
and permits the local subnet on the Private profile:

```powershell
$RepositoryAddress = '192.168.1.50' # Replace with this Windows host's reserved LAN IPv4.
Get-NetIPAddress -AddressFamily IPv4
Get-NetTCPConnection -State Listen
netsh interface portproxy show v4tov4
netsh interface portproxy add v4tov4 listenaddress=$RepositoryAddress listenport=18120 connectaddress=127.0.0.1 connectport=18121
New-NetFirewallRule -Name 'KodiLabRepository18120' -DisplayName 'Kodi Lab repository' -Direction Inbound -Action Allow -Protocol TCP -LocalAddress $RepositoryAddress -LocalPort 18120 -RemoteAddress LocalSubnet -Profile Private
```

Use the actual installer ports if changed. Configure the rule only for a trusted
home network; do not change an untrusted network's profile to make it work.
This forwarding is Windows administration, not an installer side effect. Test
`http://127.0.0.1:18121/health` on Windows and
`http://WINDOWS_LAN_IP:18120/health` from another LAN device before pairing Kodi.
See [Microsoft's WSL networking guide](https://learn.microsoft.com/en-us/windows/wsl/networking)
for NAT/mirrored mode and firewall differences. VPNs or enterprise policy may
require administrator-specific configuration; never forward Web/MCP as a workaround.

To remove only the example forwarding/rule, use administrator PowerShell:

```powershell
netsh interface portproxy delete v4tov4 listenaddress=$RepositoryAddress listenport=18120
Remove-NetFirewallRule -Name 'KodiLabRepository18120'
```

Prefer a DHCP reservation. If the Windows LAN address changes, review the
repository URL, installed Kodi repository URLs, forwarding and firewall together;
a changed URL does not move existing pairings or authorize a new target identity.
For outbound external Kodi access, use its reachable LAN address and normal
**Add Kodi** identity/Bridge checks. A Windows browser reaching Web does not prove
that a Linux container can reach that Kodi device. Report reachability failures
with sanitized versions/endpoints; do not relax trust checks or grants.

## First managed Kodi, agent and everyday use

Follow [first-run setup](HOST-INSTALLER.md#first-run-and-authorized-inspection):
install one Omega 21.3 runtime, create/provision/start an instance, check its
screen, then grant a Read-only Inspection agent that target and verify
`target_list` and `target_status`. A Windows agent can use
`http://127.0.0.1:18100/mcp` with its bearer credential, independently of repository
forwarding. Clients on another computer require the existing
[explicit LAN MCP configuration](NATIVE-LAN-MCP.md).

For start/stop/status, use the copied helper inside WSL as in
[everyday management](HOST-INSTALLER.md#start-stop-and-diagnose), or the PowerShell
wrapper with `-Action start`, `stop` or `status` and the same distribution and
installation directory. Docker Desktop must already be running. Stop managed
Kodi in Web before stopping application services when you need a quiescent lab.

Windows sign-in startup is optional. Enable Docker Desktop's ordinary sign-in
startup if desired. A Task Scheduler task running **as your ordinary user** can
then invoke `wsl.exe` with these arguments after Desktop is ready:

```text
--distribution Ubuntu-24.04 --exec python3 /home/your-user/kodi-lab-install/kodi-lab.py start --directory /home/your-user/kodi-lab-install
```

Configure a delay/retry for Docker readiness. Do not use SYSTEM, store the Web
password in a task, or rerun the installer at sign-in. No startup task is created
by the project installer.

## Troubleshooting and pending qualification

- **Docker missing/wrong engine:** start Desktop, select Linux containers, enable
  integration for the exact distribution, then check its local Unix endpoint.
  Rootless/remote daemon substitutions are rejected.
- **Bind/forwarding failure:** confirm Windows and WSL loopback reachability,
  port ownership, Private-profile firewall/portproxy state and current LAN IP.
  Keep Web/MCP on loopback; never use an all-interface repository bind as a shortcut.
- **Runtime network conflict:** inspect Windows LAN/VPN routes (`Get-NetRoute
  -AddressFamily IPv4`), Linux routes and Docker networks before installation.
  Linux's route view does not enumerate every Windows/VPN route. Recheck after
  network changes; temporary-network pool selection remains administrator-owned.
- **Volume/socket/route-mount failures:** retain the broker boundary. Do not grant
  Web/Kodi the Docker socket, add privileged containers, or replace the fixed
  route mount with a stale file. Collect bounded broker/worker errors.
- **Interrupted installation/update:** retain the receipt and files; use the
  [failure and recovery procedure](HOST-INSTALLER.md#failure-upgrade-and-recovery).
  Copying a live Docker Desktop virtual disk or deleting volumes is not a
  consistent application backup.

Before claiming Windows qualification, record a fresh-user/clean-host run with
exact Windows/WSL/Ubuntu/Desktop/Engine/Compose versions, source commit, bundle
and image identities. Verify initialization, Windows login, repository LAN
reachability, one cold managed runtime acquisition, lifecycle/screenshots,
authorized and denied MCP inspection, stop/start, backup/restore and optional
startup if enabled. The previously reported workstation result does not replace
this new-installer acceptance. Native Windows Kodi targets have their own
[Bridge portability evidence](WINDOWS-DIRECT-INSTALL.md).
