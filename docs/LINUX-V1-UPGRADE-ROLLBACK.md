# Linux V1 application upgrade and rollback reference

These commands use the source-free `compose.release.yaml` from an unpacked
bundle. Keep the prior verified application image/archive and the same named
state volumes. Do not change Kodi runtime images or the managed network during
an application-only upgrade. Read `MANAGED-STATE-RECOVERY.md` for snapshot
dependencies and clean-volume recovery.

Stop managed runtimes when a quiescent lab is desired, and always stop Web and
broker before a lifecycle operation. Select and retain the administrator that
can validate the **old** state before stopping services. Set `OLD_ADMIN_IMAGE`
to its verified immutable image ID, not a mutable tag. If application
locks exist, this administrator must contain the lock patch described under
[onboarding lock compatibility](#onboarding-lock-compatibility); an unmodified
historical image is insufficient. Keep the old BUNDLE identity and existing
volumes/configuration for this first backup. The image override below applies
only to the one-shot administrator, not the running services.

Keep the repository service read-only during backup; stop it as well before
restore:

```sh
docker compose -f compose.release.yaml -f compose.lab.yaml --profile lab \
  exec -T web kodi-lab-lab stop
docker compose -f compose.release.yaml -f compose.lab.yaml --profile lab \
  stop web broker
# If the exact reviewed legacy A pointer defect is present, complete the
# separately documented compatibility repair below before this backup.
KODI_LAB_REMOTE_IMAGE="${OLD_ADMIN_IMAGE:?set the verified old-state admin image}" \
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

If the old installation had a local temporary-network pool, carry its pool and
child prefix into the new candidate's `configure` command after checking the
current host routes and Docker networks. The pool lives only in the unbundled
`.env`; candidate transition and rollback never rewrite Docker daemon settings.

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
backup; then restore that backup with the **compatible old-state administrator**
while explicitly checking against the old BUNDLE identity. For the qualified
older Bridge feed below, this is the historical administrator containing the
lock patch, not the new candidate administrator. Setting only the old BUNDLE
environment value does not change the package's reviewed Bridge provenance.
The following image override selects the one-shot restore administrator without
rewriting the new candidate's `.env` or its service images:

```sh
docker compose -f compose.release.yaml -f compose.lab.yaml --profile lab stop web broker repository
docker compose -f compose.release.yaml --profile repository-transition run --rm --no-deps \
  --volume /absolute/path/to/old-candidate:/previous:ro repository-transition \
  --previous-bundle /candidate --bundle /previous \
  --expected-old-bundle NEW_BUNDLE_SHA256 \
  --expected-new-bundle OLD_BUNDLE_SHA256 --backup-id BACKUP_ID --rollback
KODI_LAB_REMOTE_IMAGE="${OLD_ADMIN_IMAGE:?set the verified old-state admin image}" \
  docker compose -f compose.release.yaml run --rm --no-deps \
  -e KODI_LAB_REPOSITORY_BUNDLE_SHA256=OLD_BUNDLE_SHA256 \
  --entrypoint kodi-lab-admin web restore BACKUP_ID --confirm BACKUP_ID
```

After restore, use the retained old candidate, its original `.env` and any
reviewed per-service overlays to verify and start its exact original images.
The compatibility administrator is an administration-only artifact; do not
substitute it for the original Web, broker or repository service images. Do not
remove generations or artifact bytes for rollback.
The same reverse-transition and original A-backup restore apply either before
the new schema migration or after it completes, provided the operator accepts
discarding all work after that A backup. Never invoke a lossy schema downgrade
to roll back a candidate. Migration 0057 deliberately refuses downgrade when
development capability-amendment receipts exist; restoring the old A backup
then explicitly discards those post-upgrade receipts and any other later work.

If the application migration was interrupted, stop all three services and use
only the exact intermediate backup ID printed by `upgrade` (also recorded in
`.upgrade-pending.json`). The confirmation must repeat that ID. Restore this
pre-migration backup first, using the candidate administrator compatible with
that intermediate backup and its still-new repository feed. It retains the
completed repository-transition binding at the old schema revision. Then either
retry the exact B upgrade or
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


## RC1 provenance compatibility boundary

The RC1 integration keeps schema `0071_household_onboarding` and existing stable
APT recipe/attestation formats. Upgrade Web (including its MCP backend), broker
and the broker's same-image installation worker together. Preserve existing
credentials, grants, external pairings, runtime images and profiles. A matching
schema alone does not establish downgrade compatibility.

Older application eligibility code does not enforce the new OSTree provenance
and exact Preview acknowledgement gates. After RC1 approvals exist, do not
blindly select older images against the current database. Quiesce mutations and
archive current state first; stop any new RC1 instances through the compatible
broker. Use either a separately qualified rollback build that retains those
fail-closed guards, or restore a verified, consistent pre-RC1 application/private
state backup with its matching previous service images and repository provenance.
Review and reconcile post-backup credentials, grants, pairings and user changes
before accepting a restore; do not silently discard them. Preserve acquired RC1
images and profiles, quarantined from the older application, for recovery.

Take the backup with services stopped through the supported backup procedure,
retain all referenced images and snapshots, and verify restoration prerequisites.
A live SQLite/WAL observation copy is not a rollback backup. No schema downgrade,
manual approval-row editing or Docker prune is part of this rollback.

## Onboarding lock compatibility

Without PR #21, household onboarding can leave valid private
`locks/household-onboarding/<UUID>.lock` files that cause backup to reject the
otherwise valid state with `unclassified state root: locks`. Explicit upgrade
also needs a verified backup, so switching to an unpatched administrator cannot
work around the failure.

The validator accepts exactly `household-onboarding`, `managed-agent-lifecycle`
and `external-reconnect` beneath `locks`, matching the application lock creators,
before excluding the tree from backup contents.
Directories must be private `0700` and owned by the executing administrator's
UID/GID (normally `10001:10001`). Files must have lowercase UUID-shaped `.lock`
names, be empty regular files with link count one, have mode `0600`, and have the
same private owner/group. Unknown scopes or names, symlinks, hardlinks, unexpected
types, content or unsafe metadata remain errors. Other lock scopes remain rejected.

Restore leaves the validated live lock tree in place instead of archiving,
quarantining or recreating its files. This preserves the live device/inode and
kernel flock relationship, including valid locks created after the backup.
Never delete active lock files, replace their inodes, change their metadata or
skip validation to get past an upgrade check. Existing backup integrity, private
ownership, key/schema/repository binding and snapshot checks remain required.
The lock patch adds no database migration.

For schema `0088_service_enable_start` state created by source `56517d3`, retain
a separately reviewed historical administrator based on that exact installed
source/image with only this scope-validation correction. Use its own immutable
image ID and record both its base and effective installed package hashes and
code diff. It is a patched recovery artifact, not the unchanged original image.
Keep the old BUNDLE identity for old-state backup/restore; do not relabel the
patched administrator as the old release or replace Web, broker or repository
images. The prior household-onboarding checks must remain intact. A newer-schema
administrator is not a substitute for proving old repository provenance.

Before deployment, rehearse supported backup, verification, old-state restore,
candidate repository transition, explicit migration, reverse transition and
original-backup restore on isolated copies. Compare database data, durable
files/metadata and lock device/inode identities, including held and post-backup
locks. Keep both administrators, exact bundles, backup dependency closure,
repository bytes and protected profile/image exports. A copied SQLite observation
is input to this rehearsal; only the supported verified backup is the recovery
artifact. A new production backup and its rehearsal remain required under
separate deployment authorization.

### Administrator roles and the qualified Claw combination

The independent Claw rehearsal on 2026-10-20 used PR head
`e3a1e6865d972dfa797607fa8104c78d8b9ac3f9`, on a securely protected, consistent
copy of application state. It did not deploy or start application services or
managed Kodi, and its runners had neither network nor Docker-control access.

Both old and new packages report `1.0.0rc1`; that string does not establish
compatibility. The tested roles were:

| Role | Exact qualified identity | Schema / reviewed Bridge feed |
| --- | --- | --- |
| Historical bundle administrator base, unpatched | Image `sha256:6a345cbb4387a9c389d2ae20bf90de94f47ea21d1dcc021fc1c25e5395755ebe`; installed package SHA-256 `6c775d2a37c902491d4d55508ab74d952e83dcf5ae18dcad043fcf1657cb0a80` | `0071_household_onboarding` / Bridge `1.1.15` |
| Historical administrator with only the exact lock patch | Local qualification image `sha256:5e92ec98a39279975a1fcd91dde6414fa8a2da6d6cfe9306d5cb30c16c47b30d` | Same historical migrations and Bridge validation as the base |
| PR candidate administrator and repository transition | Image `sha256:8fa19900b544d049b1c48579705130f42d8ff539ba0a840c6c076c42b9643a02` | `0078_workspace_deploy_recovery` / Bridge `1.1.25` |

The historical compatibility image is a **local, patched qualification
artifact**, not an unmodified upstream release or an image available from a
public registry. Its package identity record still names the historical base;
retain the effective image and patch identities as well. The exact code patch
SHA-256 is `12a711ccca686f433009c6a291b3dbd74489cda59a8cfe54b82320287caf9780`;
the resulting lifecycle module SHA-256 is
`24e16a379855c017f3428b3c1ae68ca8ac657c3a6ad64aed8811342fa8ea547a`.
Any replacement compatibility build needs its own review and qualification;
this reference is not an instruction to patch a running installation.

The old BUNDLE SHA-256 was
`9a97020e3c2d10eae77e98fa432daf609400dd62c0ef45ae7ab6c8a94fad6dc2`;
the tested PR BUNDLE SHA-256 was
`3f84296ccd73a30baf3c30ffde92f4a958c745b451e4cbecebbb23f3164880c0`.
A bundle rebuilt after merging or changing documentation has a new identity;
never substitute these example hashes for verification of the actual artifacts.

| Attempt on copied state | Observed result |
| --- | --- |
| Unmodified historical administrator backs up original state | Rejected: `unclassified state root: locks` |
| PR candidate administrator backs up original Bridge 1.1.15 feed | Rejected: `stable repository provenance is unverified`; its reviewed Bridge feed is 1.1.25 |
| Patched historical administrator backs up original state | Passed at schema 0071; archive integrity verified and no lock bytes included |
| PR repository transitions old to new using that original backup; PR administrator upgrades | Passed; repository switched before database migration 0071 to 0078, with its own verified intermediate backup |
| PR repository reverses the transition using the original backup; patched historical administrator restores | Passed; exact original SQLite dump, durable files/metadata, configuration and managed registrations restored |
| Held original lock plus a held lock created after backup | Both device/inode identities survived; competing flock attempts remained blocked after restore |

The successful rollback therefore requires **both** administrators: the new
candidate for the explicit repository transitions and application upgrade, and
the compatible historical administrator containing the lock fix for the first
old-state backup and final old-state restore. Reverse the repository transition
before the final restore. The successor package's old-feed rejection is an
enforced provenance boundary, not permission to regenerate a feed, override
Bridge validation or select an arbitrary older image.

The copied-state cycle does not prove live Web/MCP/broker readiness, other
historical Bridge combinations, a newly rebuilt bundle, or complete release
qualification. The previously reported import-capacity case passed on disk;
the full release suite was not repeated for this qualification.

### Preserve recovery artifacts before cutover

Use [the installation guide](LINUX-V1-INSTALL.md#install-kodi-lab-with-no-kodi-image)
to verify each retained bundle (`SHA256SUMS`, BUNDLE and application archive).
Record the actual per-service image IDs, source commit/tree labels, installed
package digests and migration heads; historical overlays can make the deployed
Web differ from the bundle Web. Record the old/new BUNDLE hashes and reviewed
Bridge versions separately. Preserve the original `.env`, all Compose overlays
and volume names securely. Do not publish credentials or private state.

For image identification/preservation, set the four variables below to the
reviewed immutable IDs in the operator's private deployment record. Run these
archive commands in a private directory with enough disk space; they preserve
application images only and do not duplicate managed Kodi runtime stores:

```sh
umask 077
docker image inspect --format '{{.Id}} {{json .RepoDigests}} {{json .Config.Labels}}' \
  "$OLD_WEB_IMAGE" "$OLD_BROKER_IMAGE" "$OLD_REPOSITORY_IMAGE" "$OLD_ADMIN_IMAGE" \
  > historical-image-record.txt
docker image save --output historical-application-images.tar \
  "$OLD_WEB_IMAGE" "$OLD_BROKER_IMAGE" "$OLD_REPOSITORY_IMAGE" "$OLD_ADMIN_IMAGE"
sha256sum historical-application-images.tar > historical-application-images.tar.sha256
```

Before relying on the archive, verify its checksum, contained image identities
and readability on isolated storage. `docker image load --input
historical-application-images.tar` is the supported load interface, but checksum
and manifest inspection alone do not prove an exported archive has been loaded
and boot-tested. Retain qualification evidence for the exact administrator
that actually completed backup/restore. For Claw, the exported compatibility
archive's image/blob hashes were verified and the original image performed the
cycle; reloading that export was not tested.

Keep the complete, verified original pre-transition backup, its manifest and
explicit backup ID; keep the separately identified intermediate upgrade backup
and receipts as well. Preserve the exact master key/encrypted secret binding
and referenced snapshot dependencies with private modes and numeric ownership.
A live SQLite observation archive is qualification input, not a production
rollback point. Create and verify a fresh quiescent lifecycle backup before a
separately authorized production cutover, and review any post-backup work that
a restore would discard.

Stop without improvising if any required historical image, package/module
identity, bundle, backup or snapshot dependency is missing or mismatched; if
lock validation or any other ownership/type/content check fails; if free storage
is insufficient; or if receipt/transition state is ambiguous. An interrupted
operation permits only its documented, identical receipt-bound retry. If that
cannot be established, keep services stopped and retain evidence. Do not start
old Web on the new schema, perform a schema downgrade, edit SQLite/provenance
receipts, reinitialize volumes, delete locks, or substitute a different backup
or image to force recovery. Unexpected state or migration requirements require
operator review before proceeding.

### Qualification resource retention

Inspect helper behavior before running a retention-only qualification. The
bundle finalizer in `scripts/linux-v1-bundle` makes four successful metadata
probes with `docker run --rm --network none`: Web and broker package versions,
the Web installed Alembic head, and broker installed recipe digests. `--rm`
automatically removes each probe after exit. Their validated outputs are bound
into `BUNDLE.json` and staging evidence, but the stopped probe identities,
inspection records and container logs are not retained. No pre-existing Docker
resource needs removal for these probes.

The Claw rehearsal retained five explicitly named, stopped qualification
containers, including its unsuccessful historical compatibility attempt; their
identities, labels, configured users and mount ownership are in the private
qualification report. The four helper probes were the retention exception:
semantic package/head/recipe evidence survived, but per-probe container evidence
was lost. Do not claim complete resource retention or reconstruct missing IDs
from an incomplete Docker event history.

For capacity-sensitive qualification, use disk-backed scratch with an explicit
pytest `--basetemp` on a suitable private disk filesystem. Check the installed
check-helper implementation rather than assuming an environment override is
supported: this PR's `scripts/validation_tiers.py` selects `/dev/shm` when at
least 1 GiB is free, while workspace import retains a 2 GiB free-space reserve.
Do not weaken the application reserve or infer a release pass from a focused
repeat of the previously failing case.

The current finalizer has no keep-container option. Do not invoke it unchanged
when automatic removal is forbidden. First arrange a separately reviewed
retention-compatible qualification procedure with explicit names/labels and
saved inspection records, or stop that staging step. For one-shot Compose
examples in this guide, retention-only procedures must omit `--rm`, add a unique
`--name`, and retain the resulting container for review. This does not authorize
starting, restarting or deleting previously retained containers or cleaning up
production resources.
