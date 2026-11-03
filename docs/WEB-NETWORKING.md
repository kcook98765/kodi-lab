# Web access and optional secure networking

Fresh installations work immediately at **http://127.0.0.1:18100/login** on the
installation computer. Use the administrator password you chose during setup.
No certificates or proxy are needed for local login, standard noVNC viewing or
human control. Kodi acquisition and Bridge pairing remain separate operations.

## Choose an access mode

| Mode | Web publication | Browser transport | MCP access |
| --- | --- | --- | --- |
| Fresh default: `local-http` | `127.0.0.1:18100` | HTTP and WS | Existing bearer authentication |
| Optional Ubuntu `lan-http` | Selected private IPv4 plus the existing loopback port | Unencrypted HTTP and WS | Local MCP retained; LAN listener rejects MCP |
| Optional `https` | Loopback upstream behind your proxy | HTTPS and WSS | Existing bearer authentication; remote exposure is a separate decision |

`127.0.0.1` always means the computer running the browser. On another computer,
it does not point at the Kodi Lab server. The add-on repository's separately
selected LAN address/port is not the Web login address.

Authentication, session expiration, HttpOnly and SameSite=Strict cookies, CSRF,
same-origin and Trusted Host validation, login throttling, one-use viewer tickets
and exclusive human control remain active in every mode. HTTPS mode additionally
requires Secure cookies, HTTPS for Web writes and WSS for viewing. HTTP modes
permit non-Secure cookies so the browser can send the session over HTTP.

HTTP does **not** protect passwords, session cookies, screenshots or input from
network observers or modification. Use LAN HTTP only on a trusted private
network. Choose HTTPS when confidentiality or protection from network attackers
is required. Neither a private address nor a valid password encrypts traffic.
An HTTP SSH tunnel also does not turn the browser origin into HTTPS.

## Private-LAN HTTP on Ubuntu

For a **new installation**, select this server's stable RFC1918 interface address:

```sh
python3 install.py bootstrap --web-lan-ip 192.168.1.50
```

Replace the example with an address actually assigned to the Ubuntu server.
The installer checks that it can bind that address, warns that traffic is
unencrypted and asks you to type the exact IP to confirm. Declining stops before
creating installation state. Open the displayed URL from another LAN computer,
for example `http://192.168.1.50:18100/login`.

The LAN publication binds only that IP, never `0.0.0.0`, an entire subnet or
every interface. The original loopback Web/MCP publication remains available.
Public, loopback, link-local and noncanonical addresses are rejected as LAN
selections.
Choose a DHCP reservation or stable address. Do not add router port forwarding
or expose this HTTP port to the Internet. The installer does not change firewall
rules. Do not change production ports or rules merely to try this option.

LAN HTTP keeps **http://127.0.0.1:18100/mcp** fully operational for local agents,
with the same bearer authentication, permissions and target grants. Web login
does not authorize MCP, and a valid token does not enable MCP on the LAN socket.

The existing Web process opens two sockets with one application and lifespan.
Docker keeps `127.0.0.1:18100 → container:8100` for local Web/MCP and adds
`SELECTED_PRIVATE_IP:18100 → container:8101` for LAN browsers. The application
admits MCP only on the normal socket, using Uvicorn's kernel-derived destination
port. Missing transport metadata fails closed. Host, Origin and forwarding
headers cannot select the local socket. No extra service, proxy or database is
introduced. Remote MCP remains a separately configured capability; the LAN Web
socket rejects it even with valid credentials.

For manual **fresh bundle** configuration, the equivalent selection is:

```sh
python3 linux-v1-bundle configure . \
  --repository-bind 192.168.1.50 --repository-port 18120 \
  --repository-base-url http://192.168.1.50:18120 \
  --web-access-mode lan-http --web-published-bind 192.168.1.50 \
  --confirm-lan-http
```

The acknowledgement flag explicitly accepts the same unencrypted-traffic and
Web security warning. This does not initialize or activate the bundle. Include
`-f compose.lan-http.yaml` **after** `-f compose.release.yaml` in every manual
LAN start/recreate command (alongside any existing lab/development overlays).
The installer includes this verified overlay automatically. For example, the
managed-service startup command is:

```sh
docker compose -f compose.release.yaml -f compose.lab.yaml \
  -f compose.lan-http.yaml --profile lab up -d --wait --no-build web broker repository
```

The overlay appends the exact LAN binding and retains the loopback mapping.
Omitting it leaves local MCP available but does not publish LAN Web. Follow the
installation guide for the remaining steps. The simple LAN option is Ubuntu
only; Windows/WSL2 retains its existing explicit networking/Caddy path.

## HTTPS with a user-managed proxy

On a **fresh installation**, select `--web-access-mode https`. For manual bundle
configuration, add the same option to `linux-v1-bundle configure`. Keep the Web
upstream published only on `127.0.0.1:18100`. Direct HTTP login is then denied;
finish proxy configuration and certificate trust before signing in.

Manage your Caddy or Nginx service and certificates outside Kodi Lab. There is
no built-in certificate manager. The existing installer-owned
[Windows Caddy path](WINDOWS-WSL2-INSTALL.md#optional-local-httpswss-ingress)
remains available with its pinned image, exact peer trust, dedicated network,
loopback HTTPS publication, persistent CA volumes and permission checks. Do not
replace its canonical files or broaden its trusted peer using these examples.

### Trusted upstream peer and hostname

For a user-managed proxy, add an owner-managed Compose overlay to the manual
bundle workflow. For example, `compose.proxy.yaml`:

```yaml
services:
  web:
    environment:
      KODI_LAB_WEB_ACCESS_MODE: https
      KODI_LAB_WEB_ALLOWED_HOSTS: '["127.0.0.1", "lab.home.arpa"]'
      FORWARDED_ALLOW_IPS: "MEASURED_PROXY_PEER_IPV4"
```

Replace the hostname and peer before use. Configure DNS or client host entries
for the selected hostname. The peer must be the proxy's actual address **as seen
by Uvicorn**, not the browser IP or a guessed Docker subnet. A host proxy can
appear through Docker's gateway; a container proxy has its own peer address.
Confirm it in an isolated setup, reserve its identity and recheck after network
recreation. Trust only the exact required peer, never `*`, a whole subnet or an
arbitrary client-supplied header. Docker/host administrators remain trusted at
this network boundary.

Base Compose trusts no forwarding peers. Uvicorn accepts forwarded scheme/client
metadata only from the explicitly trusted peer; the application does not infer
HTTPS from raw `Forwarded` or `X-Forwarded-*` headers. The proxy must overwrite
client forwarding metadata and preserve the browser's exact Host (including
nondefault port) and Origin. Do not rewrite Origin to bypass CSRF or same-origin
checks. A wrong Host, scheme or port should fail.

Include this overlay in **every** manual start/recreate command, after the normal
release/lab overlays, using the same project, `.env`, volumes and bundle:

```sh
docker compose -f compose.release.yaml -f compose.lab.yaml \
  -f compose.proxy.yaml --profile lab up -d --wait --no-build web broker repository
```

Installer-generated helpers pin their own file list and do not load arbitrary
proxy overlays. Use the manual bundle workflow for this customization and retain
its explicit project name if adapting an installer-created site. Do not mix a
manual default project with an existing installer project. These examples are
configuration templates, not deployment or upgrade instructions for Claw.

### Caddy example

For Caddy running on the same Ubuntu host, a private-CA example is:

```caddyfile
https://lab.home.arpa {
    bind 192.168.1.50
    tls internal
    @mcp path /mcp /mcp/*
    respond @mcp 403
    reverse_proxy 127.0.0.1:18100
}
```

Replace the bind and name. Caddy handles WebSocket upgrades and sanitized
forwarding headers; preserve that behavior. See the official
[reverse proxy reference](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy).
This example blocks remote MCP deliberately. It adds no automatic CA trust to
client computers; distribute only the public root certificate through a trusted
channel and verify its fingerprint. Keep CA private keys on the proxy host.
For an owned public domain, you may instead use your existing certificate/ACME
workflow and the appropriate Caddy configuration.

### Nginx example

In the `http` context, define the upgrade map; add a TLS server with your actual
certificate/key paths and private bind:

```nginx
map $http_upgrade $connection_upgrade {
    default upgrade;
    '' close;
}
server {
    listen 192.168.1.50:443 ssl;
    server_name lab.home.arpa;
    ssl_certificate /etc/nginx/tls/lab-fullchain.pem;
    ssl_certificate_key /etc/nginx/tls/lab-key.pem;

    location = /mcp { return 403; }
    location ^~ /mcp/ { return 403; }
    location / {
        proxy_pass http://127.0.0.1:18100;
        proxy_http_version 1.1;
        proxy_set_header Host $http_host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header Forwarded "";
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_read_timeout 660s;
        proxy_buffering off;
    }
}
```

Origin and WebSocket subprotocol headers pass through unchanged. The explicit
Upgrade/Connection forwarding follows the official
[Nginx WebSocket guidance](https://nginx.org/en/docs/http/websocket.html).
Use your existing TLS policy and a certificate whose SAN matches the browser
hostname/IP. Trust its issuing CA on each client; do not bypass certificate
errors or disable verification. WSS uses the same trusted origin as HTTPS.

### Verify before regular use

Sign in through the HTTPS origin and inspect the cookie: Secure, HttpOnly,
SameSite=Strict. Direct HTTP writes must fail; forged forwarding headers from
another peer must not grant HTTPS status. Check that wrong Host/Origin/CSRF
requests fail, standard viewing connects using WSS, tickets cannot be reused,
and Take/Release Control retains ownership checks. Test from the intended client
network and confirm that unintended interfaces and MCP routes remain blocked.
Do not assume that an HTTP login-page health response proves HTTPS login works.

## Web, MCP and external Kodi are different connections

Web uses administrator sessions and CSRF. MCP uses bearer tokens, protocol
headers, client permissions and target/source/workspace grants; a Web login does
not authorize an agent. Every installation mode, including LAN HTTP, retains
`http://127.0.0.1:18100/mcp` for local clients. Never put tokens in URLs or query
strings. HTTPS proxy examples above block remote MCP; enabling it requires a
separate deliberate endpoint/ACL/trust configuration. See
[native LAN MCP](NATIVE-LAN-MCP.md) for that existing advanced workflow.

External Kodi JSON-RPC has its own private address, port, Kodi authentication and
native verification. Narrow Bridge has separate installation, authenticated
pairing, reviewed-build provenance and capabilities/scopes. HTTPS on Web does
not encrypt those independent downstream connections or approve an unknown
Bridge. Their private-network transport acknowledgements, production/development
classification and agent permissions remain required. Do not expose Kodi
JSON-RPC or Bridge ports publicly or disable authentication to make Web work.

## Restarts and upgrades

Keep the selected mode/bind, `.env`, proxy overlays and certificates. Installer
management validates saved mode/bind against the installation receipt; it does
not change mode during restart. `configure` refuses to overwrite `.env`.

Legacy configuration without a mode retains HTTPS. Preserve that behavior when
preparing successor configuration; explicitly select HTTPS instead of using the
fresh local-HTTP default. Windows successors preserve omitted Caddy options.
An explicit HTTP-to-HTTPS successor change requires matching saved configuration;
HTTPS downgrades are rejected. Proxy-only removal requires
`--remove-https-proxy --web-access-mode https` and keeps HTTP login disabled.
Retain original receipts and CA material. See
[upgrade and rollback](LINUX-V1-UPGRADE-ROLLBACK.md#preserve-web-security-mode);
never initialize an existing database or edit historical receipts to switch modes.
