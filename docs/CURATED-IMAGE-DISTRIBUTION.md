# Curated Kodi image distribution and catalog bootstrap

This Linux amd64 foundation covers the exact reviewed Kodi 19.5, 20.5, 21.3,
and 22.0 Beta 2 images. It does not create or start managed Kodi instances.
The current archives are local acceptance artifacts only; none is published.

## Distribution decision

The selected V1 artifact is an exact `docker save` tar made by immutable image
ID, with no tag selected. Its SHA-256 and byte length are in the corresponding
runtime build manifest. The manifest and bounded passing Phase 8 validation
report are small wheel resources. The archive is a separate release artifact,
never wheel content. Current uncompressed archives are 225,385,984,
296,659,968, 333,008,384, and 349,366,784 bytes in matrix order.
The Kodi 19.5 export SHA-256 repeated identically on a second local save;
release publication must retain the exact reviewed files rather than regenerate
them from tags or Dockerfiles.

An immutable registry digest could be a later transport, but no registry
artifact or reviewed digest exists. It would need to preserve or explicitly
translate the reviewed Docker image ID and platform. Rebuilding the Dockerfiles
is not a distribution substitute: timestamped image configuration, package
repositories, and the costly Kodi 22 source build do not promise identical image
bytes. A future hybrid can add digest-pinned registry transport to the same
manifest after separate review. Filenames and tags are never trust inputs.

The identity chain is:

1. The installed Kodi Lab wheel supplies the curated manifest and its bounded
   validation report. Its release authenticity is a separate publication gate.
2. Each manifest records source/build inputs and their hashes, the exact Kodi
   release and linux/amd64 build platform, build result Docker image ID, archive
   SHA-256 and size, immutable catalog UUID/revision, tier, execution profile,
   and validation-report SHA-256. The manifest's own SHA-256 becomes the
   catalog revision's `manifest_digest`.
3. The archive SHA-256 identifies the exact exported tar bytes. It is not a
   registry manifest digest or Docker image ID. Docker 29 on this host reports
   the reviewed ID as an OCI index descriptor digest; the saved tar contains
   that index and other OCI objects. A clean isolated daemon's `docker load`
   imported the same bytes under the OCI config digest instead. Both exact
   IDs are recorded in the manifest as the reviewed index ID and approved
   archive-derived local ID. This is an explicit reviewed translation, not a
   mutable tag alias.
4. Docker image inspection by an approved exact ID must return that ID and
   linux/amd64. The catalog's immutable `RuntimeContainerArtifact.image_id`
   pins the actual local ID for broker provisioning. Human-readable Docker
   tags are ignored. A host already carrying the reviewed index ID keeps it;
   a clean import uses the approved config ID.
5. The catalog's definition UUID, revision UUID/number, and execution profile
   identify product policy. The same image can back an older Interpreter-default
   revision and a newer bytecode-suppression revision without merging them.

## Installed workflow

`kodi-lab-runtime list` reads only the wheel's four packaged manifests. On a
Docker administrator host, `kodi-lab-runtime load NAME ARCHIVE` copies the
archive into a private temporary directory, verifies its size and SHA-256,
loads it, and inspects the exact approved local image ID and platform. Ordinary
`docker image load --input ARCHIVE` followed by `kodi-lab-runtime verify` is an
equivalent transport when the archive digest has already been checked against
the installed manifest. `verify` inspects all four images by ID. A tag with the
right name but wrong bytes cannot pass.

After explicit Kodi Lab state initialization and migration, stop Web and run
`kodi-lab-runtime bootstrap` in the trusted broker container with its existing
Docker socket and state volume. For the current Compose shape, the one-shot
invocation is:

```sh
docker compose --profile lab run --rm --no-deps \
  --cap-add SETGID --cap-add SETUID \
  --entrypoint kodi-lab-runtime broker bootstrap
```

This does not change ordinary Compose startup. It does not build images,
provision containers, create a network, or add a broker RPC. The one-shot
process uses the broker's existing trusted Docker and SQLite access; Web still
has no Docker socket. `bootstrap` validates installed state, verifies all four
local images before one database transaction, and seeds exact definitions,
approved suppression revisions, image artifacts, execution profiles, and the
packaged passing `kodi-runtime-v1` evidence. The approval timestamp records the
explicit bootstrap. Kodi 22 retains its separate Trusted Custom Build
confirmation requirement and the global opt-in; bootstrap does not grant either.
Start Web after bootstrap completes.
The two additional capabilities belong only to this one-shot process: it must
drop from root to the Web state owner after Docker inspection. The long-running
broker retains only CHOWN, DAC_OVERRIDE, and FOWNER. Without SETGID and SETUID,
the one-shot process fails at `setgroups` before catalog mutation.

The transaction compares every existing row with the wheel-owned identity and
accepts only the two explicitly recorded Docker IDs for the same reviewed
archive. If both are present, an existing exact catalog pin is retained.
An exact repeat creates zero rows and preserves its original approval timestamp.
Missing images, wrong platform or ID, inconsistent packaged manifest/evidence,
slug/revision conflicts, altered catalog metadata, or extra/conflicting
validation records fail closed and roll back. A previously revoked revision is
not silently approved again. Existing managed-instance pins and earlier
revisions are not rewritten. Broker exact-image validation remains in force.
State backup/rollback retains the catalog rows, while Docker image bytes remain
outside the state volume. A future clean-volume recovery must reacquire and
verify the exact approved archives before those pins can be provisioned again.

For a public clean Linux amd64 installation, the release process must publish
the exact four reviewed archive files separately from the wheel, with an
authenticated wheel/archive provenance and retention plan. The operator would
install the reviewed Kodi Lab wheel/container, obtain those exact archives,
verify/load all four, run `verify`, initialize state, then run the explicit
broker-container `bootstrap`. `LINUX-V1-INSTALL.md` records the local candidate
bundle and source-free acceptance path. It does not claim public availability,
signing, or cross-version Docker image-ID portability. The clean-host
acceptance used a separate Docker Engine 29.8.1 daemon and imported the
reviewed index IDs. Other engines must produce one of the two exact recorded
IDs or fail closed pending a separately reviewed identity translation.
