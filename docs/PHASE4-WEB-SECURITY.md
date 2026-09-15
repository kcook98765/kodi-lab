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
