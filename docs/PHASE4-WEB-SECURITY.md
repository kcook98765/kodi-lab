# Phase 4 localhost Web security and execution contract

This executable supplement records the approved first Phase 4 slice.
`V1-SPEC.md` remains authoritative and is not amended.

## Exposure and rendering

The product default is exactly `127.0.0.1:8100`; ephemeral live validation uses
`127.0.0.1:8180`. Typed configuration rejects other hosts and ports. The app uses
FastAPI, autoescaped Jinja2 HTML, local CSS, and minimal vanilla JavaScript with
two-second polling. Nginx, LAN, HTTPS, CORS, remote assets, WebSockets, and SSE are
outside this milestone.

`Secure=false` on the session cookie is approved only for loopback HTTP. Before
LAN, reverse-proxy, or HTTPS deployment, the complete bind, trusted-proxy, Host,
origin, cookie, TLS, and throttling policy must be reviewed again.

## Authentication and sessions

There is exactly one fixed local administrator. Credentials can be created or
replaced only with `kodi-lab-admin set-password`, which uses hidden interactive
input. Argon2id hashes are the only password representation persisted. Changing
the password invalidates all sessions.

Login generates a 256-bit opaque session bearer and independent 256-bit CSRF
token. SQLite stores only the SHA-256 session-token digest, CSRF token, creation
and bounded last-use timestamps, and seven-day absolute expiry. Idle expiry is
12 hours. Expired and logged-out sessions are deleted. The browser cookie is
`kodi_lab_session`, HTTPOnly, SameSite=Strict, Path `/`, no Domain, and not Secure
only under the approved loopback HTTP profile. Successful login always rotates
the session. Passwords and bearer tokens are never application-log fields.

Five failed logins for a client source trigger an in-process exponential backoff
capped at 30 seconds. This only bounds localhost Argon2 load and is not a LAN
rate limiter.

## Browser mutation boundary

Every authenticated mutation requires both the session cookie and exact
session-bound CSRF token. HTML logout submits the token as a form field; JSON
lifecycle calls use `X-Kodi-Lab-CSRF`. Exact Origin is preferred and a
same-origin Referer is required when Origin is absent. Trusted Host validation
prevents a caller-selected Host from defining the accepted origin. CORS is not
enabled.

Lifecycle JSON bodies must be exactly empty objects. The only semantic authority
from the browser is the canonical ManagedInstance UUID in the route. Container
ID, revision, state, image, network, timeout, command, and Docker options are
rejected and never become broker inputs.

## Storage, broker, and readiness composition

Web authentication and runtime services use short-lived SQLAlchemy sessions.
`ManagedRuntimeStatusService` joins canonical target/instance/revision state,
bounded broker inventory/process observation, and readiness registry state. Its
DTO omits raw Docker, endpoint, database, and exception details.

Lifecycle coordination reloads current broker inventory and canonical revision
immediately before an explicit provision, start, stop, or deprovision call. The
Web process uses only the typed `BrokerClient`; it has no Docker socket, root
privilege, shell, raw broker proxy, generic JSON-RPC, or Docker SDK invocation.

Start schedules one effective readiness task per instance after broker start.
The existing 90-second coordinator retains fixed process, ping, and identity
probes. Generation checks prevent stale completion from replacing newer state;
stop/deprovision cancel or clear observations. Results are ephemeral. A Web
restart reconstructs storage/container/process status and reports readiness as
not checked.

## Route surface

HTML:

- `GET /login`
- `POST /login`
- `POST /logout`
- `GET /`

Same-origin session-authenticated JSON:

- `GET /api/instances`
- `GET /api/instances/{instance_id}`
- `POST /api/instances/{instance_id}/provision`
- `POST /api/instances/{instance_id}/start`
- `POST /api/instances/{instance_id}/stop`
- `POST /api/instances/{instance_id}/deprovision`

This is not a public API. It has no API tokens and no generic execution,
broker, Docker, or JSON-RPC route.

Phase 5 Step 5 adds authenticated read-only routes without changing this
mutation boundary:

- `GET /runtimes`
- `GET /api/runtime-matrix`
- `GET /api/targets/{target_id}/runtime-selection`

The two matrix query preferences accept only literal `true` or `false` for
Include Legacy and Include Pre-release. They are not catalog state. Global
Trusted Custom Build enablement remains deployment configuration and cannot be
sent by the browser. Target resolution returns only semantic definition identity
and revision number; it never returns or accepts revision UUID, image/artifact,
evidence, support, approval, trust, or Docker authority. No instance or catalog
mutation is added by these routes.

Phase 6 adds one destructive disposable-reset flow without changing network or
session policy. The authenticated confirmation names the exact pin and fixture
and explains data loss. Its JSON POST requires same-origin, CSRF, and literal
`{ "confirmed": true }`; other fields are forbidden. It creates only a durable
queued reset and returns 202. Background execution requires the reset-specific
database lease, and conflicting lifecycle calls return semantic 409. Web still
uses the restricted broker client and has no Docker socket. Password re-entry is
not required for this localhost single-admin profile; future LAN exposure must
revisit sensitive-action reauthentication.

Phase 11 Slice 5 adds one same-origin, session-authenticated, CSRF-protected
custom-preset creation route in the existing MCP administration area. Its JSON
body contains only a bounded display name and immutable built-in template UUID.
Jinja autoescaping keeps the name text-only. The server creates the authority
UUID and copies only registered permission rows; no token, permission string,
tool, target, protocol version, broker input, or arbitrary form payload is
caller-controlled or written to audit. Built-ins have no mutation route, and
custom rename, permission editing, and deletion are absent.

Legacy MCP support adds no Web compatibility toggle, client protocol preference,
cookie authentication, or protocol-specific privilege. Exact `2025-11-25` HTTP
requests still require the durable MCP bearer on every request and never use the
Web administrator session.

Phase 12 Slice 1 extends the existing MCP administration page/API with exact
DevelopmentSource grant/revoke and existing-workspace binding. GET may list one
client's grants, eligible ready/byte-present unbound workspaces, and current
bindings. POST/DELETE mutations require the same authenticated Web session,
same-origin validation, Trusted Host enforcement, and CSRF token as every other
administrator mutation. Request authority is UUID-only: no route accepts a
source locator, host path, binding origin, target client, transfer/share flag,
workspace bytes, or MCP self-service credential. There is no unbind/transfer,
workspace creation, or source registration through these MCP administration
routes.

Phase 12 Slice 2 changes no Web mutation route. There is no Web button for
MCP-origin creation and no manual Web MCP file editor. Existing authenticated
grant/revoke and one-time eligible-workspace binding remain the only Web
administration. `workspace_create` creates its `mcp_created` binding atomically
inside the MCP application service, and the one-owner primary key prevents that
workspace from later appearing eligible for Web rebinding. Web still accepts no
source locator/path, workspace storage path, file content, edit path, request
replay control, force flag, target, broker, Docker, shell, or command input for
this slice.

Phase 12 Slice 3 adds read-only retention visibility to existing MCP client and
workspace-binding administration: owner enabled state, semantic retention
classification, applicable deadline and automatic-cleanup blocker. Disabling a
client still revokes MCP access immediately but does not delete bytes; re-enable
clears its durable quarantine clock. Existing UUID-specific abandonment and
confirmed cleanup routes remain the only manual controls. There is no force
cleanup, ignore-state, cleanup-all, MCP cleanup, path, command or policy-timing
input.

Phase 12 Slice 4 adds only read-only semantic visibility to the existing MCP
client/workspace-binding administration: latest deployment and execution UUID,
target UUID, exact generation and bounded state, including ambiguity or
reconciliation-required classification. It accepts no new Web request field and
adds no deploy, execute, retry, reconcile, force, lifecycle or cleanup button.
Existing session authentication, Trusted Host, same-origin and CSRF controls
remain the sole Web administration boundary. Recovery continues through
existing UUID-specific target/workspace administration; no path, add-on ID,
container, broker operation, raw Kodi response or log is rendered.

Phase 12 Slice 5 reuses the existing promotion-plan review and
approve-and-execute route and adds one confirmed rejection route. A review
renders semantic source/workspace/target/generation, originating MCP client,
add-on identity/version, bounded change counts, exact existing review diffs,
validation/deployment/execution classifications, expiry and conflict state.
Internal source, backup and workspace paths remain absent.

Approval and rejection require an authenticated Web administrator cookie,
Trusted Host, same-origin, CSRF token, exact UUID route identity and literal
`{ "confirmed": true }`; unknown fields are rejected. MCP bearer authentication
is deliberately not accepted by these routes. Client disable or MCP source/
target grant revocation does not erase the review or act as approval/rejection;
Web independently rechecks canonical Phase 9 plan safety before approval.
There is no MCP approval route, hidden administration tool, force, alternate
source, apply-only, rollback, Git or repository publication request field.

## Phase 13 Slice 1 external-target administration

All external-target pages and read APIs require the authenticated Web session.
Registration, endpoint edit, credential rotation, probe, observation
confirmation, enable/disable and deactivation additionally require same-origin
and CSRF validation through the ordinary mutation-session dependency. Trusted
Host remains global. MCP bearer tokens do not authenticate these routes.

Responses expose logical name, structured endpoint, authentication mode,
separate states, bounded Kodi/protocol observations, configured/verified
capabilities and timestamps. They never expose a password, Authorization
header, secret reference, raw response, raw JSON-RPC, DNS data or socket error.
Failures use the closed semantic external-target error vocabulary.
