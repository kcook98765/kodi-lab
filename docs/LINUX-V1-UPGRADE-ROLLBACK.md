# Linux V1 application upgrade and rollback reference

These commands use the source-free `compose.release.yaml` from an unpacked
bundle. Keep the prior verified application image/archive and the same named
state volumes. Do not change Kodi runtime images or the managed network during
an application-only upgrade. Read `MANAGED-STATE-RECOVERY.md` for snapshot
dependencies and clean-volume recovery.

Stop managed runtimes when a quiescent lab is desired, and always stop Web and
broker before a lifecycle operation. Keep the repository service read-only
during backup; stop it as well before restore:

```sh
docker compose -f compose.release.yaml -f compose.lab.yaml --profile lab \
  exec -T web kodi-lab-lab stop
docker compose -f compose.release.yaml -f compose.lab.yaml --profile lab \
  stop web broker
# If the exact reviewed legacy A pointer defect is present, complete the
# separately documented compatibility repair below before this backup.
docker compose -f compose.release.yaml run --rm --no-deps \
  --entrypoint kodi-lab-admin web backup
```

### Reviewed legacy A development pointer before backup

The candidate-transition implementation preceding D-0390 could create
`development-data/CURRENT` as root even though its generation and ACK were
correctly Web-owned. The ordinary backup must continue to reject that inode.
Only an installation still serving A BUNDLE
`e68e66d271e8a36873f830526c53c1f253ef48afdda260e589a5e5eaaff41709`
with its matching completed transition evidence is eligible for the packaged
compatibility repair. First verify both retained candidate bundles, configure
the successor candidate with the same BASE URL and named volumes, and stop A
Web/broker as above. Keep the read-only repository service running. From the
**successor candidate** directory, before creating the A backup, run:

```sh
docker compose -f compose.release.yaml --profile repository-legacy-pointer-repair \
  run --rm --no-deps \
  --volume /absolute/path/to/old-A-candidate:/previous:ro \
  repository-legacy-pointer-repair \
  --previous-bundle /previous \
  --expected-old-bundle e68e66d271e8a36873f830526c53c1f253ef48afdda260e589a5e5eaaff41709
```

The one-shot profile has no network, Docker socket, broker socket or ports.
It verifies the exact A revision/provenance/active generation and every other
development entry before changing only the fixed `CURRENT` inode owner from
`0:0` to `10001:10001`. It does not change bytes, mode, pointers, ACK, database
or candidate identity. The root-owned administration receipt distinguishes
pending and completed attempts; rerun only the identical command after an
interruption. A different or additional safety failure is a stop, not
permission to chown by hand. The ordinary A `kodi-lab-admin backup` above must
then complete and be independently verified before stopping the repository or
starting a candidate transition. This compatibility command is absent from the
retained A package and earlier immutable candidate B; use only a separately
reviewed successor package that contains it.

## Repository candidate transition

When upgrading an installation that already serves a repository, stop its
repository service after the backup. Verify both retained candidate bundles.
From the **new candidate** directory, configure its `.env` with the same durable
BASE URL and named volumes, then run the explicit transition with the complete
backup ID from the command above:

The old candidate's reviewed migration identity must equal the live database
revision and the complete backup revision. The new candidate's BUNDLE records
its installed Alembic head. The transition accepts the same head or a reviewed,
unbranched ancestor-to-descendant path. It records both revisions and switches
repository provenance while the database deliberately remains at the old
revision. Do not migrate the database before this transition.

```sh
python3 linux-v1-bundle verify .
python3 /absolute/path/to/old-candidate/linux-v1-bundle verify /absolute/path/to/old-candidate
docker compose -f compose.release.yaml --profile repository-transition run --rm --no-deps \
  --volume /absolute/path/to/old-candidate:/previous:ro repository-transition \
  --previous-bundle /previous --expected-old-bundle OLD_BUNDLE_SHA256 \
  --expected-new-bundle NEW_BUNDLE_SHA256 --backup-id BACKUP_ID
docker compose -f compose.release.yaml run --rm --no-deps \
  --entrypoint kodi-lab-repository repository verify
```

Use the exact 64-character hashes from each verified `BUNDLE.json`. The
transition service has no network, Docker socket or broker socket. It mounts
only the two candidates read-only and the existing application/repository
volumes. Web and repository serving refuse an unfinished transition. If the
command is interrupted, rerun the **identical** command; do not edit the volume.
The old and new candidate identities are never inferred from directory names.
The transition job runs with narrow root authority, but writes development
generations and pointers as the verified application state owner. Do not change
ownership manually if its metadata check fails. The following application
upgrade independently checks development repository ownership and modes while
creating its backup; a failure is an upgrade stop, not permission to bypass the
backup check.
After a completed transition, keep Web and broker stopped. The repository
service is read-only and may safely serve its verified new generation, but the
operator workflow keeps it stopped until the application upgrade is complete.
New Web startup rejects the old database revision.

Verify and load the new application image artifact and set the reviewed image
references in `.env`. Before starting Web, run the explicit upgrade command
from the new installed image:

```sh
docker compose -f compose.release.yaml run --rm --no-deps \
  --entrypoint kodi-lab-admin web upgrade
docker compose -f compose.release.yaml -f compose.lab.yaml --profile lab \
  up -d --wait --no-build web broker repository
```

`upgrade` verifies the old state, creates and verifies its own pre-upgrade
backup when a migration is needed, migrates to the new installed head, and
records a receipt linked to the exact repository transition operation. The
intermediate backup retains that binding. Ordinary Web startup never migrates
and requires the new database head plus matching completion evidence. Verify
the final database revision, stable and development BUNDLE provenance, absence
of pending transition/upgrade files, login, catalog,
MCP clients, exact runtime pins, and wanted secrets after startup. The
application backup records managed snapshot hashes as dependencies; it does
not contain Docker image bytes or the snapshot tree. When a Phase 2 development
feed exists, the backup also contains its verified artifact/generation bytes
and binds them to the SQLite publication records and stable provenance.

For a repository-candidate rollback, stop Web, broker and repository. From the
new candidate, reverse the feed transition using the original pre-transition
backup; then restore that backup with the new admin command while explicitly
checking against the old BUNDLE identity:

```sh
docker compose -f compose.release.yaml -f compose.lab.yaml --profile lab stop web broker repository
docker compose -f compose.release.yaml --profile repository-transition run --rm --no-deps \
  --volume /absolute/path/to/old-candidate:/previous:ro repository-transition \
  --previous-bundle /candidate --bundle /previous \
  --expected-old-bundle NEW_BUNDLE_SHA256 \
  --expected-new-bundle OLD_BUNDLE_SHA256 --backup-id BACKUP_ID --rollback
docker compose -f compose.release.yaml run --rm --no-deps \
  -e KODI_LAB_REPOSITORY_BUNDLE_SHA256=OLD_BUNDLE_SHA256 \
  --entrypoint kodi-lab-admin web restore BACKUP_ID --confirm BACKUP_ID
```

After restore, use the retained old candidate and its original `.env` to verify
and start its images. Do not remove generations or artifact bytes for rollback.
The same reverse-transition and original A-backup restore apply either before
the new schema migration or after it completes, provided the operator accepts
discarding all work after that A backup. Never invoke a lossy schema downgrade
to roll back a candidate. Migration 0057 deliberately refuses downgrade when
development capability-amendment receipts exist; restoring the old A backup
then explicitly discards those post-upgrade receipts and any other later work.

If the application migration was interrupted, stop all three services and use
only the exact intermediate backup ID printed by `upgrade` (also recorded in
`.upgrade-pending.json`). The confirmation must repeat that ID. Restore this
pre-migration backup first. It retains the completed repository-transition
binding at the old schema revision. Then either retry the exact B upgrade or
follow the reverse-transition procedure above with the original A backup.
Do not start Web while the intermediate revision remains below B's head.

```sh
docker compose -f compose.release.yaml -f compose.lab.yaml --profile lab \
  stop web broker repository
docker compose -f compose.release.yaml run --rm --no-deps \
  --entrypoint kodi-lab-admin web restore BACKUP_ID --confirm BACKUP_ID
```

For a schema-only upgrade without a repository candidate transition, restore
its verified pre-upgrade backup and then select the retained old application
images before restarting. A repository candidate transition always requires
the reverse transition plus original A backup described above.

Restore verifies the backup, key binding, schema revision, repository binding,
development artifact hashes and snapshot dependencies before replacing state.
It quarantines newer application state
and refuses ambiguous or mismatched recovery. A clean-volume recovery has a
separate `recover-clean` path and requires the matching backup plus separately
preserved snapshot artifacts. Never use a volume deletion or direct SQLite
change as a rollback method.
