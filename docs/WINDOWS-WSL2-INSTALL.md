# Windows host installation through WSL2 and Docker Desktop

Kodi Lab has owner-accepted Windows 11 installation results through Ubuntu 24.04
under WSL2 and Docker Desktop's Linux engine. The installer below uses that
configuration. Native Ubuntu 24.04 amd64 remains the qualified baseline; the
current managed development workflow is tracked separately in
[#275](https://github.com/kcook98765/kodi-lab/issues/275). New revisions and
untested workflows need their own acceptance checks. This guide covers
Windows-hosted Linux containers, not native Windows server execution.

## Observed working configuration

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
continuation. They do not qualify the newer managed development workflow in
#275 or broader backup/restore and clean-account scenarios. Linux fixtures do
not establish native Windows results.

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
Windows local HTTP operation is covered by the owner-accepted result above;
current-revision qualification remains separate. The optional Caddy path
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

### Adopt an existing installation; disable or roll back

An initialized installation must use a **successor bundle**, not another fresh
install. Preserve its original `INSTALLATION.json`, bundle, private configuration
and all evidence. Preserve its recorded Compose project, volumes, credentials,
repository origin and runtime configuration. This procedure applies only to
compatible baseline state; it cannot adopt a retired prerelease database. See
the [schema applicability restriction](LINUX-V1-UPGRADE-ROLLBACK.md#applicability-before-the-first-public-release).

Before building or loading a successor, run the approved successor source's
image-retention preflight against the current installation while its services
are still running:

```sh
python3 /path/to/approved-source/scripts/install.py retain-images \
  --directory "$HOME/kodi-lab-install"
```

Keep the original verified bundle/archive and private receipts. Check physical
Windows storage and Docker Desktop disk headroom separately from WSL free space.
Follow [exact-image retention and recovery](LINUX-V1-UPGRADE-ROLLBACK.md#keep-exact-application-images-before-successor-staging)
if a pinned old image is missing; never pull a bare `sha256:` ID or substitute a
new image for the old administrator. This preflight adds local retention
references and changes no application state or service.

Stage and verify the approved successor in a new private directory containing
only `bundle/`. Load its reviewed images and
configure its `.env` using the supported bundle tool, carrying **all** old site
and runtime settings; only application image identities and BUNDLE identity
change. The bundle's files remain verified and untouched by ingress generation.
Use the approved successor source's installer to prepare a separate site record:

```sh
python3 /path/to/approved-source/scripts/install.py configure-successor \
  --directory "$HOME/kodi-lab-successor" \
  --previous-installation "$HOME/kodi-lab-install" \
  --web-https-proxy caddy --https-ingress-subnet 192.168.241.0/29 \
  --https-port 18443 --windows-route WINDOWS_SPECIFIC_ROUTE_CIDR
```

Repeat `--windows-route` for all current specific Windows IPv4 routes. This
command validates both bundles/configurations and generates `SITE.json`, a
management helper, the canonical Windows overlay and Caddyfile **outside** the
bundle. It pins the successor bundle/source, original receipt hash, unchanged
engine/project, proxy image, canonical overlay/Caddyfile hashes, CA/config
volume names and exact topology. Volume names derive from that
same recorded project. It performs no initialization, credential reset,
repository transition, database upgrade or service activation. Failure preserves
partial site files for diagnosis; do not overwrite them blindly.

Before any site files are written, `configure-successor` repeats the old bundle,
image availability and retention checks. Repeat `retain-images` against the old
installation immediately before stopping services. These checks do not replace
the required old-administrator backup and isolated recovery proof. The changed
preflight requires focused Windows/WSL2 verification; earlier Windows native
acceptance does not qualify this new upgrade path.

Follow [the supported upgrade sequence](LINUX-V1-UPGRADE-ROLLBACK.md#windows-successor-site-configuration):
stop the old application services/proxy, obtain a fresh supported backup and
isolated recovery proof, perform the reviewed repository-candidate transition
and explicit administrator `upgrade`, verify receipts, then start through the
successor helper. Use that helper's `compose -- ...` for successor service
commands so the old project and all site overlays stay explicit. One-shot Web
administrator jobs require Web to be stopped because its ingress IP is fixed.
Retire the old helper/sign-in task from active use without deleting evidence;
repoint any approved task only after successful qualification.

For proxy-only disable/rollback, stop Caddy and Web through the active helper.
Prepare another separate site directory with a verified/configured copy of the
**same successor bundle**, run `configure-successor` against the active site
**with `--remove-https-proxy --web-access-mode https`**, then start through the
resulting helper. Omitted HTTPS options now preserve the active Caddy topology.
This removes
Web's forwarded trust/ingress attachment while retaining the same application
state and project. No repository transition is needed when BUNDLE identity is
unchanged. Preserve stopped Caddy, its volumes and old site evidence. HTTP MCP
continues; direct HTTP browser login remains denied. Re-enabling uses the same
reviewed subnet/project and retained CA. Application-bundle rollback follows
the reverse-transition/restore guide; never start an old bundle blindly.

Native Windows acceptance still must prove Desktop publication on loopback,
non-conflicting Windows/WSL/VPN/Docker networks, actual direct versus proxy Web
peers, exact trust, TLS SAN/chain and Firefox trust, login/logout/cookies,
Origin/CSRF negatives, authorized WSS/noVNC and anonymous MCP denial, repository
LAN restrictions, state/credential preservation, stop/start, Desktop restart,
network recreation with the same addresses/CA, and rollback. Linux source tests
and the isolated Caddy fixture do not establish any of those native results.

Historical prerelease helper qualification is retained in the
[Windows update triage record](WINDOWS-UPDATE-TRIAGE.md#historical-successor-helper-correction).
It is not required for a fresh installation.

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
choose Omega's **Create Managed Kodi for this version…**, select **Install and
prepare**, return to the version page and select **Start Kodi**. Setup leaves
Kodi stopped by default. When **Running** and **View Screen** are available,
choose **View Screen → Start Viewing**. Viewing starts view-only and needs no
agent, workspace or Bridge pairing. **Stop Kodi** retains that version's profile;
**Reset Kodi** under **Advanced options** deliberately discards its settings and
installed add-ons. See [retention and recovery](MANAGED-KODI-WEB.md#stop-and-reset).

For add-on work, [Development Tools → Current Managed Kodi](DEVELOPMENT-SESSION.md)
adds a selected development environment and persistent workspace. Qualification
of that current Windows/WSL2 workflow remains open in #275. To connect Kodi on
another device, use **External Production Kodi** or **External Development Kodi**
from Overview. The Lab host platform and external device platform are separate.

Agent access is optional and separately configured in **Agents & Permissions**
(`/mcp-clients`). Follow the
[read-only agent setup](../README.md#connect-your-ai-agent), grant only the exact
target, then verify `target_list` and `target_status`. A Windows agent can use
`http://127.0.0.1:18100/mcp` with its bearer credential, independently of repository
forwarding. Clients on another computer require the existing
[explicit LAN MCP configuration](NATIVE-LAN-MCP.md).

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
startup if enabled. The owner-accepted installer result above covers only its
recorded revision and tested steps; it does not replace this broader
qualification. Native Windows Kodi targets have their own
[Bridge portability evidence](WINDOWS-DIRECT-INSTALL.md).

Use the [fresh-install acceptance receipt](qualification/windows-wsl2/ACCEPTANCE-TEMPLATE.md)
to record those results consistently. Its blank fields are NOT RUN and do not
change the current qualification status.
