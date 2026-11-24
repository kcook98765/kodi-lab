# Managed-v2 fresh installation

Use the packaged Web, broker and repository services with empty application
and managed volumes. The installer initializes `0001_foundation` with
`kodi-lab-admin initialize`. Deployments use `compose.release.yaml --profile lab`.
Existing application databases require a fresh installation.

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
Agents acquire exclusive use, admit a package and call the native installer.
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
configuration. Follow the [backup and recovery procedure](LINUX-V1-UPGRADE-ROLLBACK.md).

The broker-owned managed volume holds Kodi's working profile, private snapshots,
workspaces, package approvals and the latest operation. A managed snapshot
captures the stopped Kodi profile. Save named snapshots for everyday environment
switching or restoration. To preserve the whole managed lab, stop Kodi and
quiesce the broker before copying its separate volume. Preserve the matching
reviewed runtime image separately. Keep these artifacts and their credentials
private.
