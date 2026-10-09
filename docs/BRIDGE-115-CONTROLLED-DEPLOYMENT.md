# Readiness boundary and exact candidate

**PREPARATION ONLY. Do not execute activation or rollback in this session.**
The reviewed candidate below was built from commit 684735f before the Add Kodi
instruction correction in this source change. Delivering the correction requires
a clean rebuild from the final committed source, a new reviewed pin/image/
generation set, and an updated activation plan. Never edit BUNDLE provenance or
replace the installed template in an otherwise verified image. Reviewed bundle
pin: `01e605417beaff1cc8ad6e71f770302e812d28cc871ded5b040b3255395f9f65`.

Exact candidate Web image is
`sha256:913aa50929c232aac7238a6b1f441ec35e0bad601e2989fa990761c1c559c105`;
broker is `sha256:58412668398f68a6951ed7c2a8291a7bbf29cd7498d2b7e580092fc255a4d0f9`.
Bridge ZIP SHA256 is
`c894ca88c491edc716997aab3d7a91f2ee30722eff8d7a75b68233ddec9c3bf7`;
fingerprint is `b125a428b12cb48b3a93d2c9009478803b7bd65c85b2fdc632e0b644f2abc4c2`.
Stable generation is `35ef47d3b0a1ff18d19b14192dfaf243d5223802de84513cbe84eb0f8d3f9f28`.
Runtime image IDs differ from archived config IDs on this Docker Engine; the
BUNDLE saved_image_id fields select installed images, not the config digests.

Release qualification and operational conditions are detailed in the
[readiness report](/home/kyle/kodi-lab-deployment/bridge115-readiness-684735f-20261009T035502Z/REPORT.md).
Explicit authorization must name backend migration/deployment AND repository
publication, accepted deviations from RELEASE PASS, the final rebuilt pin, and
external Kodi update scope. Existing pairing row preservation does not approve
fingerprint changes, target reverification, credential changes or new MCP grants.
Pause external consumers for the maintenance window; do not edit grants.

Admission control is concrete: gracefully stop the existing Web FIRST, verify
normal exit0/143 and Web shutdown completion, then stop broker and prove zero live operation
leases. Never continue after forced kill or ambiguous new work. Starting the
new Web on maintenance port18101 keeps the existing18100 upstream unavailable
through validation. This overlay changes no preview port or preview service.
A local user able to reach18101 is an operator and must avoid all mutation calls.
On failure keep ingress closed; record and review pending durable operation
states rather than deleting or completing them by hand.

A predeployment backup must be quiescent, complete and verified. Observation
copies used here are migration inspection evidence only. Export original DB,
master key, encrypted credential stores, repository generations and development
publications, snapshot dependencies, referenced runtime images, old env/bundle,
image overrides and host development source tree to private separate storage.
The existing development source directory must be archived while consumers are
paused; preserve symlink/mode/owner metadata and hash the archive. Backup exports
must include an independent copy of the original manifest and checksum inventory.

No lossy downgrade is an accepted rollback. Retain the exact old administration
image for restore AFTER reversing the repository. Ordinary old Web/broker start
is prohibited until the original0067 DB and matching lifecycle binding are
restored and verified. Publication can update external Kodi independently of the
server: a server rollback cannot undo an already installed1.1.15 package. Keep
those targets outside the update window or separately authorize/co-ordinate
their update/reverification; do not silently rebind their saved fingerprint.

# Controlled activation plan — PREPARATION ONLY

This file is an operator plan, not authorization to execute it. No production
activation, migration, publication, pairing, preview restart, or credential
change was performed in preparation. Final candidate/test identifiers and
readiness are recorded in REPORT.md and evidence. Use the commands below only
after a separately authorized maintenance window and all readiness blockers
have been resolved.

## Required artifacts and preserved baseline

The candidate bundle is `bundle/`, staged from clean commit
`684735fb69ff312bf0cb56a35a6ad54fb4ed0995`, source tree
`ffe6ca5bce59d96396e3ea8f45df3f3ca2525364`. It contains installed Web/broker
images, the existing repository generator/server, Bridge ZIP/provenance, stable
and development input manifests, source-free Compose files, recipes, checksums,
and packaged administration tools. No separate Bridge installer is introduced.

Retain the old bundle at
`/home/kyle/kodi-lab-deployment/candidate-20261004-source-recovery-fd6f99b-hostnet`
and its original `.env`. Its bundle SHA-256 is
`d3e0ebd0bbfe362c7a3b9f434c3700042152cf1a52e70c4806e2b0009434a519`.
The actual running Web and broker use overlays whose images differ from the old
bundle. Preserve `rollback-overlays/live-images.yaml` and the exact archived
live images. The three-image rollback overlay is the authority for restoring
the observed deployment, not the old bundle's default image selection.

Keep these original Docker volumes: `kodi-lab-remote_remote_state`,
`kodi-lab-remote_repository_data`, and `kodi-lab-remote_broker_socket`. Keep
`kodi-lab-managed-v1`, the existing project default network, and
`/home/kyle/kodi-lab-deployment/development-sources`. Credentials and master key
remain inside remote_state (`master-key`, `secrets/external-targets`,
`secrets/external-bridges`, database credentials). Never print their contents.
Snapshot dependency trees and exact runtime images are separate required
rollback assets. Do not remove them or repin/recreate managed Kodi containers.

The production schema baseline is `0067_managed_runtime_reconstruction`;
candidate head is `0071_household_onboarding`. The private raw observation copy
in evidence is for migration inspection only. It is not a quiescent deployment
backup. Preserve both preview services and `/home/kyle/kodi-lab-web-preview`
state throughout; preview URL remains `http://192.168.2.191:8180`.

## Pre-deployment checks (reversible)

Recheck exact live container IDs, immutable image IDs, service start timestamps,
Compose file stacks, repository generation, schema, registry state, secret
inventory metadata, snapshot dependency hashes, and Docker volume names against
`evidence/baseline-*.json`. Any new reviewed-build retirement/conflict requires
review; never overwrite it. Confirm disk space, original image archives, backup
storage permissions and all required qualification results. Verify source and
artifact bytes again. Do not use an unresolved failure as permission to rerun
or bypass an acceptance gate.

```bash
set -euo pipefail
PREP=/home/kyle/kodi-lab-deployment/bridge115-readiness-684735f-20261009T035502Z
NEW="$PREP/bundle"
OLD=/home/kyle/kodi-lab-deployment/candidate-20261004-source-recovery-fd6f99b-hostnet
OLD_PIN=d3e0ebd0bbfe362c7a3b9f434c3700042152cf1a52e70c4806e2b0009434a519
OBSERVED_NEW_PIN=$(python3 -c 'import hashlib,pathlib,sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())' "$NEW/BUNDLE.json")
NEW_PIN=01e605417beaff1cc8ad6e71f770302e812d28cc871ded5b040b3255395f9f65
test "$OBSERVED_NEW_PIN" = "$NEW_PIN"
python3 "$NEW/linux-v1-bundle" verify "$NEW"
python3 "$OLD/linux-v1-bundle" verify "$OLD"
systemctl is-active kodi-lab-web-preview.service kodi-lab-web-preview-proxy.service
curl --fail --silent --show-error --output /dev/null http://192.168.2.191:8180/login
```

Verify both application archive checksums against their accepted manifests.
Candidate images are already installed locally during preparation. If absent
on the activation host, `docker load --input "$NEW/kodi-lab-application-images.tar"`
only after verification; preserve conventional old tags because tags are not
authority. The prepared candidate `.env` must select its immutable images,
same durable repository origin and port, published Web port18100, the existing
development source, and temporary network pool10.252.0.0/20 with child prefix24.
The pool is host-local configuration; recheck routes/Docker networks. Never
update Docker daemon address pools. No dependencies or runtime images are
upgraded as part of this activation.

```bash
newdc() { docker compose --project-name kodi-lab-remote --env-file "$NEW/.env" \
  -f "$NEW/compose.release.yaml" -f "$NEW/compose.lab.yaml" \
  -f "$NEW/compose.development.yaml" -f "$PREP/maintenance-web.yaml" \
  --profile lab "$@"; }
newdc_live() { docker compose --project-name kodi-lab-remote --env-file "$NEW/.env" \
  -f "$NEW/compose.release.yaml" -f "$NEW/compose.lab.yaml" \
  -f "$NEW/compose.development.yaml" --profile lab "$@"; }
olddc() { docker compose --project-name kodi-lab-remote --env-file "$OLD/.env" \
  -f "$OLD/compose.release.yaml" -f "$OLD/compose.lab.yaml" \
  -f "$OLD/compose.development.yaml" -f "$PREP/rollback-overlays/live-images.yaml" \
  --profile lab "$@"; }
newdc config --quiet
olddc config --quiet
```

Never run Compose with a different project name against the production volume
names. Do not include the baseline old-image overlays in new activation commands.

## Maintenance, backup and repository publication order

Begin a separately approved maintenance window. Block new administrative/MCP
operations and wait for admitted work to finish before stopping Web and broker.
Do not create new grants or perform household pairing. Existing managed runtimes
can remain unchanged; external Kodi update policies require special care below.

1. Stop old Web and broker; keep the repository read-only while taking backup.
2. Run the retained actual old Web administration image to create a complete,
   verified quiescent backup. Record its full ID; do not use the observation copy.
3. Export the complete backup plus separately preserved snapshot dependencies,
   runtime images, encrypted secret/key state, old bundle/environment, and live
   override images to a private backup location outside the mutable volumes.
   Verify exported hashes, ownership and modes without showing secret bytes.
4. Stop the repository. All writers/servers remain stopped during transition.
5. Run the candidate's explicit transition with both exact bundle pins and the
   original backup. This switches the complete repository generation while the
   DB remains0067. Do not migrate first or run `repository-init` on the live feed.
6. Verify the candidate feed offline, then run explicit candidate `upgrade`.
   This creates a second, verified backup with the transition binding before
   migration. Record that backup ID and receipt. Ordinary Web startup must
   reject0067 and must never perform migrations.

```bash
olddc stop --timeout 120 web
# Verify graceful Web shutdown/exit before stopping its broker.
python3 "$PREP/scripts/quiescence.py" --shutdown web
olddc stop --timeout 120 broker
python3 "$PREP/scripts/quiescence.py" --shutdown broker
olddc run --rm --no-deps --entrypoint kodi-lab-admin web backup
# Set BACKUP_ID to the exact verified ID printed above.
: "${BACKUP_ID:?record and verify the original quiescent backup first}"
# Choose a private storage location with sufficient space; never print secret files.
BACKUP_EXPORT="$PREP/activation-backup-$BACKUP_ID"
install -d -m 0700 "$BACKUP_EXPORT"
umask 077
olddc run --rm -T --no-deps --entrypoint tar web \
  -C "/var/lib/kodi-lab/backups/$BACKUP_ID" -cf - . \
  > "$BACKUP_EXPORT/application-backup.tar"
tar --extract --file "$BACKUP_EXPORT/application-backup.tar" \
  --directory "$BACKUP_EXPORT" --no-same-owner
# Preserve root-owned broker snapshots and stable/development repository bytes
# using only the actual retained broker image and existing read-only volume mounts.
docker run --rm --network none --read-only --user 0:0 --cap-drop ALL \
  --cap-add DAC_OVERRIDE --security-opt no-new-privileges:true \
  --volume kodi-lab-remote_remote_state:/state:ro \
  --volume kodi-lab-remote_repository_data:/repository:ro \
  --entrypoint tar sha256:abdc31452646115c057cc1a3a0fee961f73ac3b6f8eefbe31b18dce6aa102658 \
  -C / -cf - state/snapshots repository \
  > "$BACKUP_EXPORT/snapshots-and-repository.tar"
tar --create --file "$BACKUP_EXPORT/development-sources.tar" \
  --directory /home/kyle/kodi-lab-deployment development-sources
cp --preserve=mode,timestamps "$OLD/.env" "$BACKUP_EXPORT/old.env"
cp --preserve=mode,timestamps "$PREP/rollback-live-images.tar" "$BACKUP_EXPORT/"
tar --create --file "$BACKUP_EXPORT/previous-bundle.tar" \
  --directory "$(dirname "$OLD")" "$(basename "$OLD")"
python3 - "$BACKUP_EXPORT" "$BACKUP_ID" <<'PY'
import hashlib,json,pathlib,sqlite3,subprocess,sys,tarfile
root=pathlib.Path(sys.argv[1])
metadata=json.loads((root/'manifest.json').read_bytes())
assert metadata['complete'] is True and metadata['id']==sys.argv[2]
assert metadata['revision']=='0067_managed_runtime_reconstruction'
with tarfile.open(root/'application-backup.tar') as t:
    members={m.name.removeprefix('./'):m for m in t.getmembers() if m.isfile()}
    assert set(members)==set(metadata['files'])|{'manifest.json'}
    for name,record in metadata['files'].items():
        m=members[name]
        assert m.size==record['size'] and m.mode==record['mode']
        assert m.uid==record['uid'] and m.gid==record['gid']
        with t.extractfile(m) as f:
            assert hashlib.file_digest(f,'sha256').hexdigest()==record['sha256']
        with (root/name).open('rb') as f:
            assert hashlib.file_digest(f,'sha256').hexdigest()==record['sha256']
    directories={m.name.removeprefix('./').rstrip('/'):m for m in t.getmembers() if m.isdir()}
    directories.pop('.',None)
    directories.pop('',None)
    assert set(directories)==set(metadata['directories'])
    for name,m in directories.items():
        assert m.mode==metadata['directory_modes'][name]
with tarfile.open(root/'snapshots-and-repository.tar') as t:
    members={m.name.rstrip('/'):m for m in t.getmembers()}
    assert any(n.startswith('repository/') for n in t.getnames())
    snapshot_root=members['state/snapshots']
    assert snapshot_root.isdir() and snapshot_root.uid==0
    assert snapshot_root.gid==10001 and snapshot_root.mode==0o750
    for identity,records in metadata['snapshot_dependencies'].items():
        directory=members[f'state/snapshots/{identity}']
        assert directory.isdir() and directory.uid==0
        assert directory.gid==10001 and directory.mode==0o750
        for name,digest in records.items():
            m=members[f'state/snapshots/{identity}/{name}']
            assert m.isfile() and m.uid==0 and m.gid==10001 and m.mode==0o440
            with t.extractfile(m) as f:
                assert hashlib.file_digest(f,'sha256').hexdigest()==digest
db=root/'database/kodi-lab.db'
with sqlite3.connect(db.as_uri()+'?mode=ro&immutable=1',uri=True) as connection:
    assert connection.execute('PRAGMA integrity_check').fetchall()==[('ok',)]
    assert not connection.execute('PRAGMA foreign_key_check').fetchall()
    images=sorted({row[0] for row in connection.execute('SELECT image_id FROM runtime_container_artifacts')})
assert images
for image in images:
    inspected=json.loads(subprocess.check_output(['docker','image','inspect',image]))
    assert inspected[0]['Id']==image
subprocess.run(['docker','image','save','--output',str(root/'runtime-images.tar'),*images],check=True)
(root/'runtime-image-ids.json').write_text(json.dumps(images,indent=2)+'\n')
records={}
for p in root.iterdir():
    if p.is_file():
        with p.open('rb') as f:
            records[p.name]=hashlib.file_digest(f,'sha256').hexdigest()
(root/'export-sha256.json').write_text(json.dumps(records,indent=2)+'\n')
PY
# Set a private separate rollback storage path; do not proceed without it.
: "${BACKUP_STORAGE:?choose separate private backup storage}"
install -d -m 0700 "$BACKUP_STORAGE"
cp --archive "$BACKUP_EXPORT" "$BACKUP_STORAGE/"
python3 - "$BACKUP_STORAGE/$(basename "$BACKUP_EXPORT")" <<'PY'
import hashlib,json,pathlib,sys
root=pathlib.Path(sys.argv[1])
for name,digest in json.loads((root/'export-sha256.json').read_bytes()).items():
    with (root/name).open('rb') as f:
        assert hashlib.file_digest(f,'sha256').hexdigest()==digest
PY
olddc stop repository
newdc --profile repository-transition run --rm --no-deps \
  --volume "$OLD:/previous:ro" repository-transition \
  --previous-bundle /previous --expected-old-bundle "$OLD_PIN" \
  --expected-new-bundle "$NEW_PIN" --backup-id "$BACKUP_ID"
newdc run --rm --no-deps --entrypoint kodi-lab-repository repository verify
newdc run --rm --no-deps --entrypoint kodi-lab-admin web upgrade
```

Repository generation and image selection are reversible with the verified
original backup. After migration, a feed/image reversal alone is unsafe.
Never run an old Web process on0071 or attempt a lossy Alembic downgrade.
Restoration explicitly discards work after the backup and preserves discarded
application state in quarantine. Refuse startup on any pending receipt.

## Exact service start and post-activation verification

Start broker first. Start Web on maintenance loopback port 18101 while the
repository server remains STOPPED; official recognition reads the verified local
repository volume. Verify backend/schema/registry/preservation before publishing
the feed. Start the repository next and verify delivery. Reopen normal Web/MCP
ingress on 18100 only after all checks pass. Compose does not encode this order.

```bash
newdc up -d --wait --no-build --pull never broker
newdc up -d --wait --no-build --pull never web
python3 "$PREP/scripts/verify_live.py" --phase activation --stage backend --port 18101 \
  --reference-db "$BACKUP_EXPORT/database/kodi-lab.db" \
  --reference-secret-metadata "$PREP/evidence/baseline-secret-metadata.json" \
  --reference-backup-manifest "$BACKUP_EXPORT/manifest.json"
newdc up -d --wait --no-build --pull never repository
newdc run --rm --no-deps --entrypoint kodi-lab-repository repository verify
curl --fail --silent --show-error --output /dev/null http://127.0.0.1:18101/login
curl --fail --silent --show-error http://192.168.2.191:18120/stable/addons.xml
```

Keep the maintenance admission block in place until verification completes.
The future verification helper has not been exercised against an activated
deployment: its installed lifecycle validators read live metadata and its
authority comparisons use a consistent read-only SQLite backup to disposable
storage. It does not use separately timed DB/WAL/SHM reads for acceptance.

Use the verification command supplied with preparation (`scripts/verify_live.py`)
to check exact candidate Web/broker/repository image IDs, healthy state, schema
0071, completed transition/upgrade receipts, active official registry identity,
ZIP SHA-256, index checksum, bootstrap and package delivery. `/login` and
Compose health are insufficient: recognition can be `unavailable`, `retired`
or `conflict` while Web serves pages. Explicitly verify the one intended active
1.1.15 registry row and original MCP grants/pairings/policies remain unchanged.
Check wanted secrets and snapshot dependencies through private metadata only.
Do not pair or change target trust merely to make verification pass.

```bash
python3 "$PREP/scripts/verify_live.py" --phase activation \
  --stage published --port 18101 --reference-db "$BACKUP_EXPORT/database/kodi-lab.db" \
  --reference-secret-metadata "$PREP/evidence/baseline-secret-metadata.json" \
  --reference-backup-manifest "$BACKUP_EXPORT/manifest.json"
# Reopen normal Web/MCP ingress only after the backend/feed/preservation checks pass.
newdc_live up -d --wait --no-build --pull never web
python3 "$PREP/scripts/verify_live.py" --phase activation --stage published --port 18100 \
  --reference-db "$BACKUP_EXPORT/database/kodi-lab.db" \
  --reference-secret-metadata "$PREP/evidence/baseline-secret-metadata.json" \
  --reference-backup-manifest "$BACKUP_EXPORT/manifest.json"
systemctl is-active kodi-lab-web-preview.service kodi-lab-web-preview-proxy.service
curl --fail --silent --show-error --output /dev/null http://192.168.2.191:8180/login
```

Rollback shutdown follows the same Web-first drain and all-operation quiescence
checks. If Web was already reopened, a dispatched live-target change cannot be
undone by database restore; preserve ambiguous state and require a separate
recovery review. Never force-kill busy services to make rollback continue.

Failure detection: any unexpected image/schema/pin/generation/receipt, incorrect
ZIP or metadata, missing/retired/conflicting official row, changed authority or
credentials, failed broker/repository health, missing snapshot dependencies,
or preview interruption stops activation. Preserve logs and receipts. A pending
transition is retried only with the identical reviewed arguments; no hand edits.

## Rollback after completed upgrade

The generic bundled new-image restore command is not assumed compatible with
the old1.1.14 feed. The candidate's ordinary repository reader is pinned1.1.15.
Use the exact old administration image after the candidate reverses the feed;
this exception must have passed the disposable rehearsal recorded in evidence.

```bash
newdc stop --timeout 120 web
python3 "$PREP/scripts/quiescence.py" --shutdown web
newdc stop --timeout 120 broker
python3 "$PREP/scripts/quiescence.py" --shutdown broker
newdc stop --timeout 120 repository
newdc --profile repository-transition run --rm --no-deps \
  --volume "$OLD:/previous:ro" repository-transition \
  --previous-bundle /candidate --bundle /previous \
  --expected-old-bundle "$NEW_PIN" --expected-new-bundle "$OLD_PIN" \
  --backup-id "$BACKUP_ID" --rollback
olddc run --rm --no-deps --entrypoint kodi-lab-admin web restore \
  "$BACKUP_ID" --confirm "$BACKUP_ID"
# Restore and verify0067 FIRST; then select the exact baseline images.
olddc up -d --wait --no-build --pull never broker
olddc up -d --wait --no-build --pull never repository
olddc run --rm --no-deps --entrypoint kodi-lab-repository repository verify
olddc up -d --wait --no-build --pull never web
python3 "$PREP/scripts/verify_live.py" --phase rollback \
  --port 18100 --reference-db "$BACKUP_EXPORT/database/kodi-lab.db" \
  --reference-secret-metadata "$PREP/evidence/baseline-secret-metadata.json" \
  --reference-backup-manifest "$BACKUP_EXPORT/manifest.json"
```

For an interrupted migration, keep all services stopped and first restore the
exact intermediate backup printed by `upgrade`/recorded in `.upgrade-pending.json`
using the candidate administration image while its1.1.15 feed remains selected.
That restores0067 plus the completed forward transition binding. Then retry the
same reviewed upgrade or execute reverse transition and original-backup restore
above. If candidate transition itself is pending, use only its documented exact
retry. Never delete volumes, bypass backup verification, edit registry records,
or start mismatched image/schema generations.

## Effect on existing paired and managed Kodi

Official registration inserts reviewed provenance only. It creates no household
pairing, target/policy/consent, management credential, MCP client grant, target
scope, or production mutation authority. Existing developer pairings and all
current target records are preserved by migration and registration.

Normal Kodi repository refresh discovers1.1.15 at the existing repository ID,
bootstrap URL, index and package layout. If a Kodi installation has automatic
add-on updates enabled, publication can install1.1.15 on that target. This is
outside a server-only rollback: restoring the old feed cannot downgrade a
package already installed on Kodi. Before publication, coordinate an update
policy/target maintenance decision separately; this task does not mutate live
Kodi settings. A fingerprint change invalidates the existing pinned Bridge
identity and may require explicit administrator reverification or development
capability amendment. Do not silently rewrite trust or reuse consent.

Managed trust has no automatic migration backfill. Existing native observations,
controls and screenshots retain their authorized native path. Rich resident
Bridge observations require the exact installed/enabled1.1.15 package and an
explicit existing START/readiness lifecycle to establish managed trust. Older
runtime packages retain the conservative native fallback; backend/feed activation
does not update managed runtime packages. Do not recreate or repin live targets.
Official native compatibility labels are19.5,20.5,21.3,22.0,22.0-beta.2. Matrix19.4
managed recipes remain a separate reviewed-runtime mechanism. No compatibility
with other production native labels is implied.

The bundled PRODUCTION-OPERATIONS.md contains historical manual ZIP/manual
registration instructions. This candidate-specific plan supersedes those steps
for the official1.1.15 path: use the existing built-in repository, then automatic
verified registration at Web startup. No new download mechanism is authorized.
