# Phase 14 production validation runbook

Status: prepared, **not executed**. Every stage requires later explicit live-
validation authorization. Do not contact a production/LAN target while merely
reviewing or preparing this runbook. Do not inject failures on production.

## Global rules

- Use one explicitly approved production target and record its logical UUID,
  not endpoint credentials or native filesystem details.
- Verify branch/checkpoint, migration head, exact fifteen-tool catalog and the
  five-tool production subset before live work.
- Start with global production mutations blocked and target mutations
  suspended.
- Stop on identity, provenance, scope, preset, grant, HTTP acknowledgement,
  response, receipt, approval or audit discrepancy.
- At mutation stages, any uncertainty about whether native dispatch occurred is
  ambiguous and ends validation. Never retry under a new request UUID.
- Capture only the bounded redacted evidence in
  [PHASE14-EVIDENCE-TEMPLATE.md](PHASE14-EVIDENCE-TEMPLATE.md).
- Do not deploy software, use SSH/shell/filesystem authority, restart Kodi,
  change host lifecycle, issue raw JSON-RPC or use a production workspace.

Stage B-D currently have an artifact prerequisite: an exact corrected bridge
successor must be independently reviewed, checksummed and Web-registered. The
old deterministic 1.1.7 package remains Phase 13 evidence but is not the
corrected production artifact. Stop before Stage B if that prerequisite is not
satisfied.

## Stage A — Inspection

Authority: exact `Production Inspection` on both client and target, generic
target grant, and active current-generation `inspect` scope only. No bridge,
visual, navigate or transient scope is required. Keep both mutation vetoes set.

1. Confirm the exact client/target/preset/grant/scope and current native
   production designation in Web administration.
2. Invoke `target_list` and confirm the one expected logical production target
   is present with no endpoint/credential/observation disclosure.
3. Invoke `target_status` exactly once for that target and verify the bounded
   current semantic/native identity result.
4. Invoke one bounded `addon_list` page and verify path-free bounded inventory.
5. Record semantic outcomes and stop. Any mismatch or unexpected disclosure is
   a discrepancy; do not continue to Stage B.

## Stage B — Screenshot

Prerequisite: the exact independently reviewed corrected bridge release ZIP was
checksum-verified and manually installed by the operator on a benign,
non-sensitive Kodi screen.

1. In Web, register the exact reviewed build and provenance.
2. Generate a show-once pairing token; the operator enters it locally. Do not
   record it.
3. Pair, inspect the observed identity and explicitly confirm installed
   provenance.
4. Assign exact `Production Visual Inspection`, generic grant and current
   inspect plus visual scopes. Record the exact HTTP risk acknowledgement if
   applicable.
5. Invoke `screenshot_capture` exactly once with a new request UUID.
6. Verify bounded metadata (dimensions, encoded size, digest, semantic outcome)
   and confirm Kodi Lab persisted no pixels/base64/thumbnail/raw response.
7. Do not attach pixels to evidence. Stop after this one benign capture.

## Stage C — Navigation

Authority: exact `Production Remote Kodi` on client and target, generic grant,
current inspect plus navigate scopes, current reviewed bridge binding, and the
exact native-HTTP acknowledgement if applicable.

1. Confirm all authority/revision dependencies.
2. Deliberately clear target mutation suspension and the global mutation block
   immediately before the action.
3. Invoke `remote_action` exactly once with action `home` and one new recorded
   request UUID.
4. Replay the exact same client request UUID, target and action once. Verify the
   historical response and evidence that no second native dispatch occurred.
5. Re-block global production mutations and re-suspend target control.
6. Record bounded receipt/audit outcomes and stop. Ambiguity at any point is a
   stop condition; do not create a replacement request.

## Stage D — Transient

Authority: exact `Production Remote Kodi`, generic grant, current inspect plus
`request_transient_control` scopes, current reviewed bridge binding, and exact
native-HTTP acknowledgement if applicable. Navigate and visual scopes are not
required for the transient request itself.

1. Confirm current authority/revisions, deliberately clear target suspension
   and the global mutation block only for this validation.
2. Invoke `remote_action` exactly once with action `volume_down` and one new
   recorded request UUID. Verify `approval_required` and one pending approval;
   no receipt/native dispatch exists yet.
3. In authenticated Web administration, approve that exact displayed action
   once. MCP must not perform or simulate approval.
4. Retry the exact same request UUID, target and action once. Verify one-shot
   consumption and at most one native dispatch.
5. Replay the exact same request again once. Verify historical replay and no
   second dispatch.
6. Re-block global production mutations and re-suspend target control.
7. Record bounded approval/receipt/audit outcomes and stop. Any uncertain send,
   response loss or incoherent evidence ends validation without a new request.

## Completion handling

Successful Stages A-D do not by themselves mark Phase 14 complete. Preserve
only the redacted evidence template, run the separately authorized independent
closeout audit, reconcile any blocker, and run one final authoritative gate
after the final content change. Phase 14 remains PARTIAL until all closeout
criteria are satisfied.
