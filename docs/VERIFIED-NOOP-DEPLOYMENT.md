# Verified reuse of an unchanged managed deployment

`workspace_deploy` can return `result_classification: verified_existing` with
`deployment_work_skipped: true`. This is a new, durable verification receipt
for the request UUID. `managed_verification.reused_deployment_id` identifies
the exact previous deployment whose bytes were verified again. No stop, copy,
restart, activation or execution occurred in this request. Historical replay
returns the original receipt; it does not recheck the current target.

The first slice covers managed disposable, single video-plugin descriptors on
bytecode-suppressed runtimes. Exact workspace/source ownership and target
grants, validation and generation, selected retained session, rate admission,
heavy and mutation leases, and unresolved-operation fences remain mandatory.
The five-second deployment admission interval is unchanged. External development
and other add-on shapes retain their existing deployment procedures.

Eligibility requires the latest unambiguous successful receipt for the exact
workspace generation and profile, with managed verification proof recorded by
this implementation. Older receipts remain valid historical evidence but force
an ordinary deployment for a new request. The complete current workspace is
captured twice; its actual digest and ownership-bound destination digest must
match. The existing broker reads and hashes the entire installed tree, including
file ownership, modes and unexpected members. Native readiness, the enabled
exact add-on identity, runtime/container identity, and authenticated reviewed
Bridge trust are checked. Source, selection, authorization, runtime revision,
Bridge identity and lease ownership are checked again before finalization.

Lease reservation and release each advance the existing observation epoch.
Proof records the observed and committed revisions separately; only the known
advance for this request's own reservation is accepted. Other runtime revision
changes force ordinary deployment or the existing refusal. Lost authority or
leases during finalization fail closed before any native mutation.

`managed_verification` reports the target container/runtime identity, source
and installed digests, Bridge generation and identity digest, reused receipt,
readiness check, scanned/hashed/readback counts, zero staged/transferred bytes
and zero native restarts for a verified no-op. Activation is already satisfied;
execution is explicitly `not_performed`. File verification does not establish
fresh code execution, GUI acceptance or functional smoke. Continue with the
existing supported launch/smoke and observation tools when needed.

The optional `managed_verification.destination_only_count` reports how many
installed entries were absent from the workspace at this operation. Ordinary
Copy/Sync reuses its existing verified delta count; entries can include both
files and directories. A positive count means those entries were retained,
including after a source removal or rename. They are not necessarily obsolete,
and the receipt does not prove their individual ownership. Review the exact
removal/import receipts and ask the Web administrator to review installation
provenance when needed. No cleanup or deletion is performed.

A known zero means no destination-only entries at verification time. Exact
verified no-op equality proves zero after the existing fresh checks; retained
extras prevent that no-op path. Missing counts in historical verification JSON
remain unknown (`null`), and unavailable optional proof leaves
`managed_verification` absent or `null`. Replay returns historical evidence
without refreshing its count. The count uses the existing bounded verification
JSON; it adds no database migration, scan or destination readback.

Insufficient proof follows the ordinary deployment path, including its existing
drift and reconciliation behavior. No-op verification never repairs drift.
Stopped Kodi cannot establish eligibility. Container replacement, stop/resume,
source/generation changes, disabled add-ons, trust changes, stale history and
unknown destination files cannot inherit eligibility from an old digest.
Evidence is historical as of verification; privileged external writes after an
operation completes remain detectable by the next verification, not prevented
by a receipt.

Migration `0090_verified_noop_deployment` adds one nullable bounded proof column
and an explicit alternative success shape with all native mutation markers
false. The earlier schema required copy/start markers for direct success and
could not represent this result truthfully. Existing rows retain null proof.
Downgrade refuses to discard verification history. Use the supported backup and
restore procedure for recovery; do not point an older application at used data.

This closes one measured optimization slice of #109/#111/#113. It does not
implement owned-file removal, general live activation or a combined agent tool.
