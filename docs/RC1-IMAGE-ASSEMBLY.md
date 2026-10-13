# Piers RC1 assembly qualification

This document records the Phase 3 local assembly/acceptance foundation. At that
checkpoint installation admission remained inactive. The cumulative backend and
Development Web integration now use a separately pinned installation descriptor;
see [the installation backend](RC1-INSTALLATION-BACKEND.md). The original
schema-2 acquisition input and its inactive evidence flags remain unchanged:
acquisition/assembly alone never confer installation approval. Production
deployment remains separately authorized.

## Input review

`runtime/recipes/piers-22.0-rc1-r1.json` pins the three upstream OSTree trees and
Kodi source revision. Phase 2 performs real signature/integrity/binding checks.
`assembly_assets/piers-22.0-rc1-r1/ubuntu-support.json` separately pins the
Ubuntu 26.04 base, 97 support DEBs (64,991,044 download bytes), signed InRelease
and uncompressed Packages index hashes, signing fingerprint, keyring hash, base
inventory and final 178-package inventory. All names, versions, architectures,
artifact URLs and SHA256 values are reviewable in that file. The key identity is
Ubuntu Archive Automatic Signing Key 2018:
`F6ECB3762474EDA9D21B7022871920D1991BC93C`.

Review used APT's authenticated metadata followed by independent `gpgv`
verification of the retained InRelease files and SHA256 verification of their
uncompressed Packages indexes and each downloaded DEB. At installation time
fixed wheel-owned hashes authorize exact DEBs; moving indexes are neither
consulted nor used to resolve replacement versions. A removed historical
artifact causes failure, requiring a newly reviewed recipe/closure in a later
phase. No automatic fallback or maintainer-hosted binary archive exists.

## Handoff and assembly

Trusted worker code holds `OstreeAcquirer.acquire(operation_id)` open while
calling `runtime.assembly.assemble_verified_ostree_image`. A live operation lease
binds stage inode, UUID and evidence digest, and permits one assembly consumer.
A serialized verification report or manually constructed `AcquiredOstree` does
not authorize assembly. Stage data are copied using nofollow descriptor-relative
reads; each canonical inventory is recomputed over the bytes copied into the
context. Changes in content, modes, paths, component identity or closure fail.

The gzip tar context uses deterministic names/order/mtime/owners and is limited
to 1 GiB compressed and five minutes of capture. A Linux anonymous memfd seals
writes, growth and shrinkage before Docker receives it. It has no filesystem
checkout/cache path and closes on all ordinary exits. The acquisition context
continues to hold its worker lock and deletes its own staging. New downloads
are always exact pins. Hard termination can leave acquisition stage remnants;
Phase 2 rejects them instead of reusing them.

Support downloads have exact hashes/sizes, a 128 MiB total bound, fixed HTTPS
Ubuntu URLs, no redirects/proxies and a ten-minute deadline. Assembly uses a
pinned base, a fixed asset allowlist and `network_mode=none`. APT only orders the
explicit local DEBs with repositories and Docker networking disabled. APT, its
library, gpgv and the base keyring package are then removed. A final `FROM
scratch` copies the merged support filesystem, omitting archive/cache layers,
then adds each verified payload. Installed inventory and Docker configuration
must match the fixed contract. Build output/evidence are bounded; failures close
the context/stream and remove only an operation-owned final tag, if created.
Docker's normal intermediate build cache can remain; it is not an acquisition
cache and never proves runtime eligibility.

Reproducibility here means exact reviewed inputs, context and installed contents.
Docker history and generated filesystem timestamps are not claimed bit-identical.
The local image ID is recorded for each assembly. No Kodi payload is packaged
in Kodi Lab's wheel or repository.

## Runtime contract

The app resides in `/app`, Freedesktop in `/opt/fdo`, Mesa in `/opt/mesa`. The
launcher uses the explicit Freedesktop dynamic loader/library path and Python
3.14 home/site paths; the Ubuntu supervisor interpreter is kept independent.
The fixed Xrandr wrapper uses the same loader. Trusted assembly creates
`/opt/fdo/lib/x86_64-linux-gnu/addons -> /app/lib/kodi/addons`, rejecting an
unexpected existing destination. Acquired symlinks are never used to create
this correction on the host.

UID/GID 1000, Xvfb :99 at 1280x720x24, llvmpipe, container-local session D-Bus,
internal HTTP JSON-RPC 8080 and `/home/kodi/.kodi` remain fixed. Profile bootstrap
preserves existing settings and rejects unsafe/foreign-owned profile paths.
Shutdown requests Application.Quit, allows six seconds for Kodi and 1.5 seconds
for Xvfb, within an eight-second supervisor budget and the unchanged ten-second
managed Docker stop timeout. Forced termination is failure evidence.

Live tests drop all capabilities, use default seccomp plus no-new-privileges,
and add no host mounts, devices, Docker socket, published ports or host D-Bus.
They use an isolated internal network and independently isolated profiles.

## Acceptance and limitations

The explicit live gate is:

```console
KODI_LAB_TEST_PHASE3_DIRECTORY=/absolute/private/klrc1-phase3-... \
  uv run pytest -q tests/runtime/test_piers_live.py
```

The private experiment directory must contain assembly evidence and the
separately verified native test fixture. The gate records image/input evidence,
identity, actual embedded Python, framebuffer PNGs, navigation changes, library
maps, native activation, security state, two clean primary cycles and another
container's profile isolation. Created resource IDs are recorded before removal.
The Greynetic fixture is pinned separately to commit
`6ef36f452bd36f81824a571e85e6f83e6def01e26893a0322867c616c8e39aa7` and
version 22.0.5; its real signatures, bindings and file hashes are rechecked.
It is injected only into the disposable test container, never the final image or
production provider closure. Loading one actual screensaver does not qualify
all native add-ons.

Stock upstream TagLib 2.3.1 remains exact and unmodified. The Flathub platform
lacks Kodi's Matroska fast-scan patch; upstream warns that Matroska tagging is
degraded to v21 behavior in the
[official RC1 known issues](https://kodi.tv/article/kodi-22-piers-rc-1/).
Basic development-runtime acceptance can disclose this
limitation without a library overlay. It does not claim patched-build media
metadata equivalence. A dedicated Matroska media regression is a separate gate.

This document records the isolated Phase 3 assembly qualification. Subsequent
backend and Web qualifications are documented in
[the installation backend](RC1-INSTALLATION-BACKEND.md) and
[the Development workflow](RC1-WEB-WORKFLOW.md). Assembly evidence alone creates
no approved installation and expands no broker authorization. Production deployment
remains separately authorized.
