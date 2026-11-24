# Review added files before a source copy

An add-on may generate extra files in its installed folder. Ordinary Copy/Sync
continues to refuse unexpected destination changes. An administrator can explicitly
review up to 32 added regular files and approve one ordinary guarded copy. This
works for attached registered sources on stopped disposable managed Kodi targets;
it does not change external targets, workspace deployment or MCP permissions.

## Administrator steps

1. Keep Kodi stopped and retain a verified snapshot or other supported recovery
   backup before copying. Review the failed copy in Kodi history; do not erase its
   receipt or retry uncertain work.
2. Open the Kodi details page, expand **Source attachments and copy records**, and
   choose **Review added files before copying** for the attached source. The saved
   Current Kodi copy failure also links here when provenance was refused.
3. Enter the exact added paths relative to the add-on directory, one per line.
   Choose **Inspect these added files**. No path names are automatically trusted.
   If the exact additions are unknown, stop and investigate; there is no filesystem
   browser or guessed ignore list.
4. Inspect every displayed path, regular-file type, byte size, SHA-256, octal mode
   and owner. Check the target/container/source identities and the trusted,
   current-destination and candidate-source digests. Review the file contents
   independently: Kodi skin XML and executable files can change runtime behavior.
   Matching a familiar name or XML structure does not establish safety.
5. Check the approval box for each file and the overall acknowledgement, then
   choose **Approve these files and copy source**. This approval starts exactly
   one ordinary stopped-only source copy. It expires 15 minutes after preview.
6. Keep the review URL. Refresh it for the saved operation state and use Kodi
   history for recovery. Refresh and repost never create another operation. A
   failed or consumed approval requires a new review only after the original
   outcome is settled. If executor submission loses its reply, the exact queued
   operation remains recorded; preserve it and use normal operation recovery.
   Do not create another request to work around a queued or uncertain operation.

Review itself does not update the baseline. The canonical inventory with only the
named files excluded **in memory** must reproduce the retained successful full
copy digest. Thus every prior file and directory, including earlier preserved
files, must still be exact. After a successful copy, normal complete readback
records the new destination digest, including the preserved approved files.

## Limits and security contract

Only additions in already trusted directories are eligible (the add-on root also
counts). Added directories, symlinks, hardlinks, special files, source collisions,
changed/deleted trusted entries and any unreviewed addition refuse the operation.
Added files must have the approved runtime uid/gid and ordinary 0644 or 0755 mode.
The proof covers the existing canonical fields: type, path, SHA-256, size, mode and
ownership. Timestamps and inode numbers are not canonical provenance fields.
Replacing a file with canonically identical bytes/metadata is indistinguishable
from retaining it. Approval is of those exact bytes and metadata, not a name.

Approval binds the retained baseline operation/evidence, registered source and
attachment, candidate digest, selected session, workspace generation and digest,
target, container inspection (including image and labels), runtime revision and
execution profile. When no selected session exists, normal source-copy admission
must still permit that exact disposable target. Stale evidence retaining a proven
successful copy may be reviewed; unknown or recovery-required authority cannot be
adopted. Revoked ownership, changed scope, expired leases, expired approval and
replay fail closed.

Source scanning, double capture, immutable staging, delta archive construction,
normal mutation/global-heavy leases and recovery receipts are reused. Immediately
before applying the immutable delta, the broker rechecks all approval bindings
and reads the entire destination again. During this final preimage/copy/readback
window a SQLite writer transaction prevents identity/generation changes. Other
database writers temporarily wait and may time out, including a Web request that
needs to refresh its authentication session timestamp. Source hashing and staging
are outside this window. Refresh saved status after the copy settles; never infer
an operation outcome from a browser timeout. Normal lease expiry still applies;
post-write uncertainty retains reconciliation rather than reporting success.

The approved files stay outside the source delta and must remain byte/metadata
identical in complete readback. There is no force switch, deletion, baseline
adoption, content-based exemption, automatic retry, Kodi start, enablement or native
acceptance. A later candidate that intentionally owns an already trusted preserved
file follows the existing provenance-checked ordinary copy rules.

Review/approval/dispatch records are retained independently of ordinary prunable
copy details, including target retirement. Storage is bounded to 1,024 reviews;
when full, review refuses rather than deleting audit history. There is no new
history-deletion or arbitrary filesystem administration capability.

## Installation and later qualification

Deploy matching Web, broker and schema `0018_source_addition_reviews` together.
The additive migration changes no existing authority rows. It refuses schema
downgrade when review history exists; restore a compatible pre-upgrade backup
for application rollback. See [upgrade/rollback](LINUX-V1-UPGRADE-ROLLBACK.md) and
[managed-state recovery](MANAGED-V2-FRESH-INSTALL.md).

Source/fixture validation is separate from native qualification. Before production,
qualify one separately authorized disposable managed Omega skin iteration: normal
initial copy, runtime generation, Stop, exact review/approval, candidate copy,
unchanged generated-file readback, then separately authorized native checks.
Keep preserved production profiles and snapshots out of that qualification.

Once preserved in a successful copy, an approved file is part of the trusted full
destination. If a later runtime changes that same file, this additions-only
workflow cannot approve its modification. Preserve the profile and investigate;
do not delete it or replace provenance records to make review pass.
