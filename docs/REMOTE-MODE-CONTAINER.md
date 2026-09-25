# Remote-mode container foundation

This is a Phase 1 development foundation, not a V1 release. It runs one
installed Kodi Lab wheel for Web, API, and HTTP MCP against external Kodi
targets. It creates no managed Kodi containers and has no lifecycle broker,
Docker socket, proxy, or local Kodi dependency.

## Build and initialize

From the repository checkout, with Docker Compose available:

```sh
docker compose build web
docker compose run --rm --no-deps --entrypoint kodi-lab-admin web version
docker compose run --rm --no-deps --entrypoint kodi-lab-admin web migrate
docker compose run --rm --no-deps --entrypoint kodi-lab-admin web generate-master-key
docker compose run --rm --no-deps --entrypoint kodi-lab-admin web set-password
docker compose up -d --no-build web
```

The build stage uses the repository as input to build a wheel and export
hash-checked runtime requirements from `uv.lock`. The final image installs
the wheel and requirements; it has no source checkout. The separate wheel-build
stage can later be replaced by a reviewed prebuilt wheel input. No image is
published by this procedure. The package version remains `0.0.0`.

The named Compose volume `remote_state` is mounted at
`/var/lib/kodi-lab` and is the single private state root. It is owned by the
image's unprivileged UID 10001 and holds SQLite, the encrypted external-target
and bridge secret directories, the master-key file, and durable Web/MCP state.
`migrate` creates the state tree and advances the database to the packaged
Alembic head. `generate-master-key` creates a private key file once and refuses
to replace it. `set-password` prompts for the administrator password. These are
explicit setup operations; normal Web startup does not migrate or initialize.

The small container launcher checks the key file's ownership and mode, reads
it into the existing master-key environment contract, and executes the normal
foreground `kodi-lab-web` command. If the key file is absent or unsafe, startup
fails. Back up and restore the entire quiescent state volume as one unit: the
database, encrypted credentials, and exact master key belong together.

## Access and recreation

Open `http://127.0.0.1:18100/login` on the Docker host, sign in, then open
`/pair-kodi`. The Compose port is published only on host loopback. A different
local port can be selected with `KODI_LAB_PUBLISHED_PORT` on every Compose
invocation. The app listens on `0.0.0.0:8100` inside its container so Docker
can forward the published port; the Web Trusted Host middleware admits the
configured `127.0.0.1` and its existing `localhost` default, and rejects
other Host values.
Browser mutations require the exact request origin or referer plus CSRF, and
HTTP MCP retains its bearer authentication, protocol gate, and explicit Host
checks; an Origin header, when supplied, must name an allowed origin. Port
matching for MCP is tied to each explicitly allowed host, never to every host.

The direct HTTP and non-Secure session cookie are suitable here only for the
host-loopback publication. LAN browser access and TLS termination need a
separate reviewed deployment configuration. External Kodi targets are reached
outbound through Docker's normal networking; the target's address and firewall
must permit traffic from the Docker host/network. No protected or production
Kodi target is part of this setup.

To replace only the application container while keeping its state:

```sh
docker compose stop web
docker compose rm -f web
docker compose up -d --no-build web
```

Keep the named volume. Removing it deliberately destroys the database, login
state, master key, and encrypted credentials. Check the installed package
identity with `docker compose run --rm --no-deps --entrypoint kodi-lab-admin web version`
before and after an image change.

## Current limits

Initialization, password setup, upgrades, backup, and restore remain manual;
the safe automated lifecycle belongs to Phase 2. This Compose slice does not
include managed local Kodi or a future lab profile. It does not prove access to
any physical Kodi device, Docker Desktop networking, or a public deployment.
