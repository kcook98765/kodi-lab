# Managed-v2 fresh installation

This development branch uses the packaged Web, broker and repository services.
Use a fresh release Compose project and empty application and managed volumes.
The schema revision is `0001_managed_v2`. Existing application databases
are refused. Omega 21.3 is the current managed runtime. Its reviewed binary-package
recipe validates a local image and records the resulting immutable image ID.

Use the installed `kodi-lab-admin initialize` command before starting Web.
Configure the repository with its reviewed release bundle and choose a
deployment address using the normal installer. Start the packaged broker,
Web and repository, then sign in. Under **Kodi Runtimes**, install reviewed
Omega. When the installer reports **Ready**, open **Managed Kodi**, prepare
the working profile and start Kodi. Use this page for viewing, Take Control,
Stop, Reset and snapshots. During preparation the profile is bound to the
reviewed image. Ordinary container replacement does not change that binding.

Under **Agents**, issue credentials with the Trusted Managed Lab role for
managed development. Ordinary managed work uses **acquire**, **start**,
**package import** and **native install**. Direct deploy can update a stopped
profile without reinstalling the package. Explicitly release the lease when
work is finished. External registrations and grants are administered
independently under **External Kodi**.

## State and recovery

The application backup covers SQLite and the Web volume's private records.
Repository publication files live in their own volume and are covered by the
configured repository backup process. Preserve the matching credentials and
repository inventory when recovering that data.

The separate broker-owned managed volume contains the working profile, named
snapshot archives, package approval store, private workspaces and latest
operation. A snapshot captures a validated stopped Kodi profile. Database and workspace backups are separate. To retain the entire managed lab, copy
the separate managed volume with Kodi stopped and the broker quiescent. Keep
the volume private and retain the exact Docker image separately. Application
backup and profile snapshots do not contain Docker image layers. Use the managed UI or
MCP to save and load named snapshots for everyday work. Do not place private
profiles, archives or the credential store in a repository publication.
