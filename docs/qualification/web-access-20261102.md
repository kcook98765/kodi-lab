# Simple Web access focused qualification

Issue #189; starting main `1bc363d`. Source work only on Claw, in isolated
worktrees. No deployment, merge, database migration, full test suite or managed
Kodi acquisition/startup was performed.

## Implemented behavior

- Fresh bundle configuration records `local-http` and loopback publication.
- Ubuntu LAN HTTP requires an exact RFC1918 IP and typed confirmation before
  installation state is created. It disables MCP on the shared listener.
- Missing mode retains legacy HTTPS. HTTPS keeps Secure cookies, HTTPS writes
  and WSS; Compose trusts no forwarding peer until explicitly configured.
- Successors preserve mode/Caddy when options are omitted. Explicit HTTP-to-HTTPS
  is supported; HTTPS downgrades are rejected. Proxy removal remains explicit
  and retains HTTPS enforcement.
- Browser request IDs use cryptographic random bytes on insecure HTTP origins.
  Existing noVNC tickets, authentication and exclusive control remain enforced.

## Focused results

**FOCUSED PASS:** 327 distinct cases across 14 individually run files:

| Area | Cases passed |
| --- | ---: |
| HTTP login/cookies/Origin/Host/CSRF/WS/ticket reuse/MCP isolation | 9 |
| New installer modes, confirmation, rendered Compose, legacy receipt protection | 16 |
| Windows HTTPS installer, real verifier, successor preservation/downgrade/upgrade | 58 |
| Downloaded bootstrap | 12 |
| Existing host installer | 65 |
| Release bundle packaging | 45 |
| Exact proxy trust, HTTPS cookies, forged headers and WSS | 45 |
| Standard noVNC configuration | 7 |
| Current Kodi browser behavior | 4 |
| Live-preview browser behavior without `randomUUID` | 1 |
| noVNC authorization/admission | 24 |
| noVNC human ownership and handoffs | 15 |
| Authentication, session expiration, CSRF and login throttling | 8 |
| Bootstrap configuration/Trusted Host validation | 18 |

Focused Ruff format/lint and strict mypy passed for affected Python paths.
The extensionless `scripts/linux-v1-bundle` also passed separate Ruff and strict
mypy checks (the focused wrapper accepts `.py` source arguments only).

Additional isolated checks: 12 Docker/Caddy cases passed; seven opt-in volume
permission Docker cases were skipped. The real pinned Caddy fixture passed TLS
validation, exact peer trust, forwarding-header sanitization, WSS, restart and CA
persistence. It used an existing immutable application image as an echo backend,
with disposable resources and no application database. Eleven permission unit
cases passed. No image build or managed runtime was needed.

Two real Chromium cases passed against an ephemeral loopback Uvicorn server:
localhost HTTP and a DNS-mapped insecure HTTP origin (`isSecureContext=false`).
Both proved browser login, non-Secure/HttpOnly/SameSite=Strict cookies, pinned
noVNC rendering of a synthetic RFB frame through an authorized WS ticket, Stop
Viewing and no page errors. Broker/selection/RFB were fixture doubles; this is
not native Kodi acceptance. Initial smoke harness failures (redirect expectation,
selected-page fixture, CSP-compatible polling and fixture pixel format) were
corrected without weakening application CSP or changing noVNC vendor bytes.

The seven existing installer Caddy/configuration/network/permission/successor
preflight functions are AST-identical to starting main. Before/after running
container IDs and published production ports were identical; the unrelated DinD
container was also preserved. Test resources were individually removed by their
own fixtures; no host-wide cleanup was used.

## Independent review and limits

OpenCode 1.19.3 performed read-only review using only read/grep/glob, with shell,
edits, MCP and delegation denied. Initial implementation review was CLEAR.
Final review found one P2 documentation typo in proxy-removal instructions;
it was corrected and read-only resolution confirmation returned CLEAR.

Native Windows/Desktop acceptance, a clean-host installer build, full release
qualification and deployment remain unperformed. Existing Windows CA/permission
and successor requirements still apply. LAN HTTP intentionally disables local
and remote MCP on that shared listener; use local HTTP or deliberate HTTPS/MCP
networking when agent access is needed. The simple LAN option is Ubuntu only.
