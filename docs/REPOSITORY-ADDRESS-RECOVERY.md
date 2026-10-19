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

## Origin migration remains a separate operation

There is currently **no general origin-migration command**. If the old origin
cannot be recovered, retain consistent backups and plan that migration before
changing publication state. Do not delete repository volumes, hand-edit ZIPs,
patch provenance or disable integrity checks to make generation succeed.

A supported migration must atomically account for stable and development feeds,
package/version/cache identities, publication/workspace metadata, installation
receipts, and recoverable rollback. Changing descriptor URLs changes package
bytes even when the old filename says `1.0.0`. That work is tracked in
[issue #9](https://github.com/kcook98765/kodi-lab/issues/9).

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
