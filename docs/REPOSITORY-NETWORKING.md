# Add-on repository networking

Fresh Kodi Lab installations use HTTP on the selected private LAN address,
normally `http://192.168.1.50:18120`. No certificate or proxy is required.
This serves repository add-ons, Narrow Bridge and approved add-on ZIPs.

HTTPS is optional. If you already operate a reverse proxy with a trusted TLS
certificate, supply its repository origin during fresh installation:

```sh
python3 install.py bootstrap --directory "$HOME/kodi-lab-install" \
  --repository-bind 192.168.1.50 --repository-port 18120 \
  --repository-base-url https://repo.example.net
```

Replace the addresses and hostname. Standard HTTPS port 443 needs no explicit
port; a nonstandard port such as `https://repo.example.net:18443` also works.
Use an origin only: no credentials, path prefix, trailing slash, query or
fragment. The backend stays HTTP on the explicit private address and port;
the public-facing port is independent. Kodi Lab does not install a repository
proxy, issue certificates, configure DNS, install CAs or change your firewall.

Configure the proxy and certificate before installation. Its hostname must
resolve and be reachable from the installer host and every Kodi device. Forward
requests directly to the backend once it starts. Before reporting ready, an HTTPS
installation downloads both feeds' catalogs, SHA-256 checksums, bootstrap ZIPs
and packages, including Narrow Bridge, and matches them to locally verified
repository provenance. TLS chain and hostname verification remain enabled;
redirects, incorrect content and HTTPS failures are rejected without HTTP
fallback. A failed check preserves the installation and records the last
completed stage; it does not stop services or erase state. Do not rerun
initialization blindly.

For the manual bundle workflow, use the same origin with `linux-v1-bundle
configure --repository-base-url https://repo.example.net` alongside the normal
required configuration arguments. Bundle configuration validates syntax;
the automatic external connectivity check belongs to the host installer.
For a manually started bundle, check the public endpoints below yourself.

## Example with an existing Nginx proxy

Add a dedicated repository virtual host to your existing Nginx configuration.
Use your actual certificate files and private backend address. This example
terminates TLS at Nginx and forwards the complete URI without rewriting it:

```nginx
server {
    listen 443 ssl;
    server_name repo.example.net;
    ssl_certificate     /etc/nginx/tls/repo.example.net.fullchain.pem;
    ssl_certificate_key /etc/nginx/tls/repo.example.net.key;
    ssl_protocols TLSv1.2 TLSv1.3;

    location / {
        proxy_pass http://192.168.1.50:18120;
        proxy_set_header Host $host;
        proxy_cache off;
    }
}
```

Use a dedicated hostname with no Web login, authentication gateway, path prefix
or redirect on these download routes. Forward `/install/`, `/stable/` and
`/development/`, including ZIPs and `.sha256` sidecars. Preserve GET and HEAD
requests and Range headers; avoid stale caching of catalog/checksum pairs.
No WebSocket forwarding or trusted-proxy setting is needed for this repository
backend. Keep backend traffic on your trusted private network. HTTPS protects
the device-to-proxy connection; the proxy-to-backend hop in this example is HTTP.

Once installed, these checks should succeed without disabling certificate
verification:

```sh
curl --fail https://repo.example.net/stable/addons.xml
curl --fail https://repo.example.net/stable/addons.xml.sha256
curl --fail https://repo.example.net/development/addons.xml
curl --fail https://repo.example.net/development/addons.xml.sha256
curl --fail --output repository.kodi-lab-1.0.0.zip \
  https://repo.example.net/install/repository.kodi-lab-1.0.0.zip
```

## Kodi devices and trust

Add `https://repo.example.net/install/` as the Kodi file source, install the
stable repository bootstrap ZIP, then install **Kodi Lab Narrow Bridge** from
**Install from repository → Kodi Lab**. The optional development bootstrap at
the same source advertises HTTPS for the development feed independently.
Both descriptors use Kodi's 19-and-later repository format, SHA-256 catalog
verification and package hash sidecars for Matrix 19.x, Nexus 20.x, Omega 21.x
and Piers 22.x preview. See the [Kodi repository format](https://kodi.wiki/view/Add-on_repositories).

The certificate must match the hostname used in the URL. An IP address needs
a certificate valid for that IP. The full certificate chain must be trusted by
the installer host and **each Kodi build/device**; browser trust alone does not
prove Kodi trust. Publicly trusted certificates usually avoid private-CA setup.
For a private CA, supply trust through the platform/build's supported mechanism
before using HTTPS. Kodi's CA store can differ from the operating system's;
support varies by build. Do not disable certificate verification or append Kodi
URL options that bypass it. Native device acceptance remains a separate check.

Kodi Lab is pre-release. Choose the transport for a fresh installation. To
change it, use a fresh installation and reinstall the repository add-ons on
Kodi; this feature provides no installed-origin migration, compatibility
bridge, automatic URL repair or upgrade/rollback workflow. Preserve any
existing installation separately.

## Security boundaries

Repository HTTPS protects repository downloads on the TLS connection. Package
integrity checks and approved publication boundaries still apply. The download
server remains read-only, with its existing exact route allowlist and private
listener binding. A proxy does not grant publication or administration rights.
Only expose the repository packages you intend clients to download; HTTPS does
not add download authentication.

This option does not secure the Web UI or MCP, external Kodi JSON-RPC, or Narrow
Bridge communication. Their authentication, authorization and transport setup
are independent. Narrow Bridge's protocol is unchanged. See the installation
guides for those services; a repository hostname should point only to the
repository backend.
