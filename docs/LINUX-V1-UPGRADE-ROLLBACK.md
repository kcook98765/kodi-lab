# Linux V1 application upgrade and rollback reference

These commands use the source-free `compose.release.yaml` from an unpacked
bundle. Keep the prior verified application image/archive and the same named
state volumes. Do not change Kodi runtime images or the managed network during
an application-only upgrade. Read `MANAGED-STATE-RECOVERY.md` for snapshot
dependencies and clean-volume recovery.

Stop managed runtimes when a quiescent lab is desired, and always stop Web and
broker before a lifecycle operation:

```sh
docker compose -f compose.release.yaml -f compose.lab.yaml --profile lab \
  exec -T web kodi-lab-lab stop
docker compose -f compose.release.yaml -f compose.lab.yaml --profile lab \
  stop web broker
docker compose -f compose.release.yaml run --rm --no-deps \
  --entrypoint kodi-lab-admin web backup
```

Verify and load the new application image artifact and set the reviewed image
references in `.env`. Before starting Web, run the explicit upgrade command
from the new installed image:

```sh
docker compose -f compose.release.yaml run --rm --no-deps \
  --entrypoint kodi-lab-admin web upgrade
docker compose -f compose.release.yaml -f compose.lab.yaml --profile lab \
  up -d --wait --no-build web broker
```

`upgrade` verifies the old state, creates and verifies its own pre-upgrade
backup when a migration is needed, migrates to the new installed head, and
records a receipt. Ordinary Web startup never migrates. Verify login, catalog,
MCP clients, exact runtime pins, and wanted secrets after startup. The
application backup records managed snapshot hashes as dependencies; it does
not contain Docker image bytes or the snapshot tree.

For a schema-changing rollback, stop Web and broker and use only the exact
backup ID printed by `upgrade` (also recorded in the state receipt). The
confirmation must repeat that ID:

```sh
docker compose -f compose.release.yaml -f compose.lab.yaml --profile lab \
  stop web broker
docker compose -f compose.release.yaml run --rm --no-deps \
  --entrypoint kodi-lab-admin web restore BACKUP_ID --confirm BACKUP_ID
# Set .env back to the retained prior reviewed application images.
docker compose -f compose.release.yaml -f compose.lab.yaml --profile lab \
  up -d --wait --no-build web broker
```

Restore verifies the backup, key binding, schema revision, and snapshot
dependencies before replacing application state. It quarantines newer state
and refuses ambiguous or mismatched recovery. A clean-volume recovery has a
separate `recover-clean` path and requires the matching backup plus separately
preserved snapshot artifacts. Never use a volume deletion or direct SQLite
change as a rollback method.
