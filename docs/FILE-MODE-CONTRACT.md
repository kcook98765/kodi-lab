# Source and resident file-mode contract

Source manifests represent normalized deployment semantics. Directories are
`0755`; regular files are `0644 | (source_mode & 0111)`. The eight supported file
modes are `0644`, `0645`, `0654`, `0655`, `0744`, `0745`, `0754`, and `0755`.
Read/write permissions and special bits are normalized at source capture;
individual owner/group/other execute bits are preserved.

Source capture, staged trees, uploaded-ZIP ingestion, workspace capture/edit/import,
promotion preimages and rollback checks retain that existing policy. The source
manifest format and ZIP payload policy identity are unchanged. Normalized staging
and transport archives carry those exact masks. The repository-delivery path
continues to reject executable regular files under its narrower package contract.

Supported application backup/restore preserves actual safe workspace modes and
numeric Web ownership, including partial execute masks inside the private root.
Its existing checks still reject group/other writes, special bits, unsafe types,
hardlinks and wrong ownership; no backup-mode exception or permission repair is
needed for these eight masks.

Managed destination readback records actual permission bits, UID/GID, type, size
and content hashes. Verification compares them exactly against source plus approved
runtime ownership. It does not normalize a wrong owner, widened mode, special bits,
changed contents or unsafe file type into success. Ownership-bound destination
digests remain distinct from semantic source digests. GUI launch derives and checks
the correct destination evidence, then sends the semantic source digest to Bridge.

Stable Bridge 1.1.27 and its 1.1.28 RC1 overlay correct two consumers: incoming
regular-file manifests accept only those eight integer masks, and POSIX resident
inspection preserves each execute bit when deriving the semantic manifest.
Materialization already uses authenticated manifest modes. Windows/Android logical
mode handling remains separate; this change does not claim native acceptance there.
Resident semantic inspection does not replace the broker's exact managed
permission/ownership proof. POSIX unsafe-file reads use a nonblocking no-follow
open before the existing type/hardlink check, so a FIFO cannot wait for a writer.

## Artifact and history transition

New Bridge bytes receive new versions, semantic fingerprints, source inventories,
deterministic ZIP hashes, bundle pins and committed-source provenance. They must be
installed through the existing reviewed lifecycle procedure with matched application
artifacts. No manual file replacement or generic service ZIP admission is added.

The exact predecessor 1.1.25/26 identities remain approved only for established
trust, alongside all existing previous/historical/retained/legacy identities.
Active matching trust can authenticate without secret rotation or generation
rewriting. New establishment and current managed launch require 1.1.27/28. Exact
complete 1.1.25/26 upgrade inventories are retained in addition to 1.1.23/24;
unknown, changed, linked or extra-file trees remain ineligible for replacement.

Historical source/workspace/ZIP manifests and receipts are preserved unchanged.
An old resident observation of a partial-mask tree used a different normalization;
it is not reinterpreted or promoted into current evidence. Upgrade, fresh current
validation and exact deployment/inspection are required. Unchanged `0644` and
`0755` semantic manifests retain their byte representation. Generation, runtime,
container, identity, grant and authenticated managed-launch marker fences remain
mandatory, including after an artifact upgrade.
