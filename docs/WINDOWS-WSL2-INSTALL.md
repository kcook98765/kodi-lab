# Windows host installation through WSL2 and Docker Desktop

Windows 11 through Ubuntu 24.04 WSL2 and Docker Desktop's Linux engine is a
qualified initial-release platform alongside Ubuntu 24.04 amd64. This guide
covers Windows-hosted Linux containers. Native Windows server execution,
macOS and ARM64 are not qualified.

## Qualified managed-v2 scope

Stage 10 qualified the normal PowerShell fresh bootstrap on Windows 11 with
Ubuntu 24.04 WSL2 and Docker Desktop. Omega 21.3 passed native install, update,
same-version replacement, direct deployment, snapshot and restart persistence,
plus live noVNC and human input ownership. Docker Desktop's Engine socket
is mounted in the restricted broker only. Matrix, Nexus and Piers were catalog
checks on Windows. Piers remains a development preview.

The fresh bootstrap uses empty application and managed volumes. Current
managed-v2 update acceptance on Windows is separate from fresh bootstrap.
The disposable public repository workflow is qualified on Linux. Migration
from the retired pre-alpha architecture is unsupported. [Stage 10 acceptance](https://github.com/kcook98765/kodi-lab/pull/406#issuecomment-7146880731)

## Earlier observed configurations (historical)

[Issue #8](https://github.com/kcook98765/kodi-lab/issues/8) records this workstation
on **2026-10-17**, followed by owner-accepted maintained-installer operation
on **2026-11-04**. The table records the earlier observed versions, not minimum
requirements or a current qualification matrix:

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
below remove that dependency. The November 4 result used the maintained
PowerShell frontend at `0ac2b1d3b9c43dd2cb457a6d6d0c0b9b9293abcc`: installation,
login, repository delivery, native Omega acquisition and view-only graphics
were accepted by the owner. That result used the retired prerelease schema,
not the current initial-release baseline. Managed Stop/Start, authorized/denied
MCP, backup/restore and a separate clean account/host were not tested in that run.
[Issue #8 is closed by owner acceptance](https://github.com/kcook98765/kodi-lab/issues/8#issuecomment-6647085960);
subsequent native acceptance on **2026-11-05**, at
`3f969cf6911475b8be729a0109817b8ef8070578` with `0001_initial_release`, closed
[password-prompt acceptance #222](https://github.com/kcook98765/kodi-lab/issues/222)
and [Firefox Create continuation #226](https://github.com/kcook98765/kodi-lab/issues/226).
Those results cover hidden-input retry in the same install run, Windows Firefox
login, and real Omega acquisition/readiness with foreground and tab-return
continuation. Those earlier results alone did not qualify the newer workflow;
the November 13 acceptance above supersedes that pending status. Linux fixtures
do not establish native Windows results.

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

For the managed-v2 development candidate, download the PowerShell installer
from the reviewed candidate commit and pass that same full commit with
`-Revision`. The unqualified command below selects `main`, which still contains
the retired architecture until the coordinated merge. Existing historical
databases require a fresh installation with empty application and managed volumes.

Download [the PowerShell installer](https://raw.githubusercontent.com/kcook98765/kodi-lab/main/scripts/install-windows.ps1)
to Windows, then run it as your ordinary Windows user:

```powershell
.\install-windows.ps1
```

It discovers your selected Ubuntu user's Linux home, obtains the shared installer
and source at one exact Git commit, and defaults to `~/kodi-lab-install` inside
Ubuntu. It prompts for the repository address and Web password. No manual clone,
UNC path or Linux user-name substitution is required. Use `-Distribution Ubuntu`
if your distribution is named `Ubuntu`, or provide the actual name from
`wsl --list --verbose`. `-InstallDirectory` remains available for a custom Linux
destination; `-Revision` selects a full lowercase Git commit ID. A failed source
download or mismatched revision never proceeds to initialization.

For a new installation with working LAN DNS, use `-RepositoryAddress` for the
Windows private interface and `-RepositoryBaseUrl http://kodi-lab.home.arpa:18120`
for the durable advertised origin. Replace that example with your configured
hostname and check its resolution from Kodi before installing the repository.

You can also download [the Python installer](https://raw.githubusercontent.com/kcook98765/kodi-lab/main/scripts/install.py)
inside Ubuntu and run `python3 install.py bootstrap`. Both entry points use the
same installer and preserve the ordinary manual methods below.

### From an existing source checkout

In **Ubuntu/WSL**, clone the repository into your Linux home and run:

```sh
git clone https://github.com/kcook98765/kodi-lab.git "$HOME/kodi-lab"
cd "$HOME/kodi-lab"
python3 scripts/install.py install --directory "$HOME/kodi-lab-install"
```

When prompted, enter the **Windows host's private LAN IPv4 address**, not WSL's
transient VM address or `127.0.0.1`. Choose the Web administrator password at the
existing password prompt. The installer detects WSL2 and requires Docker Desktop.

From **PowerShell**, the wrapper can also use an explicit existing checkout.
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

For a fresh default installation, open **http://127.0.0.1:18100/login** in
Windows and sign in. Local HTTP and standard noVNC/WS need no certificate or
proxy. HTTPS mode requires trusted HTTPS/WSS and Secure cookies. Native
Windows local HTTP operation is covered by the accepted workflow above;
untested revisions and access modes require separate validation. The optional Caddy path
below selects HTTPS and keeps direct HTTP login disabled, including matching
HTTP Origin requests. Readiness checks do not replace authenticated browser
acceptance. MCP inspection still requires a client and target grant.

## Optional local HTTPS/WSS ingress

Select this option when you want encrypted browser sessions. In HTTPS mode,
administrator login and the screen viewer require trusted HTTPS/WSS.
MCP clients may continue using the existing loopback HTTP endpoint with Bearer
credentials. This optional path is **source-level verification only** until the
native Windows checks below pass. It does not establish Windows acceptance.

For a **fresh installation**, first inspect `Get-NetRoute -AddressFamily IPv4`
in Windows and `ip -4 route show table all` plus `docker network inspect` in
WSL, including active VPN routes and any reserved temporary-network pool. Choose an unused canonical private IPv4
**/29** and an unused unprivileged HTTPS port. The subnet below is an example,
not a reserved allocation. In ordinary PowerShell:

```powershell
.\install-windows.ps1 -WebHttpsProxy caddy `
  -HttpsIngressSubnet 192.168.241.0/29 -HttpsPort 18443
```

The wrapper passes current Windows routes as literal arguments. The Python
installer checks Linux routes, all Docker networks and `10.250.0.0/24` too.
Every subsequent activation refreshes Windows routes through `powershell.exe`;
keep WSL interoperability and that executable on WSL's PATH. An unavailable
route query or a conflict stops activation. Connect the VPN before checking;
repeat these checks when Windows/VPN routing changes. For direct Python
installation, additionally supply each specific Windows route with repeated
`--windows-route CIDR` arguments; omit only `0.0.0.0/0`.

The path is `Firefox → https://127.0.0.1:18443 → Caddy → Web:8100`.
Use **127.0.0.1**, including the selected HTTPS port, consistently; `localhost`
is a different origin and is not added to the bundle's allowed hosts. Caddy's
certificate contains the `127.0.0.1` IP SAN; `default_sni` selects it
when an IP client omits SNI. There is no LAN HTTPS/Web/MCP
publication or HTTP redirect listener. Leave repository LAN forwarding as
separately configured below. Check Windows listeners/portproxy rules before
activation and verify from a second LAN device that Web/MCP/HTTPS are unreachable.

For the example subnet, the dedicated bridge gateway is `192.168.241.1`, Caddy
is `192.168.241.2`, and Web's ingress address is `192.168.241.3`. Uvicorn receives
**only** `FORWARDED_ALLOW_IPS=192.168.241.2`; it trusts no gateway, subnet, wildcard,
hostname or additional peer. The existing backend on `127.0.0.1:18100` remains
HTTP and ignores forged forwarding headers from other peers. Caddy preserves
Host/Origin and uses its normal sanitized forwarding headers and WebSocket
support; no authentication, CSRF, cookie or noVNC rule is changed.

Only Web and Caddy join this bridge. It is a regular Docker bridge:
`internal: true` prevented published ports in the isolated Linux fixture.
Caddy therefore has ordinary bridge egress; this is not an egress firewall.
Cross-bridge separation and the reserved peer address provide a network trust
boundary, not cryptographic container identity. Ordinary containers on other
bridges cannot claim the occupied endpoint through normal Docker allocation.
Docker administrators can replace/attach containers or alter networking and
remain trusted. The helper refuses unexpected ingress membership/address drift
before activation. Caddy runs as UID/GID 10003, drops all capabilities, has no
Docker/broker socket, and mounts only its Caddyfile and separate CA/config
volumes. A private 128 MiB tmpfs holds a copy of the pinned official binary to
remove its low-port file capability without granting a capability to Caddy.

### Explicit certificate trust

No installer command imports certificates into Windows or Firefox. Caddy uses
its [internal CA](https://caddyserver.com/docs/automatic-https#local-https).
In WSL, select the actual installation or successor site directory:

```sh
SITE="$HOME/kodi-lab-install"
CID=$(python3 "$SITE/kodi-lab.py" compose --directory "$SITE" -- ps -q caddy)
test -n "$CID"
docker cp "$CID:/data/caddy/pki/authorities/local/root.crt" "$HOME/kodi-lab-caddy-root.crt"
openssl x509 -in "$HOME/kodi-lab-caddy-root.crt" -noout -subject -fingerprint -sha256
```

Export **only `root.crt`**. Never copy the CA directory or `root.key` to Windows,
a browser, a repository or a support report. Named volumes
`PROJECT_caddy_data` and `PROJECT_caddy_config` retain CA and configuration
state across stop/start and successor upgrades using the same project. Keep any
CA backup private; removing the data volume creates a different CA and breaks
existing trust. It is separate from the application database backup.

Copy the public root certificate to Windows using your WSL home share. Inspect
its identity/fingerprint against the WSL output before trusting it. For trust
limited to a Firefox profile, open **Settings → Privacy & Security → Certificates
→ View Certificates → Authorities → Import**, select this root, and explicitly
allow it to identify websites. Alternatively, after reviewing the certificate,
ordinary PowerShell can import it into your Windows user's root store:

```powershell
certutil -user -addstore Root "$HOME\Downloads\kodi-lab-caddy-root.crt"
```

Use the real destination file. Firefox can use Windows roots when **Allow
Firefox to automatically trust third-party root certificates you install** is
enabled; verify the actual profile/policy and restart Firefox as needed. See
[Mozilla's CA guidance](https://support.mozilla.org/en-US/kb/setting-certificate-authorities-firefox).
Open `https://127.0.0.1:18443/login` with no certificate exception. Inspect the
certificate chain, SHA-256 root fingerprint and IP SAN in Firefox, then sign in.
Never use `curl -k`, disabled certificate validation or a browser exception as
acceptance evidence. Trust only the deliberate local CA, and remove it later
only by its reviewed certificate identity.

### Check and restart

```sh
python3 "$SITE/kodi-lab.py" compose --directory "$SITE" -- config
python3 "$SITE/kodi-lab.py" compose --directory "$SITE" -- exec -T web printenv FORWARDED_ALLOW_IPS
python3 "$SITE/kodi-lab.py" compose --directory "$SITE" -- logs --tail 50 caddy
python3 "$SITE/kodi-lab.py" compose --directory "$SITE" -- stop caddy
python3 "$SITE/kodi-lab.py" compose --directory "$SITE" -- up -d --wait --no-build caddy
```

Inspect `PROJECT-https-ingress` with `docker network inspect`: exactly Web and
Caddy, the recorded /29, gateway +1, Caddy +2 and Web +3. Inspect Caddy's network
attachments too: only ingress. The helper verifies canonical site files, bundle,
engine and project before management commands, and network/route identity before
activation. Do not edit the overlay, Caddyfile, bundle or receipt to bypass a
failure. Default helper `stop`/`start` includes Caddy when configured. Existing
sign-in startup can use that helper after native qualification.

Verify Secure, HttpOnly, SameSite=Strict session cookies after login. Same-origin
mutations still require the session CSRF token. In Firefox Network tools, an
otherwise authorized viewer connection must upgrade with **101** to
`wss://127.0.0.1:18443/...`; incorrect/missing Origin and invalid/replayed viewer
tickets must remain denied. Proxy qualification can use isolated viewer doubles;
live noVNC/display acceptance needs its own authorized managed runtime. This task
does not require starting a Kodi instance. Anonymous protocol-valid MCP POSTs
without a browser Origin must still receive `401 Bearer`; browser Origin policy
may reject a request earlier. Keep existing MCP credentials and target grants.

### Recover a current-schema site

Keep the original verified bundle, installation receipt and private backup.
Use the [managed-v2 recovery procedure](MANAGED-V2-FRESH-INSTALL.md#state-and-recovery)
for a current-schema backup. Use fresh volumes for the replacement.

## Repository LAN access and external targets

Web/MCP stay on loopback. The WSL installer creates a small, recorded
`compose.windows.yaml` outside the verified bundle. It **replaces** the
repository's host publication with `127.0.0.1:18121`; it does not add a wildcard
listener. The repository advertises the chosen Windows LAN address on `18120`.
Only the restricted broker receives Docker Desktop's VM socket,
`/var/run/docker.sock.raw`, mounted at its existing `/var/run/docker.sock` path.
This preserves Engine-owned Linux volume bind paths when the broker creates
managed containers; the host socket otherwise translates them into Ubuntu
distribution paths. The route mount, peer identity checks and managed network
rules are unchanged. See [Docker's VM socket guidance](https://docs.docker.com/extensions/extensions-sdk/guides/use-docker-socket-from-backend/).
A plain Compose ports list would append a second binding;
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
See [repository address diagnosis and recovery](REPOSITORY-ADDRESS-RECOVERY.md):
moving a Windows listener does not rewrite the repository's embedded URLs, and
ordinary restart/candidate transition does not migrate an advertised origin.
For outbound external Kodi access, use its reachable LAN address and normal
**External Production Kodi** or **External Development Kodi** connection and
identity/Bridge checks. A Windows browser reaching Web does not prove
that a Linux container can reach that Kodi device. Report reachability failures
with sanitized versions/endpoints; do not relax trust checks or grants.

## First managed Kodi, agent and everyday use

From **Overview** (`/kodi`), open **Managed Kodi** (`/managed-kodi`) for Kodi hosted
by the Lab. Follow [first-run setup](HOST-INSTALLER.md#first-run-and-authorized-inspection):
open **Advanced → Reviewed runtime installations**, select Omega 21.3, then
review and approve **Install**. Once Ready, open **Managed Kodi**, select Omega,
then **Prepare** and **Start**. Preparation leaves Kodi stopped. Select **View**
for live noVNC; it starts view-only. **Take Control** enables human input and
**Release Control** returns to viewing. No workspace or Bridge pairing is
needed. **Stop** retains that version's profile; **Reset** deliberately discards its settings and
installed add-ons. See [retention and recovery](MANAGED-V2-FRESH-INSTALL.md).

For add-on work, issue the agent a Trusted Managed Lab credential in **Agents**
and follow the [current MCP workflow](MCP-TOOLS-AND-PERMISSIONS.md). Local Windows
and WSL clients can use `http://127.0.0.1:18100/mcp` with the bearer credential.
Use the [explicit LAN MCP configuration](NATIVE-LAN-MCP.md) for another computer.
External Production and External Development Kodi use their own registrations
and target grants.

The maintained installer checks its loopback repository listener before install
initialization and `start`. For a manual bundle, run `python3 linux-v1-bundle
preflight . --repository-bind 127.0.0.1` inside WSL before startup. This verifies
local bind availability; check Windows LAN forwarding and the advertised feed
from Kodi separately. See [address recovery](REPOSITORY-ADDRESS-RECOVERY.md).

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

## Troubleshooting and future qualification

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
  [failure and recovery procedure](HOST-INSTALLER.md#failure-and-recovery).
  Copying a live Docker Desktop virtual disk or deleting volumes is not a
  consistent application backup.

Keep the accepted #275 evidence for unchanged workflows. For a changed path,
record exact Windows/WSL/Ubuntu/Desktop/Engine/Compose versions, source commit,
bundle/image identities and the affected checks. A changed successor-upgrade
path needs focused Windows backup/recovery and image-availability verification;
it does not require repeating Omega acquisition or the accepted development campaign.
Optional HTTPS and sign-in startup require their own acceptance when selected.
Native Windows Kodi targets have separate [Bridge portability evidence](WINDOWS-DIRECT-INSTALL.md).

Use the [acceptance receipt](qualification/windows-wsl2/ACCEPTANCE-TEMPLATE.md)
to record new results consistently. Blank template fields are NOT RUN; they do
not invalidate completed acceptance.
