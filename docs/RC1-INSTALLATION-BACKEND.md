# Kodi 22 RC1 reviewed installation backend

The reviewed backend integrates a fixed `piers` selector into the existing reviewed installer.
The selector means only `22.0-rc.1`; it does not follow the current beta branch.
The published Matrix/Nexus/Omega recipe list remains unchanged. Development-mode
Web actions use this backend; production deployment is separately authorized. No RC1 catalog or installation record is created by importing code.

The schema-3 `piers-22.0-rc1-installation-r1.json` installation descriptor is
separately hash-pinned. It binds the exact release, execution profile, provider,
base image and assembly assets. The original schema-2 acquisition input keeps
its original digest and inactive flags; acquisition and assembly evidence do
not themselves authorize an installation. Their original canonical digests are
included in the final provider-specific, HMAC-sealed installation attestation.
Existing APT resources, recipe digests and attestation serialization are retained.

## Admission and authority

`RuntimeInstallationService.start_preview` requires Development mode and an
exact `PreviewAcknowledgement`: recipe ID, revision, digest, release, Preview
tier and `development-only-prerelease` acknowledgement. The state-owning
application persists this receipt in the existing pending installation row.
The broker verifies that exact receipt using a read-only SQLite connection,
checks the installed worker contract, and launches the same closed one-shot
worker from its own immutable Docker image. The broker gains no SETUID/SETGID
capability or database-writing authority.

A repeated pending handoff can use the same operation and exact receipt. An
interrupted acquiring/assembling/validating operation uses existing exact-worker
observation and free-lock reconciliation. A failed operation needs a new explicit
admission; its replacement reacquires the same immutable inputs. Consent cannot
transfer to RC2, final, a different recipe digest, image or sealed revision.

The existing worker remains offline, read-only, root with only its existing
SETUID/SETGID capabilities and fixed mounts. RC1 creates a socketless acquisition
helper on the existing operation-owned acquisition network. Its command inputs
are fixed wheel-owned OSTree pulls and the reviewed Ubuntu closure; it has no
Docker socket, state/profile mount, devices, capabilities or published ports.
Only the worker lock volume is shared. The helper is stopped and removed before
sealed image-context capture, leaving no concurrent acquisition writer.

OSTree and GPG tools are installed only in the broker/worker target. No Kodi
binary is included in the wheel, source tree, Web image or worker image.
Signatures, ref/collection bindings, architecture, complete inventories and
source/release metadata are verified using the existing Phase 2 implementation.
Missing historical objects or support DEBs fail without substitution.

## State machine and sealing

`install_reviewed_runtime` keeps the existing pending → acquiring → assembling
→ validating → ready/failed transitions and acquisition lock. The new dispatch
calls `assemble_in_worker`, which uses the Phase 3 leased, inventory-checked,
sealed-memfd assembler. Supporting packages are downloaded by exact hash and
size, then verified again by the offline worker. No persistent binary cache is
introduced. Private staging is cleaned when the acquisition lease ends.

The ready transaction checks the complete RC1 validation report before creating
its Preview definition, exact revision, artifact, execution profile, validation
evidence, sealed attestation and approval. It does not bootstrap a managed
instance for RC1. There is no second installation state machine.

The bounded `kodi-ostree-recipe-acquisition-v1` attestation includes the
installation recipe, exact consent and operation, full acquisition/assembly
evidence, three upstream pins, trusted primary/subkey identities, support
closure, asset hashes and actual image ID. The unchanged HMAC additionally binds
the revision, operation and runtime validation evidence. Supporting closure data
is wheel-owned and hash-bound rather than duplicated into oversized DB records.

Eligibility independently requires Preview, exact release/recipe/profile/image,
reviewed OSTree source kind, approved revision, complete provider evidence,
passed validation and a valid HMAC. `include_prerelease` alone is insufficient:
`preview_recipe_digest` must identify this exact reviewed recipe. The existing
schema represents all these values; no migration is introduced.

## Validation and next activation boundary

`validate_ostree_image` uses existing RPC, readiness and framebuffer helpers and
the Phase 3 execution checks: native releasecandidate/1 and source prefix,
canonical RC1, actual Python 3.14.7/API 3.1.0, FFmpeg 9.0.2, X11/llvmpipe,
nonblank 1280×720 captures, a changed Settings window, two clean cycles, profile
persistence, private session D-Bus and non-root/capless/seccomp isolation.
Temporary Python-probe completion is bounded; the RPC response alone does not
prove its result file exists. Forced shutdown and stale display artifacts fail.

Stock Flathub TagLib 2.3.1 remains unchanged. The assembly evidence discloses the
missing Kodi Matroska fast-scan patch; this runtime does not claim media-metadata
equivalence to a patched dependency build. The Phase 3 native Greynetic proof
remains a separate qualification and does not expand installation dependencies.

Development-mode actions, exact consent, status polling and provisioning are
integrated and qualified in isolation. Production deployment remains a separate
authorized step; no external pairing or MCP grant is created by installation.
