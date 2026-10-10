# Native LAN MCP setup

Kodi Lab already implements Streamable HTTP at exact `/mcp` in its Web
process. Direct HTTP and the optional local stdio adapter use the same client
identities, bearer verifier, permission presets, immutable custom snapshots,
target grants, policies, current scopes and tool handlers. No second MCP server
or authorization database is required.

## Exposure and configuration

The fail-closed default remains loopback only: Compose publishes
`127.0.0.1:18100` to container port `8100`; the installed Web process listens
inside its container. Do not publish that plaintext port on the LAN. Keep the
Web administration routes and non-Secure Web cookie on loopback.

For direct LAN agents, an administrator supplies a narrowly configured HTTPS
proxy forwarding only exact `/mcp` to the existing loopback port. Use
[the nginx template](../deployment/mcp-lan-nginx.conf.template). It is an
unrendered server block, not an installed service. Supply every marker:

| Marker | Required administrator value |
| --- | --- |
| `PRIVATE_LAN_IPV4` | This host's explicit RFC1918 LAN interface address, never a wildcard |
| `TLS_PORT` | An available TCP port on that interface |
| `MCP_HOST` | The exact private address or LAN DNS name in the certificate |
| `MCP_AUTHORITY` | Exact HTTP Host authority, including a nondefault port; omit `:443` for default HTTPS |
| `CERTIFICATE_CHAIN_FILE`, `CERTIFICATE_KEY_FILE` | Absolute paths to the server certificate chain and protected private key |
| `AUTHORIZED_LAN_CIDR` | The specific authorized private LAN subnet |
| `LOOPBACK_WEB_PORT` | The actual existing host loopback publication, normally `18100` |

Validate the rendered configuration with `nginx -t` before a separately
approved deployment. Firewall/ACL rules must restrict this listener to the
chosen LAN; do not forward it from a router or expose it publicly. Preserve
Host, Origin and bearer headers. Do not strip Origin, rewrite it to a trusted
value, disable certificate checks, log headers/bodies/query strings, or proxy
`/api`, `/login`, `/mcp/`, arbitrary paths or Web cookies. The template turns off
request/error logging and rejects query strings. Its exact authority check
also rejects unknown virtual hosts. Application Host/Origin checks remain active.

Set `KODI_LAB_MCP_LAN_URL` to the actual HTTPS URL ending in `/mcp`. It has no
default. Source and release Compose pass it to Web without changing the
loopback port mapping. URL credentials, queries, fragments, plaintext HTTP,
public IPs, wildcard hosts and IPv6 are rejected. A DNS name must resolve only
to RFC1918 IPv4 addresses for readiness verification.

If the endpoint uses a private CA, install that CA in every agent's trust store.
For the Kodi Lab readiness check, optionally set `KODI_LAB_MCP_LAN_CA_FILE` to
an absolute readable PEM CA bundle path. In Docker this is a **container path**;
an administrator must mount that certificate read-only through a local Compose
override. A CA certificate is public trust material; never mount the TLS private
key into Web. Otherwise readiness uses the operating system trust store. This
setting does not distribute certificates to agents.

The authenticated Agents page checks the configured endpoint without a token,
with TLS validation, no ambient proxy, no redirects and a two-second total
budget. It verifies the endpoint returns this running MCP process's opaque
instance marker with its anonymous Bearer 401 response. This marker grants no
authority. The check does no Kodi I/O or durable authority mutation. On failure
or absent configuration, the page keeps Local / stdio as the default and
labels LAN unavailable. A successful check verifies server-side reachability;
each agent computer must separately verify its network and certificate trust.

## Agent setup

Open **Agents**, deliberately choose a client preset, grant the exact target and
current Home inspection scope, then issue a token. Choose **LAN / Streamable
HTTP** when available. Copy the verified URL and generated HTTP configuration
into protected user settings on the other LAN computer. The generated server
entry uses `type: "http"`, `url` and `headers.Authorization: "Bearer …"`;
Claude Code and VS Code accept this shape, with their respective outer
configuration wrapper. It does not imply all versions of those applications
negotiate a supported protocol. Never paste the token into a URL, shell command,
shared project file, or log. Web cookies are not MCP credentials. Tokens remain
show-once and are never recovered on page reload or stored in browser storage.

HTTP supports exact MCP `2026-07-28` (stateless `server/discover`, per-request
metadata) and `2025-11-25` (stateless `initialize`, then versioned requests).
Both return JSON over standard Streamable HTTP; standalone GET streams and
DELETE sessions are unsupported (405). All applicable POST requests authenticate,
including initialization, notifications, discovery and every tool call. Invalid
Origin is rejected even on unsupported methods. No sessions are created.
Older revisions, including `2025-06-18`, remain unsupported. The recorded
OpenCode `1.14.39` uses `2025-11-25`; the recorded Codex CLI `0.154.0` offered
`2025-06-18` and was incompatible. Do not infer current application compatibility
from the configuration syntax or a historical version; see isolated test evidence.

Local / stdio continues to require the installed `kodi-lab-mcp-stdio` command
on the agent computer, `KODI_LAB_MCP_TOKEN` in its environment, and an exact
loopback HTTP `/mcp` URL in `KODI_LAB_MCP_URL`. The Agents page starts with this
server's configured Web port (`8100` package/container default, `8180` in the
isolated preview); use the actual local publication or forwarding port instead
when applicable (normally `18100` for Compose). A configured port is not a
client-side reachability check.
Remote agents choosing stdio still require forwarding. The adapter remains
modern-only (`2026-07-28`); direct HTTP avoids that installation/forwarding.

Protocol and client configuration references:
[2025-11-25 transport specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports),
[SDK protocol eras](https://ts.sdk.modelcontextprotocol.io/v2/protocol-versions),
[Claude Code configuration](https://code.claude.com/docs/en/mcp),
[VS Code configuration](https://code.visualstudio.com/docs/agent-customization/mcp-servers).

## Later activation and rollback

The application changes supply configuration validation, transport checks and
agent setup. They do not install a proxy, choose a permanent endpoint, issue
certificates, update firewall rules or activate production. A later activation
requires separate approval and these machine-specific preparations:

1. Reserve the permanent private LAN address or DNS name and TCP port. Ensure
   DNS resolves only to the intended private interface, and the certificate SAN
   covers the exact hostname or address. Supply its chain and protected private
   key to the proxy; install the issuing CA in every client when necessary.
2. Render and validate the proxy template against the actual loopback Web
   publication. Bind only the selected private interface; restrict both proxy
   ACL and firewall to authorized LAN clients. Keep router port forwarding and
   public listeners disabled.
3. Install a separately reviewed application build, set its explicit LAN URL
   and optional read-only CA bundle mount, and retain the existing loopback
   publication. Keep authenticated Web administration on its established access
   path; the MCP proxy does not provide remote Web administration. An
   authenticated Web administrator verifies the displayed endpoint and uses
   existing clients, presets, immutable snapshots, grants and current scopes.
   Enabling a listener must not create clients or expand permissions.
4. From a second authorized LAN host, verify certificate trust, both supported
   MCP revisions, initialization/discovery and an approved read-only target.
   Check missing, expired and revoked credentials, ungranted targets,
   insufficient permissions, current scope revocation, Origin/Host rejection
   and alternate-route denial. Confirm local stdio and the Agents examples
   still work with their actual ports. Use isolated credentials for acceptance;
   do not infer client reachability from the server's readiness result.

Before activation, retain the previously approved application build and proxy,
environment and firewall configuration. To roll back exposure, disable only
this MCP LAN listener and its firewall allowance, clear `KODI_LAB_MCP_LAN_URL`
and its optional CA setting, and reload the affected configuration through the
approved operational procedure. Revert the application build if required,
keeping existing database and credential state intact. Local loopback/stdio
remain the fallback. This implementation requires no schema migration or grant
rewrite; rollback must not disable bearer, TLS, Origin or Host protections to
keep a remote connection working.

## Qualification boundary

Run focused transport/config/UI tests and HTTP/stdio authorization parity tests
in disposable state. Real SDK requests through an isolated TLS proxy verify
network transport and certificate handling; fixture Kodi adapters verify health
and grant denials without production access. This is separate from an actual
second physical LAN host and live Kodi acceptance. Report those gaps explicitly.
No production client, credential, target, pairing, migration, deployment or
permission change is part of this implementation.

The real-network test is opt-in: set `KODI_LAB_TEST_LAN_IPV4` to the test host's
actual private address and run `tests/web/test_mcp_lan.py`. It starts a disposable
loopback app and renders the real nginx template with a temporary certificate
trusted only by test clients. Its database, Kodi adapters, clients and credentials
are disposable; no existing preview or production MCP identities are reused.

Second-host testing additionally requires `KODI_LAB_TEST_RYZEN=1`,
`KODI_LAB_TEST_RYZEN_IDENTITY`, `KODI_LAB_TEST_RYZEN_KNOWN_HOSTS` and
`KODI_LAB_TEST_RYZEN_EVIDENCE`. This optional harness is explicitly scoped to
the authorized test host `kyle@192.168.2.141`. Supply an existing private key
and a known-hosts file whose fingerprint has been verified independently.
[The lightweight client](../scripts/validate-native-lan-mcp-client.py) receives
isolated credentials and a public test CA over process stdin. SSH only launches
it; it makes direct HTTPS requests from that host, with certificate and host
checking enabled, without adapter installation, forwarding or persistent changes
on the remote computer. Its report contains only safe checks and endpoint metadata.

Optional browser verification sets `KODI_LAB_TEST_BROWSER_EVIDENCE` and
`KODI_LAB_BROWSER_TOOLS` to the preview evidence directory and its existing Node
modules directory. It tests the persistent HTTP preview as unconfigured, then
intercepts HTML rendered by the live disposable HTTPS-configured app. Preview
assets are real; token issuance is mocked to avoid changing preview MCP state.
Configured-fixture screenshots are distinct from actual-preview screenshots.
