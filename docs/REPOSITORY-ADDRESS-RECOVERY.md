# Repository address diagnosis and recovery

The repository has three different addresses to consider:

| Setting | Purpose |
| --- | --- |
| Listener bind / Windows forwarding | The local interface and port accepting connections |
| Advertised base URL | The origin configured with `--repository-base-url` |
| Generated origin | The origin embedded in repository ZIPs, indexes and package URLs |

A listener can move while the advertised/generated origin stays unchanged.
Downloading a bootstrap ZIP from the new listener does not rewrite its embedded
feed URLs. Internal container health is not a check of those URLs from Kodi.

## Prevent DHCP-related changes

For a new installation, prefer a durable LAN DNS hostname controlled by this
installation, or reserve the host's private IP address in DHCP. Keep the listener
bound to the selected private interface. A hostname in `--repository-base-url`
must resolve correctly from **every Kodi device**, including Android devices;
resolution on the Lab host alone is insufficient.

For example, bind to the host's reserved private IPv4 address but advertise a
site-owned name such as `http://kodi-lab.home.arpa:18120`. Configure that name in
your LAN DNS first. A hostname is not created by the installer. HTTPS/proxy
origins must reach the actual repository. They need not resolve directly to a
local interface, so Lab does not guess a replacement origin from DNS or the
incoming request's Host header.

If the hostname remains unchanged after DHCP moves the host, update DNS and the
deliberate listener/Windows forwarding/firewall configuration. The generated
origin and installed Kodi repository URLs can remain unchanged. Check Windows,
WSL, Docker and Kodi reachability separately; see
[Windows networking](WINDOWS-WSL2-INSTALL.md#repository-lan-access-and-external-targets).

## Diagnose an existing installation

1. Open **Settings → Kodi Repository**. It shows the advertised origin and warns
   when a verified generated origin differs. A literal IP receives a DHCP
   reservation reminder. Stable/Home and optional Development ZIPs are separate.
2. Inspect the configured bundle environment and any Windows forwarding rules.
   Preserve the installed bundle, project name, candidate hashes and state.
3. Download the stable bootstrap ZIP and inspect its descriptor locally:

   ```sh
   unzip -p repository.kodi-lab-1.0.0.zip repository.kodi-lab/addon.xml
   ```

   Compare `info`, `checksum` and `datadir` origins to the advertised address.
   Inspect `repository.kodi-lab-dev/addon.xml` separately if Development is used.
4. From the Kodi device's network, verify the exact embedded feed and checksum
   URLs. Then refresh the Kodi repository and check Bridge availability. HTTP 200
   for `/install/` alone is not sufficient.

The Web diagnostic verifies stored package bytes and the configured candidate
pin before reporting their origin. A mismatch remains **unverified for serving**;
the diagnostic does not bypass the existing origin/candidate guards. A matching
origin is not proof of DNS, routing, firewall or device reachability.

## Immediate recovery without changing repository identity

Restore reachability of the existing generated origin: restore its reserved
address, correct its existing DNS record, or restore the explicitly configured
forwarding/proxy endpoint. Coordinate address ownership first; do not assign an
address now used by another device. Restore the previously matching advertised
configuration if it drifted, then use ordinary verification/startup commands.
Preserve private-interface binding and the installation's Compose project.

Editing `.env`, restarting containers or ordinary candidate transition does not
rewrite generated repository URLs or repository add-ons already installed in
Kodi. Re-running `generate` with a different origin is deliberately rejected.

## Explicit origin migration

Use a reviewed Lab release containing `kodi-lab-repository migrate-origin`.
Finish or reconcile pending repository deployments first. Stop Web and broker,
create and verify a complete application/repository backup using the existing
[backup procedure](LINUX-V1-UPGRADE-ROLLBACK.md), then stop repository serving.
Keep the same Compose project, volumes, candidate and Bridge trust. Migration
requires the current installed database head; complete a candidate/application
upgrade separately before moving its origin.

Choose the old origin exactly as stored, the new credential-free origin, one
unused numeric repository version newer than **both** current repository add-ons,
and a fresh UUID. The example moves initial `1.0.0` repositories to `1.0.1`.
Use a higher version if that identity has already been published. A failed
preparation can leave verified immutable packages; never overwrite them with
different bytes under the same version.

The command patches the explicitly mounted site environment and, for scripted
installations, its `INSTALLATION.json`. It retains all other settings and receipt
fields. Configuration files must be ordinary, single-link files with no group or
other write access; protect these files before migration. Mount their containing
directory, since safe replacement renames an adjacent temporary file. Do not
mount a single `.env` inode. For scripted installations, `SITE_ROOT` below is the
installation directory and the environment is `/site/bundle/.env`. For manual
bundles mount the bundle directory and use `/site/.env`; omit the installation
receipt argument only when that installation has no such receipt.

From the installed bundle directory, keep your normal explicit project and
Windows overlay arguments on every Compose command. With services already
stopped and `SITE_ROOT` set to the absolute existing installation directory:

```sh
# Generate this UUID once; retain it with the exact arguments for recovery.
python3 -c 'import uuid; print(uuid.uuid4())'
docker compose -f compose.release.yaml --profile repository-transition \
  run --rm --no-deps --cap-add CHOWN \
  --entrypoint kodi-lab-repository \
  --volume "$SITE_ROOT:/site" repository-transition migrate-origin \
  --old-base-url http://192.168.50.10:18120 \
  --base-url http://kodi-lab.home.arpa:18120 \
  --repository-version 1.0.1 --operation-id OPERATION_UUID \
  --backup-id VERIFIED_BACKUP_ID \
  --site-environment /site/bundle/.env \
  --installation-receipt /site/INSTALLATION.json \
  --repository-bind 192.168.50.11 --confirm-services-stopped
```

`--repository-bind` is optional: supply it only for a deliberate listener change.
It still requires a private IPv4 interface. Windows/WSL2 forwarding, DNS, proxy
and firewall changes remain explicit host administration; the migration never
infers or changes them. The one-shot job has no network or Docker/broker socket.
The additional `CHOWN` capability preserves the original configuration inode
owners during replacement. Its only extra writable mount is the selected site
configuration directory. Never point it at an unrelated installation.

The operation verifies the old feed, bundle, backup and database, builds both
new immutable generations, then journals progress before selecting them. It
updates the development repository's origin, bootstrap hashes and acknowledged
index generation together. The newer Development index includes its own
repository update, as Stable already does. Subsequent publication and candidate
transition preserve the selected repository versions. Old generations and ZIPs
remain available for verification. Existing publication/deployment receipts and
external reachability confirmations remain historical: an external target needs
a fresh confirmation of the new repository version and bytes. Workspaces,
publications and Bridge credentials are preserved.

On interruption, keep services stopped and rerun the **identical** command,
including its UUID, old/new origins, version, backup and mount paths. It checks
saved hashes and resumes only those recorded transitions; contradictory bytes
or administrator edits stop recovery. Serving and Web startup refuse the pending
`ORIGIN-MIGRATION.json`. Do not remove the journal, copy pointers, edit ZIPs or
reinitialize volumes. Completion receipts are retained under
`origin-migrations/OPERATION_UUID.json` in repository storage.

After completion, re-read the updated environment, verify repository provenance
with `kodi-lab-repository verify`, confirm Compose configuration, then start the
normal services. Complete the listener/DNS/Windows forwarding checks and inspect
both advertised and embedded URLs from a Kodi device. The page and install
listing now show the actual versioned stable and optional development ZIPs.

To reverse a published move, first back up the now-current origin and repeat
this migration with old/new origins reversed, a fresh UUID and a **higher**
repository version, for example `1.0.2`. Restore listener/DNS/forwarding explicitly.
This keeps client update ordering and unrelated application state intact. A
pointer downgrade or ordinary database restore alone is not an origin rollback;
old backups bind their original repository generations and configuration.

After a coherently generated successor feed is available, clients whose old
literal feed is unreachable need explicit recovery: install/update the official
repository add-on from the new source, or temporarily restore the old endpoint
to deliver its reviewed update. Check both the add-on descriptor and any saved
Kodi File Manager source. Follow normal version/cache rules; do not change
Kodi's XML files by hand as the general installation procedure. Verify repository
refresh and Bridge availability afterward. Unchanged Bridge identity and
credentials do not require re-pairing solely because the repository moved.

See [backup/rollback](LINUX-V1-UPGRADE-ROLLBACK.md) and
[managed state recovery](MANAGED-STATE-RECOVERY.md) before any publication change.
