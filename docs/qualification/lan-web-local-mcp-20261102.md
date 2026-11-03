# Preserve local MCP alongside LAN HTTP Web

Owner-requested revision of draft PR #191 / issue #189, with documentation in
draft #192. Starting implementation `7bac447`; starting docs `a688854`.

## Transport boundary

The existing Web process opens two sockets for one application, event loop and
lifespan. Base Compose always retains `127.0.0.1:18100 → container:8100`.
Optional verified `compose.lan-http.yaml` appends only
`SELECTED_PRIVATE_IP:18100 → container:8101` for browsers. The installer selects
this overlay for LAN mode on install/start and its supported Compose commands.
Manual bundle workflows must include it explicitly. No new service is added.

In LAN mode, MCP admission requires Uvicorn's socket-derived `scope.server` port
to match the primary listener. The browser socket and missing transport metadata
fail closed before MCP dispatch. Host, Origin, source address and forwarding
headers do not select the trusted listener. Local bearer authentication, agent
permissions, target grants and separately configured MCP proxies are unchanged.
Both sockets are bound before serving; bind failure closes opened sockets through
the shared exit stack. Duplicate configured ports are rejected.

Installer readiness now requires local Web health and anonymous local MCP
`401 Bearer`, plus LAN Web health and explicit LAN MCP denial. The URL and warning
explain that local MCP remains available. No production state, grants or secrets
were used by these checks during development.

## Focused evidence

**FOCUSED PASS: 283 cases across 11 individually invoked files.**

| File | Passed |
| --- | ---: |
| `tests/web/test_lan_listener.py` | 3 |
| `tests/web/test_http_access.py` | 9 |
| `tests/packaging/test_web_access_installer.py` | 16 |
| `tests/packaging/test_linux_v1_bundle.py` | 45 |
| `tests/web/test_run.py` | 5 |
| `tests/web/test_mcp_stdio.py` | 7 |
| `tests/web/test_proxy_trust.py` | 45 |
| `tests/packaging/test_host_installer.py` | 65 |
| `tests/packaging/test_https_ingress_installer.py` | 58 |
| `tests/packaging/test_standalone_installer_bootstrap.py` | 12 |
| `tests/test_foundation.py` | 18 |

The new tests drive the production two-socket entry point with real ephemeral TCP
sockets and the existing isolated application/authentication fixtures. They prove
authorized local tool discovery and target inspection, missing/invalid credential
denial, denied agent permission, ungranted neighbor rejection, grant removal and
client disablement. LAN login/session use succeeds while MCP is denied with the
same valid token, forged Host/Origin/forwarding metadata and all supported HTTP
methods. The test deliberately trusts forwarded client/scheme headers to prove
they cannot change the destination socket. Production Compose retains its
existing explicit proxy trust protections.

Rendered Compose is checked for the exact loopback and LAN target-port mappings;
bundle verification covers the LAN overlay hash and inventory. Installer tests
check both endpoint probes and preservation. HTTP cookies/CSRF/WS tickets,
HTTPS/WSS trust, Windows Caddy/successor and HTTP/stdio authorization/replay
regressions passed. Focused Ruff and strict mypy passed; the extensionless bundle
tool passed separate Ruff/mypy, and the staging script passed `bash -n`.

Independent OpenCode 1.19.3 read-only review returned **CLEAR**, with no actionable
security or correctness findings. It inspected the locked Uvicorn implementation,
current source/docs and focused evidence using only read/glob/grep; shell, edits,
MCP and delegation were denied. Production signal/lifespan handling and partial
bind cleanup were assessed statically; the TCP fixture lets TestClient own the
application lifespan. No installed lifecycle acceptance is claimed.

These are focused source and real-socket fixture results. No full suite, image
build, new live Docker fixture, managed runtime acquisition, native Kodi/Windows
acceptance, merge or deployment was performed for this revision. Claw production
containers/ports, databases, credentials, grants and unrelated workloads were
preserved. The earlier browser/Caddy smoke evidence remains historical evidence
for the original draft, not a new installed deployment claim.
