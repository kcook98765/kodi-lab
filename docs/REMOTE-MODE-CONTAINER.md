# Remote-mode container lifecycle

This is a Phase 2 development deployment, not a V1 release. It runs one
installed Kodi Lab wheel for Web, API, and HTTP MCP against external Kodi
targets. It creates no managed Kodi containers and has no lifecycle broker,
Docker socket, proxy, or local Kodi dependency.

The later Phase 3 Compose foundation adds an optional `lab`-profile broker.
Normal commands below start only Web. Web mounts a private broker-socket volume
read-only even in remote-only mode; no broker socket exists until the optional
service starts. Web never mounts the Docker socket. See `BROKER-CONTAINER.md`.

## Build and initialize

From the repository checkout, with Docker Compose available:

```sh
docker compose build web
docker compose run --rm --no-deps --entrypoint kodi-lab-admin web version
docker compose run --rm --no-deps --entrypoint kodi-lab-admin web initialize
docker compose run --rm --no-deps --entrypoint kodi-lab-admin web set-password
docker compose up -d --wait --no-build web
```

The build stage uses the repository as input to build a wheel and export
hash-checked runtime requirements from `uv.lock`. The final image installs
the wheel and requirements; it has no source checkout. The separate wheel-build
stage can later be replaced by a reviewed prebuilt wheel input. No image is
published by this procedure. The current package version is `1.0.0rc1`, a local
unpublished prerelease; the earlier container checkpoint used `0.0.0`.

The named Compose volume `remote_state` is mounted at
`/var/lib/kodi-lab` and is the single private state root. It is owned by the
image's unprivileged UID 10001 and holds SQLite, the encrypted external-target
and bridge secret directories, the master-key file, and durable Web/MCP state.
`initialize` accepts only an empty private root (Compose may have created the
empty mount directory). It creates private directories, a new random master key,
and a database at the installed Alembic head. It publishes a format-1 marker
binding the key digest only after migration succeeds. Repeating it on a complete
installation at the installed head validates and changes nothing. A partial,
unversioned, or mismatched installation is rejected; inspect and recover it
explicitly. The older container commands `migrate` and `generate-master-key`
are rejected in this remote container so they cannot bypass lifecycle checks.
They remain available to the standalone installed-host procedure.

`set-password` prompts twice without echo and retains the existing one-local-
administrator semantics. No password or key is supplied by Compose. Run it
after initialization and before exposing the Web interface. A Phase 1 volume
with a complete private key, secret directories, and versioned database can be
adopted with explicit `upgrade`. New-image startup rejects its missing format
marker until `upgrade` backs up the volume and adds the marker, even if the DB
is already at head. A missing key with existing database or encrypted secrets
is an error, never a reason to generate another key.

Normal startup holds an exclusive state lock for the Web process lifetime and
checks private root/key/database metadata, SQLite integrity, one Alembic
revision equal to the installed head, the required marker/key relationship,
and absence of an unfinished upgrade. It reads the existing key into the
existing application contract and starts Web. It never initializes, backs up,
or migrates on `docker compose up`.

## State, backup, upgrade, and rollback

The application rollback set is SQLite, the exact master key, both encrypted
secret trees, the state marker, and Web-owned durable ZIP, workspace, and
promotion artifacts. The backup excludes root-owned managed snapshots,
screenshots, stages, SQLite sidecars, Docker runtime data, and
backup/quarantine management. The SQLite backup API folds committed sidecar
content into a standalone database. Snapshot disaster recovery is a separate
stopped-state copy and ownership proof. See `MANAGED-STATE-RECOVERY.md` for the
complete ownership map and both recovery sets. Stop both Web and broker before
a managed-state backup, upgrade, restore, or clean-volume recovery operation.

Stop Web before backup or upgrade. The lock rejects a second Web process or
concurrent lifecycle command. A Phase 1 Web image did not hold this lock, so
stop it explicitly before operating on its volume. With the new image:

```sh
docker compose stop web
docker compose run --rm --no-deps --entrypoint kodi-lab-admin web backup
docker compose run --rm --no-deps --entrypoint kodi-lab-admin web upgrade
docker compose up -d --no-build web
```

`backup` publishes a UUID-named directory under `backups/` in the same named
volume. Its private manifest records UTC creation time, installed package hash,
format, Alembic revision, key digest, exact file inventory/hashes, and completion.
Directory/file modes and numeric UID/GID are checked and recorded; read-only
published ZIP modes remain `0500`/`0400`. Publication uses a staging
directory, verification, and atomic rename. A failed backup prevents migration.
Backups survive application-container replacement but not destruction of the
named volume; export the private backup directory separately for host disaster
recovery. A clean-volume application restore uses `recover-clean` after a
verified backup directory has been copied to `backups/ID` in a fresh volume.
There is no automatic retention or public export in this phase.

`upgrade` validates current state, creates and verifies its own pre-upgrade
backup, writes an unfinished-upgrade marker, runs packaged forward migrations
when needed, verifies the installed head, and writes a receipt binding the
backup to the transition. If already at head with a marker it makes no backup
or database change. A markerless Phase 1 volume is backed up and adopted at
the same revision without running a migration. Failure
after the marker blocks Web startup until recovery. Keep the previous reviewed
application artifact/image as well as the backup. Verify `/login`, authenticated
state, and relevant secrets after starting the new application. Compose's
loopback `/login` healthcheck and `up --wait` establish process/HTTP readiness;
authenticated state and secrets still require operator verification.

To roll back a schema-changing upgrade, stop Web and use the exact backup ID
printed by `upgrade` (also stored in `.last-upgrade.json` or
`.upgrade-pending.json`):

```sh
docker compose stop web
docker compose run --rm --no-deps --entrypoint kodi-lab-admin web restore BACKUP_ID --confirm BACKUP_ID
# Set KODI_LAB_REMOTE_IMAGE to the retained previous local image reference.
docker compose up -d --wait --no-build web
```

`restore` accepts only the backup bound to the most recent successful or
unfinished upgrade. It verifies the complete manifest, SQLite revision, and key
digest before touching current state. It marks restoration in progress, moves
newer state to a private `quarantine/UUID` directory, restores the exact prior
state, and verifies the restored revision and key before clearing the marker.
An interrupted restore blocks startup; retry the exact backup ID with the same
confirmation. Each retry quarantines any partial state. Retain all quarantine
and backup directories until the restored application has been verified.
Root-owned managed snapshots stay in place and keep their original ownership;
the application rollback does not claim to roll their bytes back. Its manifest
records their hashes as dependencies and restore rejects any mismatch before
mutating application state. Merely
selecting an old image does not roll back a changed schema. The previous image
must understand the restored revision; there are no downgrade migrations.

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
docker compose up -d --wait --no-build web
```

Keep the named volume. Removing it deliberately destroys the database, login
state, master key, and encrypted credentials. Check the installed package
identity with `docker compose run --rm --no-deps --entrypoint kodi-lab-admin web version`
before and after an image change.

## Current limits

The operations above remain explicit. This Compose slice does not
include managed local Kodi or a future lab profile. It does not prove access to
any physical Kodi device, Docker Desktop networking, or a public deployment.
