# Linux amd64 four-version managed lab

The `lab` profile runs the installed Web/API/MCP application and its restricted
broker. Kodi Lab and the broker create and own the Kodi containers. Compose has
no long-running Kodi services. Use an isolated Compose project and state volume;
the protected legacy compatibility lab is a separate installation.
For source-free Linux V1 candidate installation, use `LINUX-V1-INSTALL.md`.

## Reviewed matrix and identity

| Selector | Kodi release | Tier | Curated revision | Image identity |
| --- | --- | --- | --- | --- |
| `19` | 19.5 stable | Legacy | `51950000-0000-4000-8000-000000000005` | `sha256:73c221f1fc1fc14cbfdfd82e62fd4b87752ef71e0521227cb6790451fa51d2bd` |
| `20` | 20.5 stable | Current | `52000000-0000-4000-8000-000000000005` | `sha256:be35d7cb8899dfe474c6a1eca62da99c4ada1280f4325933d7b6193e83537638` |
| `21` | 21.3 stable | Current | `3a000000-0000-4000-8000-000000000006` | `sha256:70e0166fd79ea731c3f81056c2f0516f8fe5c9c8ec729b0c0f1c3e6bac66f254` |
| `22` | 22.0 Beta 2 | Preview | `52200000-0000-4000-8000-000000000005` | `sha256:4fb611666a2637904d6edd0cf90aa8cfc12c8d89edaa7a7fa60c61ef26655918` |

The wheel-owned manifests also authorize one exact archive-derived loaded image
ID per release. `kodi-lab-runtime verify` accepts only those recorded IDs and
linux/amd64. The artifact saved in the catalog is the ID actually present on
the host. Tags do not select a runtime.

## Observed capability matrix

The following results came from one isolated linux/amd64 Compose project using
the four exact reviewed images. A passing cell names the operation actually
exercised; it does not imply broader add-on or library compatibility.

| Capability | Kodi 19.5 | Kodi 20.5 | Kodi 21.3 | Kodi 22 Beta 2 |
| --- | --- | --- | --- | --- |
| Broker provision, start, stop, restart, deprovision | Start/stop/restart passed | Start/stop/restart passed | Start/stop/restart passed; prior deep deprovision proof | Start/stop/restart/deprovision/reprovision passed |
| Bounded readiness and JSON-RPC identity | 19.5 stable passed | 20.5 stable passed | 21.3 stable passed | 22.0 beta ordinal 2 passed |
| Navigation and input | Home and Up passed | Home and Up passed | Home and Up passed | Home and Up passed |
| Library query | One movie, one show passed | One movie, one show passed | One movie, one show passed | One movie, one show passed |
| Packaged fixture and reset | `small-mixed-v4` passed | `small-mixed-v3` passed | `small-mixed-v2` passed | `small-mixed-v5` passed |
| Snapshot create and restore | Passed | Passed | Passed | Passed, including restore after deprovision |
| Passive add-on ZIP install | Passed | Passed | Passed | Passed |
| Managed screenshot | 1280×720 PNG passed | 1280×720 PNG passed | 1280×720 PNG passed | 1280×720 PNG passed |
| Mediated live display | Unsupported by the exact-revision gate | Unsupported by the exact-revision gate | Authenticated PNG frame passed | Unsupported by the exact-revision gate |
| Runtime/profile | UID/GID 1000, Xvfb, bytecode `suppress` | UID/GID 1000, Xvfb, bytecode `suppress` | UID/GID 1000, Xvfb, bytecode `suppress` | UID/GID 1000, Xvfb, bytecode `suppress` |
| Tier and trust | Legacy inclusion | Current | Current | Preview acknowledgement, invocation opt-in, exact revision confirmation |

The four fixture databases and disposable container layers are independent.
Resetting Kodi 19 while the other three ran left their readiness and movie/show
counts unchanged. Each snapshot appeared only in its source instance's Web
history. Compose control-plane recreation and `compose down`/`up` retained the
managed containers, records, exact image IDs, and snapshots. One idle Docker
stats sample with all four running measured approximately 119, 126, 134, and
146 MiB respectively (about 524 MiB total); CPU percentages were 5–7% each.
These are observations, not resource guarantees.

Canonical managed Target and ManagedInstance UUIDs are UUIDv5 values from the
fixed product namespace in `kodi_lab.runtime.lab`, with names
`managed-kodi-19`, `managed-kodi-20`, `managed-kodi-21`, and `managed-kodi-22`.
Each target has a fixed human name and exact release. Bootstrap verifies all
existing canonical rows before filling missing rows. It accepts a target-only
partial result from an interrupted invocation and completes it on rerun. It
does not rewrite a conflicting target, instance type, revision pin, or name.
Fixture assignment and stopped/running observations can change normally.

## Development-checkout first run

Check host routes and Docker network IPAM for overlap with `10.250.0.0/24`.
Obtain and verify/load the four reviewed archives as described in
`CURATED-IMAGE-DISTRIBUTION.md`. The archive publication and signing procedure
is not part of this development slice. From the repository, build the installed
Web and broker images and initialize a fresh private Compose state volume:

```sh
docker compose --profile lab build web broker
docker compose run --rm --no-deps --entrypoint kodi-lab-admin web initialize
docker compose run --rm --no-deps --entrypoint kodi-lab-admin web set-password
docker compose --profile lab run --rm --no-deps \
  --cap-add SETGID --cap-add SETUID \
  -e KODI_LAB_TRUSTED_CUSTOM_BUILDS_ENABLED=true \
  --entrypoint kodi-lab-lab broker bootstrap --ack-preview-kodi22
docker compose -f compose.yaml -f compose.lab.yaml --profile lab \
  up -d --wait --no-build web broker
```

The one-shot bootstrap checks initialized state, verifies the four images,
ensures the exact product managed network, validates/locks state after dropping
to the Web UID, verifies or seeds the curated catalog, confirms only the
reviewed Kodi 22 Beta 2 revision, and creates the four stopped canonical pins.
The explicit Preview flag and invocation-scoped Trusted Custom Build opt-in are
both required. Normal Web settings remain unchanged. Preview confirmation is
stored only for that one exact revision; arbitrary custom builds receive no
trust. A repeat requires stopping Web first because Web holds the state
lifecycle lock. Rerun the same bootstrap command, then start Web again.

The managed network is external to Compose. Bootstrap must precede the overlay
`up` on a fresh host. The broker owns no network mutation RPC, and Web never
receives `docker.sock`.

## Daily operation

With Web and broker healthy, run the installed command inside Web:

```sh
docker compose -f compose.yaml -f compose.lab.yaml --profile lab \
  exec -T web kodi-lab-lab status
docker compose -f compose.yaml -f compose.lab.yaml --profile lab \
  exec -T web kodi-lab-lab start
docker compose -f compose.yaml -f compose.lab.yaml --profile lab \
  exec -T web kodi-lab-lab stop
```

`start`, `stop`, and `status` accept any subset of `19 20 21 22`, for example
`start 19 22`. Empty selection means all four. `start` provisions an absent
container through the broker, starts it, and checks bounded exact-version
readiness; a stopped container is restarted without reprovisioning. `stop`
leaves the container, image, records, history, and snapshots in place. `status`
checks live readiness for running instances. Each command reports every chosen
runtime independently and exits unsuccessfully if any selected runtime fails.
An ambiguous lifecycle result requires the existing reconciliation flow before
retrying that instance.

The four containers have independent disposable writable layers and Kodi
libraries/profiles. They have no persistent per-instance Docker volumes. A
normal container stop/start retains the layer; deprovision removes it. Snapshot
artifacts live in the private application state volume and are separate from
the disposable writable layer. Preserve or restore them through the existing
Web operations and recovery contract.

`docker compose down` removes the Web/broker control plane but does not issue
broker-managed Kodi stop or deprovision operations. Stop the managed runtimes
explicitly first if that is desired. Recreating Web and broker with the same
volumes and fixed managed network preserves database records and broker-owned
containers. If a managed runtime was left running, the new control plane
reobserves its exact owned container; freshness-dependent Web state such as a
readiness task is re-established by normal observation or a new lifecycle
operation.

## Reset and cleanup

`stop` retains each container's writable layer. Reset one runtime through the
existing authenticated Web reset operation after assigning a compatible
packaged fixture with `kodi-lab-lab fixture assign SELECTOR SLUG` in the Web
container; it destroys and reprovisions that runtime's disposable layer.
To reset all four, request four independent per-instance resets and inspect
each result. Reset is not a Compose operation. Snapshot restore is separate
from fixture reset. Deprovision one stopped runtime through its existing Web
lifecycle operation; its managed record and snapshot history remain. To destroy
an isolated disposable lab, deprovision each managed container, stop the
control plane, and remove only that project's disposable volumes and network.
Export the verified application backup and published snapshot tree first if
they must survive. Removing the state volume loses its database, snapshots,
and other private artifacts; it does not remove independently managed Docker
containers. Never use volume removal as a substitute for deprovision.

Mediated live display remains limited to exact Kodi 21.3. All four reviewed
images use Xvfb and the managed screenshot path. The Kodi 22 fixture uses a
configuration-only `MyVideos149.db` seed from the exact reviewed Beta 2 image;
its packaged audit rejects unexpected library state. Bytecode suppression is
fixed by each immutable execution profile; this phase verified those profiles
and installed a passive ZIP on each release, but did not repeat the earlier
bytecode absence proof. No noVNC, external physical target onboarding, Kodi
bridge, or legacy lab component is added here. Local clean-host installation is
covered by `LINUX-V1-INSTALL.md`; public archive publication remains later work.
