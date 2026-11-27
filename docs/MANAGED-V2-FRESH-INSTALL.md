# Managed-v2 fresh installation

Use the packaged Web, broker and repository services with empty application
and managed volumes. The installer initializes the installed schema head with
`kodi-lab-admin initialize`. Deployments use `compose.release.yaml --profile lab`.
The in-place update below applies to compatible managed-v2 sites. Migration
from the retired pre-alpha architecture is unsupported.

Configure the approved repository bundle and deployment address with the
[normal installer](HOST-INSTALLER.md). After starting the services, sign in
and use **Kodi Runtimes** to acquire a reviewed runtime. Piers RC2 remains a
preview and requires exact recipe approval. Wait for **Ready**, then open
**Managed Kodi** to choose the version and prepare its working profile.
The broker prepares the authenticated Narrow Bridge. Use the managed page
for status, noVNC viewing, Take Control, Stop, Reset and named snapshots.
Prepare leaves the selected working profile stopped. Start checks native Kodi
and authenticated Bridge readiness. There is one independent working profile
per runtime version and one running managed instance per site. Stop preserves
the profile. Reset replaces it with a clean baseline. Stop the current instance
before switching versions. Use **Release Control** when the agent should
resume native input.

Under **Agents**, issue credentials and explicitly enable Trusted Managed Lab.
Agents use [bounded ZIP admission](MCP-TOOLS-AND-PERMISSIONS.md#managed-package-transfer), acquire exclusive use and call the native installer.
For ordinary edits, stop Kodi, deploy from the private workspace, then start
and execute the changed add-on. Release exclusive use when finished.
Administer external registrations and grants independently under **External Kodi**.

## Watch managed Kodi during development

Select **View** to watch the running Kodi. The passive viewer requests a
2 FPS update rate and has no viewer-specific duration limit. The Web sign-in still expires after 12 hours
without Web activity or seven days from login. Agent tool calls use their own
credentials. When the Web session expires, sign in and select **View** again.

**Take Control** temporarily excludes agent input for up to ten minutes.
**Release Control** closes the interactive worker and restores any still-valid
agent claim after cleanup. The agent can inspect Kodi while its previous claim
remains valid. If native input cleanup cannot be verified after a failure,
stop Kodi to clear the input fence. A stopped or replaced Kodi ends the stream.
Select **View** again to watch the next start.

## Managed Kodi audio

Managed Kodi plays audio into a silent PulseAudio sink. The broker starts it
in a network-disabled worker for the active Kodi container and removes it
on Stop. The endpoint uses a private Unix socket and needs no host audio
configuration. It keeps playback paced while discarding audio samples.
Piers routes its existing ALSA default through the PulseAudio plugin shipped
in its reviewed image.

The audio worker comes from the installed broker image. After a compatible
application update, Start uses the endpoint with the existing reviewed Kodi
image, working profile and snapshot bindings. A profile Reset or runtime
reacquisition is unnecessary.

## Managed Kodi network access

On **Managed Kodi**, select **Allow network access** and save to let Matrix,
Nexus, Omega and Piers connect to the LAN and internet. The setting is off
by default. It is global to this Lab site and remains saved across restarts
and upgrades. The saved policy and the selected runtime's current policy
are shown separately. Stop Kodi normally and start it again to apply a
change. A profile Reset is unnecessary.

When allowed, Kodi and its add-ons use normal outbound Docker bridge routing
and Docker's DNS forwarding. Local names such as `home.arpa` depend on the
Docker host's resolver. This setting allows outbound access to reachable host,
LAN and internet services and reduces network isolation. It does not publish
additional inbound ports or change management authentication, Kodi container
privileges or profile mounts. Access still depends on the host's Docker and
network configuration. This is ordinary unicast networking; LAN multicast
device discovery may differ from a standalone Kodi installation. The separate
disposable public-repository test keeps its restricted network policy.

A repository published on the Docker host's LAN address can have different
reachability from a repository on another LAN host. A request from managed
Kodi enters the host on a Docker bridge. It may be filtered before Docker
forwards the published port, even when the URL works from the host and from
physical LAN clients. A successful DNS lookup confirms the address only.
If the numeric address also times out, check the explicit listener binding,
Docker port mapping and host ingress policy. A host-local health check tests
a different packet path. See [Docker's packet filtering guidance](https://docs.docker.com/engine/network/packet-filtering-firewalls/).

For ordinary managed development, use the broker-owned private feed and the
[reviewed dependency catalog](#approve-dependencies-for-native-installs).
To qualify a repository meant for physical devices, use a separately
authorized LAN Kodi client or a repository origin on a host already
reachable from the disposable managed profile. Keep the managed runtime on
its assigned bridge and use the existing repository publication policy.

To restore isolation, clear the setting, save, then Stop and Start any
affected runtime. Each managed bridge is reconciled at the next Start for
that version and its profile, named snapshots, approved packages and
workspaces remain available. Before downgrading to a build that only
recognizes internal managed bridges, start and stop each previously enabled
generation once with access off. Verify that the ordinary internal bridge is
in use and retain the usual application and managed-state recovery inputs.

## Reviewed runtimes

| Managed runtime | Qualification and limits |
| --- | --- |
| Matrix 19.5 | Legacy Linux amd64 development runtime. Official Team Kodi Jammy binaries with embedded FFmpeg that has known vulnerabilities and receives no Ubuntu FFmpeg updates. Use reviewed add-ons and trusted media. |
| Nexus 20.5 | Ubuntu Noble distribution packages. Reviewed Linux development runtime with native screenshots. |
| Omega 21.3 | Ubuntu Resolute distribution packages. Reviewed Linux development runtime with native screenshots. Earlier Windows/WSL 2 managed native acceptance used Omega. |
| Piers 22.0 RC2 | Team Kodi Flatpak from Flathub Beta. Development preview. Native screenshots are unavailable on the reviewed X11/GLES runtime. Live noVNC and ordinary managed navigation work. |

Matrix native screenshots are supported. Matrix 19.5 Windows/WSL 2 qualification
is pending. [Matrix acquisition and qualification](MATRIX-19.5-CANDIDATE.md)

The **Kodi Runtimes** cards link to the actual binary distribution. Expand
**Runtime Details** for the package version or commit, available upstream Git
revision, acquisition checksum and recorded image ID. Before installation the
card shows the reviewed acquisition inputs. External Kodi reports its own native
version and identity. Its binary provenance is shown as unknown. Use reviewed add-ons and controlled
media across all four runtimes. Managed isolation is designed for development.
It is unsuitable for running arbitrary malicious add-ons or untrusted media.
Compatibility with one Kodi generation does not establish compatibility with
another.

## Update a managed-v2 site

Use this path for a compatible packaged managed-v2 site at a supported
schema revision. Use a reviewed successor with its exact source,
bundle and image identities. Keep the existing Engine, Compose project,
volumes, configuration, runtime pins and repository origin. Retain the
old verified bundle and application image IDs before staging the successor.

1. Check host and Docker capacity for both bundle generations, images and
   backup. Keep the working profiles and named snapshots. Stop managed Kodi
   and release ownership.
2. Verify the successor bundle. Quiesce Web and broker and take the packaged
   application backup against the current repository configuration. Reopen
   the backup and verify its manifest, database and repository data before
   applying the transition. Preserve separately any managed state needed
   for recovery.
3. Stop the old repository writer. Run the successor's receipt-bound
   `repository-transition` using the verified backup and exact previous
   and successor bundle identities. Verify the repository, then run the
   successor's `kodi-lab-admin upgrade` before starting Web, broker and
   repository on the existing volumes.
4. Check exact installed image identities, Web login, broker health,
   repository health and authenticated MCP. Confirm managed profiles,
   snapshots and authorization survived. Keep the verified recovery inputs.

### Replace a retained Matrix 19.4 slot

A normal application update or 19.5 acquisition leaves the prepared 19.4 profile
bound to its original image. The Matrix tab shows the version of that retained
working profile. **Reset uses the pinned image** and is for profile replacement
within the same runtime.

Schedule the Matrix cutover with the administrator. Stop Matrix and confirm
its Kodi container has exited, ownership is released and no managed
operation is pending. With the broker quiesced, stop any private feed helper
whose label and name match the retired Matrix target. Record the 19.4 reviewed image pin, profile pointer and
identity. Verify a private archive of the stopped Matrix directory, its Docker
image, and the administrator or agent snapshots bound to that image. Shared
snapshot directories live at the managed root and under agent directories.

Acquire and validate 19.5 and read back the exact reviewed image handoff.
With the broker quiesced after a graceful Stop, move the existing
`matrix/` directory out of the active managed root on the same filesystem
to a private recovery location. Keep the volume and image referenced by
the old directory. Start the broker and use ordinary **Prepare** to create
a fresh Matrix profile, then **Start** to verify the selected image. The old
snapshots retain their existing IDs, checksum and original image binding.

For rollback, stop and quiesce the new slot, preserve its directory, restore
the original Matrix directory at its original path and restore the exact 19.4
reviewed image pin. Restart the broker and verify the original profile/image
identity and snapshot compatibility before further development. Keep both
versions' recovery assets privately. Start only the restored active slot.

## State and recovery

Stop Web and quiesce repository writes before running the packaged
`kodi-lab-admin backup` command. The application backup contains SQLite, the
application key and private external records and workspaces. With the repository
configured, the backup also contains the matching external development
publication data. Keep the backup private. To recover, stage that verified
backup in `backups/ID` on the empty application volume and run
`kodi-lab-admin recover-clean ID --confirm ID` with the matching repository
configuration. Use a compatible application version and the backup's
application key. If the chosen backup contains
`.repository-candidate-transition.json`, also stage the verified pre-transition
backup identified by its `backup_id` under `backups/<that-id>` in the destination.
Repeat for each predecessor that contains a transition receipt. Clean recovery
verifies the chain before restoring state so the selected bundle and a later
supported rollback can both start. Keep those dependencies with each recovery
set that references them. Verify the recovered site on the
separate empty volume before retiring any original volume. SQLite includes terminal installer UUIDs recorded
by the time of backup, so they remain protected from replay on a fresh broker
socket volume.

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

### Native install dialogs after package application

For a first install in Estuary, Kodi may open an add-on's own OK dialog
before the installer returns to its normal completion view. The broker can
complete the managed install receipt after checking the selected package and
all required dependencies against the approved bytes, enabled states and
private repository origin. A verified reply may include
`interaction.state=dialog_observed` and the native window ID. Inspect the
current Kodi screen and handle only the known first-run prompt. This reports
package state. Configuration and functional readiness are separate checks.
If exact verification fails, the install remains pending for normal
administrator recovery. Avoid replaying the installation to clear a dialog.

### Direct deployment with generated skin includes

The broker records the member paths and digests of the last verified native
install or successful direct deployment for the working profile. Direct
deployment checks those installed source bytes first. A source path omitted
from the next workspace is removed. Snapshot save and load carry this
provenance alongside the profile. Older snapshots still load. If an older
profile has no record, supply the exact digest of an admitted baseline ZIP
as `baseline` on `managed_workspace_deploy`. The broker checks its ID,
version, installed bytes and private repository origin before using it.

With Kodi stopped, run the usual `managed_workspace_deploy`. For an eligible
runtime-generated skin include, the reply reports `state=review_required`
and lists each exact path, digest, length and mode. Review those identities
against the output you expect from the add-on's generator. For instance,
compare Skin Shortcuts' generated include to its output on the selected
stopped profile. The operator can inspect the file on that profile through
the local broker container. Once the output is reviewed, pass the returned
`review` digest on the same deploy call with the same workspace and optional
baseline. The broker rechecks the bytes and copies the approved include
directly from the stopped profile. The source workspace stays independent.

This lane accepts regular, non-executable skin XML rooted at `includes`.
XML can control skin behavior, so the broker always asks for exact review
of such additions. Unexpected executables, unsafe entries, content drift
and collisions with reviewed source paths refuse before the profile is
changed. A later workspace or file change requires a fresh review.

## Public repository release testing

For the optional disposable workflow, see [approved public repository testing](MANAGED-PUBLIC-REPOSITORY.md).
