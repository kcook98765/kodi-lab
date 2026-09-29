# Phase 3.5 managed-state ownership and recovery contract

This is the Linux Compose development contract. The state volume remains
`remote_state` at `/var/lib/kodi-lab`; the broker socket has its separate
`broker_socket` volume. Web and its administrative lifecycle commands run as
numeric `10001:10001`. The broker runs as `0:0` and resolves the fixed Web
group as numeric 10001. Docker volume contents, rather than container IDs or
container names, carry state across recreation.

## State ownership map

`W` means Web/admin UID:GID `10001:10001`, `B` means published broker artifact
UID:GID `0:10001`, `P` means broker process UID:GID `0:0`, and `D` means
Docker daemon ownership outside the data root.
`R` is the application-version rollback set; `C` is the installation recovery
set; `T` is temporary; `X` is reproducible or cache data. `S` marks private or
authority-bearing content. `V` means it must survive replacement of the Web
or broker container. A dash means that property does not apply. Directory and
file modes below are exact where shown; source-tree files retain their reviewed
per-file safe modes.

| Data-root path or external state | Creator; reader; writer | Owner and mode | Class | Sensitive | Survive container replacement | Application rollback |
| --- | --- | --- | --- | --- | --- | --- |
| `database/kodi-lab.db` | admin migration; Web and broker read; Web/admin write | W, parent `0700`, file `0600` | R,C | S | V | Yes, via SQLite backup API |
| SQLite `-wal`, `-shm`, `-journal` | SQLite; Web/admin | W, SQLite private modes | T | S | No independent value | Fold committed transactions into backup DB |
| `master-key` | admin initialize; Web/admin read; admin creates | W, `0600` | R,C | S | V | Yes, exact key bytes |
| `secrets/external-targets/**` | Web; Web reads/writes, broker does not need to write | W, directories `0700`, files `0600` | R,C | S | V | Yes, bound to DB and key |
| `secrets/external-bridges/**` | Web; Web reads/writes | W, directories `0700`, files `0600` | R,C | S | V | Yes, bound to DB and key |
| Login, session, MCP, grants, approvals, external and managed target records | Web; Web/broker trusted DB readers; Web writes | In SQLite, W `0600` | R,C | S | V | Yes |
| `.state.json` | admin; Web/admin reads; admin writes | W `0600` | R,C | S | V | Yes, key digest binding |
| `.upgrade-pending.json`, `.restore-pending.json`, `.recovery-pending.json`, `.last-upgrade.json` | admin; admin/Web reads; admin writes | W `0600` | management | S | V until resolved | Receipt and fail-closed markers stay outside the copied set |
| `.lifecycle.lock` | Web/admin; Web/admin | W `0600` | T | S | No independent value | No |
| `backups/<id>/**` | admin; admin reads/writes | W, private directories; original safe file modes recorded | management,C input | S | V | No recursive backup |
| `quarantine/<id>/**` | admin; admin reads/writes | W, original safe modes | management | S | V until operator resolves | No |
| `snapshots/<uuid>/{manifest.json,state.tar}` | broker; broker and Web read; broker writes/deletes | B, root and UUID directories `0750`, files `0440` | C | S | V | Bytes no; hashes bound as dependencies |
| Preserved snapshots | broker artifact as above; Web metadata | B bytes plus W SQLite rows | C | S | V | Rows yes; bytes no |
| `snapshots/.staging/**` | broker; broker | B, private stage `0700` | T | S | No | No |
| `addon-zips/artifacts/**` | Web ZIP validator; Web and broker read; Web manages | W, private parents `0700`, published directories `0500`, files `0400` | R,C | S | V | Yes, with exact modes |
| `addon-zips/staging/**` | Web upload; Web | W `0700`/`0600` | T | S | No | No |
| `addon-zips/{install,replacement}-staging/**` | broker; broker | P, directories `0700`, files `0600` | T | S | No | No |
| `agent-workspaces/**` including baselines and edits | Web; Web and broker read; Web writes | W, private parents; files may be `0600`, `0400`, `0644`, or `0755` inside private root | R,C | S | V | Yes; abandoned `.staging-*` omitted |
| `source-promotions/**` including preimage backup and journal | Web; Web reads/writes | W, directories `0700`, immutable preimages `0400` | R,C | S | V | Yes; abandoned `.staging-*` omitted |
| `development-source-staging/**` | broker; broker | P, directories `0700`, transient files `0600` | T | S | No | No |
| `external-deployment-*` root stages | Web; Web | W private temporary directory | T | S | No | No |
| `screenshots/staging/**` raw captures | Web prepares, broker writes and chowns to W, Web reads/deletes | W `0700`/`0600` | T | S | No | No |
| `screenshots/instances/**` latest PNG cache | Web; Web | W `0700`/`0600` | X | S | No; recapture | No |
| Packaged fixture definitions/media | wheel; Web/broker read | image-owned, outside data root | X | No | Reinstall same reviewed wheel | No |
| Managed runtime/revision, broker lifecycle metadata and receipts | Web; Web/broker read; Web writes | In SQLite, W `0600` | R,C | S | V | Yes |
| Docker images, managed writable layers and runtime volumes | Docker; broker mediates | D, outside data root | external | Potentially | Separate Docker lifecycle | No; future lab recovery must define runtime reconstruction |
| Broker socket and process state | broker; Web client | socket volume `0:10001`, directory `0750`, socket `0660` | T | S | Recreated | No |
| `run/`, cache/build output, package assets | runtime or wheel; runtime readers | private or image-owned | T,X | Variable | Recreated | No |

There is no current durable data-root `instances/`, `artifacts/`, `logs/`, or
`runtime-builds/` implementation. An unclassified top-level path fails backup
instead of being silently omitted. A future durable tree requires an explicit
classification before it can be covered by this contract.

## Two recovery boundaries

**Application version rollback** uses `kodi-lab-admin backup`, `upgrade`, and
receipt-bound `restore`. Its set is SQLite, the exact master key and encrypted
secret trees, `.state.json`, accepted add-on ZIP artifacts, agent workspaces,
and source promotion evidence. The backup excludes published managed snapshots,
screenshots, broker stages, SQLite sidecars, backup/quarantine management, and
Docker runtime state. The manifest records hashes, sizes, numeric UID/GID and
safe modes for every copied file, and exact directory modes. The SQLite backup
API makes the database consistent. The backup records hashes of each published
snapshot as an external dependency without copying snapshot bytes. Verification
rejects unexpected ownership, mode, symlink, hard link, inventory, path,
revision, key, or content. Restore first checks that the published snapshot
set still has exactly those hashes; a later-created, removed, or changed
snapshot blocks rollback before application mutation. The application rollback
then restores only its set, leaving root-owned snapshots and their ownership
unchanged. The pre-upgrade backup is not a whole-lab point-in-time backup.

**Installation/lab disaster recovery** needs an off-volume copy of a verified
application backup and every published snapshot UUID subtree from the same
quiescent state. Preserve numeric identities and private modes. Do not copy
`.staging` or screenshot caches. The application backup and snapshot tree are
separate recovery artifacts; their existing hashes and the read-only
`published_snapshot_inventory` check provide local integrity and ownership
proof. The application manifest binds the exact published snapshot hashes;
`recover-clean` rejects any mismatch before writing. A release-grade export
format, atomic cross-artifact capture, authenticity, retention, offsite
transport, and reconstruction of future
managed Docker writable state remain unresolved. A same-volume `backups/`
directory alone cannot survive volume destruction.

## Clean-volume procedure and current limits

Stop Web and broker. Copy the verified `backups/<id>` directory and published
snapshot UUID directories to trusted storage outside the original volume,
preserving numeric UID/GID and modes. Record independent hashes for those
copies. On a genuinely fresh volume, stage the application backup at
`backups/<id>` owned by `10001:10001` under private directories, and restore
published snapshots to `snapshots/<uuid>` with fixed `0:10001` ownership and
`0750`/`0440` modes. The snapshot root is `0:10001` mode `0750`. Do not stage
an incomplete snapshot or `.staging` directory. Check the recorded hashes and
run `published_snapshot_inventory` before application recovery. This copy of
broker-owned bytes is a stopped, trusted Docker-administrator operation; the
Web UID restores only the application-owned set. Then run:

```sh
docker compose run --rm --no-deps --entrypoint kodi-lab-admin web recover-clean BACKUP_ID --confirm BACKUP_ID
docker compose --profile lab up -d --wait --no-build web broker
```

`recover-clean` accepts only a truly empty application state (apart from the
staged backup, optional published snapshots, and lifecycle lock). It verifies
the backup before writing, then writes a recovery-pending marker, copies the
exact application set, checks inventory/hashes/modes/key/revision, and clears
the marker last. Interrupted recovery blocks Web; start again with a fresh
volume rather than normalizing partial state. The matching application wheel
must understand the recovered Alembic revision. Web and broker must be started
only after both artifact sets and ownership checks complete.

The accepted single volume avoids a data migration and keeps broker access to
SQLite, ZIPs, workspaces, and snapshots. Splitting out snapshots today would
add a mount and adoption procedure without reducing the broker's effective
Docker-daemon authority. The socket volume remains separate. Remote-only
operation never requires a broker or a snapshots subtree. A future split may be
reconsidered with the full managed lab storage design; it must migrate the
Phase 1/2/3 single volume without abandoning existing snapshot UUIDs.

The Phase 3.5 isolated native-Linux proof used one synthetic encrypted
credential, durable login/MCP records, and one actual published snapshot. It
verified an off-volume hash/UID/GID/mode copy, destroyed the original test
volume, staged both sets on a genuinely new volume, rejected a tampered
snapshot hash before recovery, recovered the application set, and started and
recreated both installed-wheel services. HTTP login, scoped MCP, broker ping,
the encrypted fixture, snapshot validator, and ownership checks passed. No
managed Kodi runtime or physical target was involved.

## Stable and development Kodi repository volume

`repository_data` is separate from private application state. The stable feed
contains reviewed, reconstructible bridge/bootstrap bytes and per-install
provenance. Keep the source-free candidate, BUNDLE digest, configured base URL,
and stable `PROVENANCE.json` in the installation recovery record. A changed
origin cannot reuse the 1.0.0 repository identity with different bytes.

Phase 2 backups taken with Web stopped include a verified copy of
`repository_data/development-data` alongside the online SQLite snapshot in the
same private backup directory. The manifest binds the exact stable generation
identity; backup verification cross-checks the copied development index,
package hashes, pending imports, publication UUIDs, and SQLite feed state.
Unknown or tampered bytes abort backup. The static repository service can stay
running during backup because it has a read-only mount; stop it for restore.

For clean recovery, first load the *same* candidate and configure the *same*
base URL. On a fresh repository volume run `repository-init` and verify the
stable generation against the retained provenance. With Web and repository
stopped, stage the exact private backup under `backups/ID` of an empty
application state volume and run `kodi-lab-admin recover-clean ID --confirm ID`
with both volumes mounted. Recovery verifies the stable identity, restores the
development subtree and SQLite snapshot, compares their hashes/UUIDs, and only
then clears the application recovery marker. If it is interrupted, keep Web
and repository stopped and repeat from fresh volumes. The development feed
never serves an unacknowledged generation; stable metadata remains independent.
