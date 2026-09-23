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
- Record revision transitions as `policy/native-bound/high-water`. The current
  policy revision must equal the permanent high-water, and the native-bound
  revision must never exceed the current policy revision.
- Classify each transition before evaluating it. A native-material transition
  advances policy exactly once, binds the native row to that same revision and
  leaves high-water equal to policy. A policy-only transition advances policy
  exactly once and high-water with it while leaving the native binding at the
  most recent native-material revision. Thus `native-bound <= policy` is valid;
  immediate three-way equality is required only after a native-material
  transition.
- Production designation is policy-only. If the preceding coherent state is
  `r/r/r`, designation must produce `(r+1)/r/(r+1)`, not three-way equality.
- Keep digest names unambiguous in validation records: use
  `transport_location_hash` for the redacted transport-location identity and
  `native_control_consent_digest` for native-control consent. Do not collapse
  them into generic `endpoint_digest` wording.
- Do not deploy software, use SSH/shell/filesystem authority, restart Kodi,
  change host lifecycle, issue raw JSON-RPC or use a production workspace.

Stage B-D have an artifact prerequisite. Bridge 1.1.7 remains Phase 13 evidence
and is not the corrected production artifact. The packaging-only 1.1.8
candidate is `service.kodi_lab_bridge-1.1.8.zip`, ZIP SHA-256
`20a14873a46969cd3f1f962875a3905caa56ce6f088e95f6d3b2b8de6e666762`
and semantic fingerprint
`4a055b9479aeb72d2f8fe16d317ed525f69991908b28580d8a071be33aa26e5a`.
It has not been installed on production or Web-registered. Stop before Stage B
unless independent artifact review is accepted and the exact 1.1.8 bytes are
available; reviewed-build registration remains an explicit Stage B action.

## Stage A — Inspection

Authority: exact `Production Inspection` on both client and target, generic
target grant, and active current-generation `inspect` scope only. No bridge,
visual, navigate or transient scope is required. Keep both mutation vetoes set.

1. Confirm the exact client/target/preset/grant/scope and current native
   production designation in Web administration.
   When designation is performed for this validation, classify it as a
   policy-only transition and verify generation and native binding do not
   change, policy advances exactly once, and high-water equals the new policy.
   For the current validation sequence the evidence transition is
   `5/5/5 -> 6/5/6`; these absolute values are evidence for this sequence, not a
   general invariant. The target remains verified and disabled. Do not require
   immediate three-way equality after designation.
2. Invoke `target_list` and confirm the one expected logical production target
   is present with no endpoint/credential/observation disclosure.
3. Invoke `target_status` exactly once for that target and verify the bounded
   current semantic/native identity result.
4. Invoke one bounded `addon_list` page and verify path-free bounded inventory.
5. Record semantic outcomes and stop. Any mismatch or unexpected disclosure is
   a discrepancy; do not continue to Stage B.

Stage A revision acceptance requires an exact-once monotonic policy increment,
`high-water == policy`, and `native-bound <= policy`. Native binding equals the
new policy revision only when the current transition is native-material;
policy-only transitions may legitimately produce `(r+1)/r/(r+1)`. The prior
Stage A STOP at `6/5/6` remains valid historical evidence, but is reconciled as
a validation-expectation defect rather than a runtime or schema defect.

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
