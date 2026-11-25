# Managed-v2 fresh installation

Use the packaged Web, broker and repository services with empty application
and managed volumes. The installer initializes `0001_foundation` with
`kodi-lab-admin initialize`. Deployments use `compose.release.yaml --profile lab`.
Migration from previous pre-alpha installations is unsupported.

Configure the approved repository bundle and deployment address with the
[normal installer](HOST-INSTALLER.md). After starting the services, sign in
and use **Kodi Runtimes** to acquire a reviewed runtime. Piers RC2 remains a
preview and requires exact recipe approval. Wait for **Ready**, then open
**Managed Kodi** to choose the version and prepare its working profile.
The broker prepares the authenticated Narrow Bridge. Use the managed page
for status, noVNC viewing, Take Control, Stop, Reset and named snapshots.
Ordinary Stop and Start preserve the selected version's profile. Reset creates
a clean profile. Stop the current instance before switching versions.

Under **Agents**, issue credentials and explicitly enable Trusted Managed Lab.
Agents use [bounded ZIP admission](MCP-TOOLS-AND-PERMISSIONS.md#managed-package-transfer), acquire exclusive use and call the native installer.
For ordinary edits, stop Kodi, deploy from the private workspace, then start
and execute the changed add-on. Release exclusive use when finished.
Administer external registrations and grants independently under **External Kodi**.

## State and recovery

Stop Web and quiesce repository writes before running the packaged
`kodi-lab-admin backup` command. The application backup contains SQLite, the
application key and private external records and workspaces. With the repository
configured, the backup also contains the matching external development
publication data. Keep the backup private. To recover, stage that verified
backup in `backups/ID` on the empty application volume and run
`kodi-lab-admin recover-clean ID --confirm ID` with the matching repository
configuration. Use the same `0001_foundation` schema and application key. Verify the recovered
site on the separate empty volume before retiring any original volume.

The broker-owned managed volume holds Kodi's working profile, private snapshots,
workspaces, package approvals and the latest operation. A managed snapshot
captures the stopped Kodi profile. Save named snapshots for everyday environment
switching or restoration. To preserve the whole managed lab, stop Kodi and
quiesce the broker before copying its separate volume. Preserve the matching
reviewed runtime image separately. Keep these artifacts and their credentials
private.

An unclean exit remains visible as `exited_unconfirmed`. For an identified
exited container, explicitly choose Reset or load a compatible snapshot to
replace the working profile. Both actions discard the old working profile,
so preserve unsaved data before proceeding. Snapshot Load validates the
snapshot before removing the exited container. A pending operation may still
require an administrator to use Recover first.

## Approve dependencies for native installs

Managed Kodi uses an isolated network and a private package feed. A host
operator can approve a reusable set of exact dependency ZIPs from Kodi's
official repository or another source the owner has reviewed. Select archives
compatible with the Kodi versions that will use them and verify their identity
before approval. Keep the reviewed archives together on the host so the same
set can be approved again on another lab. The catalog contains one approved
version per add-on ID and is shared across the managed versions.

Mount the reviewed ZIP directory read-only in a one-shot broker command.
Use the site's configured Compose command with its usual project and env file,
and pass only the exact reviewed filenames:

```console
docker compose -f /path/to/compose.release.yaml --profile lab run --rm --no-deps --pull never \
  --user 1000:1000 --volume /path/to/reviewed-zips:/reviewed:ro \
  --entrypoint kodi-lab-managed-catalog broker \
  /reviewed/script.module.example-1.0.0.zip /reviewed/script.module.other-2.0.0.zip
```

The command validates each archive using the existing intake rules, rejects
repository add-ons and prints the approved IDs, versions and SHA-256 digests.
A batch can contain up to 64 ZIPs totalling 128 MiB. Reapproving the same bytes
is harmless. Changing an existing version requires `--replace-versions`.
Different bytes for the same approved version are refused. Retain named
snapshots before testing incompatible version changes.

Once the catalog is provisioned, a trusted agent admits the add-on under
development and submits one native install request. The broker exposes the
approved archives through the private feed. Kodi selects the dependencies the
add-on needs, and the broker verifies newly installed dependencies against the
approved archives and repository origin. If an approved archive is missing,
provide the add-on ID and version to the operator. Workspace deployment of
ordinary source edits needs no dependency approval or native reinstall.
