# Linux broker container decision and deployment foundation

Phase 3 decision: **A, accept broker container for Linux V1**. This is a local
development foundation, not the four-runtime managed lab or a public release.
It requires native Linux Docker Engine behavior. The broker image installs the
same Kodi Lab wheel and locked dependencies as Web; it contains no source
checkout and starts `kodi-lab-broker`. Web continues to use the unprivileged
wheel image. The `lab` Compose profile includes the broker only when selected.
The accepted peer mapping was tested on a daemon without user-namespace
remapping; other mappings need their own validation.

## Security boundary

The broker is trusted and has effectively host-root-level Docker daemon
authority through `/var/run/docker.sock`. Containerization does not sandbox
that authority. Web/API/MCP and Kodi containers must never receive the Docker
socket or Docker-group membership. The broker accepts only its existing
length-bounded Unix protocol and closed semantic operation registry. Requests
cannot supply arbitrary Docker commands, host paths, mounts, exec, or archive
destinations. Trusted SQLite and packaged resources resolve authority;
owned-container labels, immutable IDs, revisions, networks, and preconditions
are rechecked by the existing adapters. Snapshot, add-on, development-source,
and screenshot paths remain fixed or derived from trusted state. No TCP broker
listener exists. Broker absence fails clients with a bounded unavailable error;
ordinary remote Web remains available.

The socket is `/run/kodi-lab/broker.sock` in a named volume mounted read-only
into Web and read-write into the broker. The directory is `0:10001` mode
`0750`; the socket is `0:10001` mode `0660`. The broker runs as UID 0, resolves
the installed `kodi-lab` account, and accepts only `SO_PEERCRED` UID 10001.
Web runs as UID/GID 10001. Both Docker's filesystem boundary and the exact UID
are required. Another container given the socket volume and UID 10001 can
impersonate Web; only trusted Docker deployment administrators can grant the
mount, and they already have daemon-level authority. A compromise of Web alone
does not supply that mount to another container or the Docker socket.

The broker image is read-only apart from `/tmp` tmpfs, the private socket
volume, and the existing private state volume. It drops all capabilities and
adds only CHOWN, DAC_OVERRIDE, and FOWNER needed for root-owned socket and
artifact operations beneath the UID 10001 state root. It does not use
privileged mode, host PID/network namespace, published ports, host paths other
than the Docker socket, or broad host filesystem mounts. The broker's default
Compose network is for ordinary container plumbing, not a broker listener.

## Isolated native-Linux gate

The experiment used Compose project `kodi-lab-phase3-20260925`, its own state
and socket volumes, Web port `127.0.0.1:18137`, a private internal test network,
and one disposable test container. No physical Kodi or protected legacy
resource was contacted or modified.

- A socket probe and then the installed broker image confirmed that a shared
  Unix socket reports the Web peer as UID/GID `10001:10001`, including after
  broker recreation. UID 10002 was blocked by directory permissions; UID 0
  reached the socket but the real broker returned `unauthorized_peer`. The
  broker observed PID 0 for the Web peer across separate PID namespaces; PID
  is audit-only and no host PID namespace is required.
- Web mounted no Docker socket. The broker reached the host Docker daemon
  without privileged mode or host namespaces. Its actual socket was `0:10001`
  mode `0660`; its shared state root remained `10001` mode `0700`.
- Web's typed client received `ping`, the exact 27-operation catalog, and a
  bounded managed inventory. A raw `docker_exec` request returned
  `unknown_operation`. A different container with both socket volume and UID
  10001 succeeded, establishing the explicit impersonation condition above.
- From the installed broker image, the Docker SDK created, started, observed,
  stopped, and removed one disposable, exactly labeled container on a
  dedicated internal network. The existing inventory adapter observed its
  immutable identity. Archive put/get and a fixed exec command succeeded.
  This was a representative backend lifecycle experiment, not a new broker
  protocol operation or a managed-Kodi provisioning claim.
- Broker stop made typed Web requests fail with `BrokerUnavailableError` while
  `/login` returned HTTP 200; broker restart restored `ping`. Remote-only Web
  startup remained separate from the `lab` profile.

Fixture transfer, snapshot capture/restore, add-on staging, and X11 screenshot
operations retain focused adapter tests. Their complete native managed-Kodi
runtime paths are deferred to the later lab phase; this gate did not create
one of the four managed runtimes. Local development-source deployment remains
disabled until a bounded development root is configured in the later lab
profile; no extra host source mount is introduced here.

## Operation

The remote-only commands in `REMOTE-MODE-CONTAINER.md` continue to work. Once
an initialized private state volume exists, build the matching local images and
start the optional broker with Web:

```sh
docker compose build web broker
docker compose --profile lab up -d --wait --no-build web broker
```

For remote-only operation, use `docker compose up -d --wait --no-build web`.
Do not grant the socket volume to any other service or grant `docker.sock` to
Web/Kodi. The broker healthcheck proves that a live listener returns the
expected denial to its root probe. Broker shutdown unlinks only its verified
socket inode; a correctly owned stale socket is revalidated on restart.

For managed Kodi, first recheck host routes and Docker network IPAM for overlap
with `10.250.0.0/24`. Run the installed `kodi-lab-runtime network-ensure` as a
one-shot broker-container command. It creates only the fixed
`kodi-lab-managed-v1` bridge with the broker-required labels and subnet, or
verifies an existing exact network. A same-name impostor or another Docker
network overlapping the subnet fails closed. It never removes a network. Then
use `compose.lab.yaml` so Web can reach the broker-observed Kodi endpoint:

```sh
docker compose --profile lab run --rm --no-deps \
  --entrypoint kodi-lab-runtime broker network-ensure
docker compose -f compose.yaml -f compose.lab.yaml --profile lab \
  up -d --wait --no-build web broker
```

The overlay attaches Web to the fixed managed network and its ordinary Compose
network; broker stays only on the ordinary network. The managed Kodi container
still has only the fixed managed attachment, no published port, and no host
networking. Web receives neither `docker.sock` nor extra broker authority. The
installed Web image includes a WebSocket protocol dependency so its existing
Kodi 21.3 mediated-display route can negotiate an upgrade. Remote-only Compose
remains independent of the managed network.

Stop both Web and broker before lifecycle backup, upgrade, or restore.
Phase 3.5 keeps snapshot publication at `0:10001`, `0750`/`0440`. The
application rollback set contains Web-owned durable state and leaves snapshots
untouched; clean-volume recovery pairs an off-volume application backup with
the separately copied and checked published snapshot tree. See
`MANAGED-STATE-RECOVERY.md`. The application backup binds published snapshot
hashes as external dependencies. Release-grade export, retention, atomic
cross-artifact capture, and full managed-runtime reconstruction remain release
work.

## Host fallback comparison

| Dimension | Selected broker container | Linux host broker fallback |
| --- | --- | --- |
| Docker authority | Daemon-level via mounted socket | Daemon-level via host socket |
| Web isolation | No Docker socket; private socket volume | No Docker socket; private host socket bind |
| Peer identity | Numeric UID 10001 plus private volume | Host UID plus private filesystem bind |
| Filesystem | Existing state volume, root-owned artifacts | Host-visible state bind and host ownership setup |
| Installation and Compose UX | Same wheel build, optional service | Separate account, systemd unit, volume-to-host sharing |
| Recovery | Compose health/restart; Web fails closed | systemd health/restart; Web fails closed |
| Attack surface | Broker image, shared volume, daemon socket | Host daemon process, host paths, daemon socket |
| Portability | Native Linux proved; Windows/WSL2 Desktop working; new-installer acceptance tracked separately | Linux-only host service; Docker Desktop needs host integration |

The unselected host fallback could use the same `kodi-lab-broker` wheel script,
a dedicated `kodi-lab` account, root systemd service, and a private socket
directory bind-mounted read-only into Web. It needs a host-visible private data
root for trusted state and artifact access. No host service or installer asset
is shipped in this decision. Docker Desktop's Unix peer credentials, UID/GID
mapping, volume semantics, and host daemon socket exposure require their own
validation before support is claimed. The later
[Windows/WSL2 installer](WINDOWS-WSL2-INSTALL.md) records the issue #8 operational
configuration and provides the maintained installation path while retaining these
qualification requirements. It uses the existing container broker, not the host
fallback, and does not expand the Docker/socket authority boundary.
