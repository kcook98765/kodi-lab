# Phase 14 production validation evidence template

Classification: redacted semantic evidence only. Never include passwords,
Authorization headers, pairing tokens/secrets, HMAC material, raw screenshot
pixels/base64, native filesystem paths, raw protocol/JSON-RPC payloads or full
network responses.

## Validation identity

Expected bridge candidate before Stage B: add-on `service.kodi_lab_bridge`
version `1.1.8`, ZIP SHA-256
`20a14873a46969cd3f1f962875a3905caa56ce6f088e95f6d3b2b8de6e666762`,
semantic fingerprint
`4a055b9479aeb72d2f8fe16d317ed525f69991908b28580d8a071be33aa26e5a`.
Stop if the independently accepted artifact differs.

- Date/time window (bounded):
- Validator:
- Repository commit:
- Alembic head:
- Logical target UUID:
- Logical MCP client UUID:
- Kodi version:
- Transport classification (`http`/`https`, no endpoint):
- `transport_location_hash` (redacted location identity; no raw endpoint):
- `native_control_consent_digest` (when applicable):
- Exact reviewed bridge add-on version:
- Reviewed ZIP SHA-256:
- Semantic build fingerprint:
- Reviewed-build UUID:
- Bridge UUID (if approved for evidence):
- Starting global mutation state:
- Starting target suspension state:

## Preflight contract

- MCP tool count: `15`
- Production-eligible tools observed: `target_list`, `target_status`,
  `addon_list`, `screenshot_capture`, `remote_action`
- Exact production artifact checksum/provenance verified: PASS/FAIL
- Secrets/raw paths/raw payloads absent from evidence: PASS/FAIL

## Stage A — Inspection

- Start/end time:
- Preset/grant/inspect-scope semantic state:
- Transition classification: `native-material` / `policy-only` / `none`
- Before revision tuple (`policy/native-bound/high-water`):
- After revision tuple (`policy/native-bound/high-water`):
- Policy revision monotonic and advanced exactly once when material: PASS/FAIL
- High-water equals current policy revision: PASS/FAIL
- Native-bound revision does not exceed policy revision: PASS/FAIL
- If native-material, native-bound equals current policy revision: PASS/FAIL/N/A
- If policy-only, native-bound remains the last native-material revision:
  PASS/FAIL/N/A
- Production designation classified policy-only: PASS/FAIL/N/A
- Generation / verification / administrative state before and after:
- `target_list` outcome and expected target present:
- One `target_status` outcome:
- One bounded `addon_list` page outcome/count:
- Endpoint/credential/path disclosure absent: PASS/FAIL
- Discrepancy/stop reason (if any):

For the current validation sequence only, the expected designation evidence is
`5/5/5 -> 6/5/6`, with generation unchanged, verification remaining `verified`,
designation changing `pending_confirmation -> confirmed`, and administrative
state remaining `disabled`. This is an example, not a general invariant.
Immediate three-way equality after designation is not an acceptance criterion.
Use only the distinct names `transport_location_hash` and
`native_control_consent_digest`; do not record an ambiguous generic
`endpoint_digest`.

## Stage B — Screenshot

- Start/end time:
- Manual exact ZIP installation attested by operator: PASS/FAIL
- Reviewed-build registration/pairing/provenance states:
- Native generation / bridge revision:
- Visual scope UUID:
- Screenshot request UUID:
- Outcome:
- Dimensions:
- Encoded byte count:
- PNG SHA-256 metadata:
- Bounded capture duration:
- Kodi Lab durable pixel retention absent: PASS/FAIL
- Pixels/base64 omitted from this evidence: PASS/FAIL
- Discrepancy/stop reason (if any):

## Stage C — Navigation

- Start/end time:
- Navigate scope UUID:
- Mutation unblock/suspension transition times:
- Request UUID:
- First `home` outcome / receipt state:
- Exact replay outcome:
- Single-dispatch evidence classification:
- Re-block/re-suspend confirmed: PASS/FAIL
- Ambiguous or replacement request used: MUST BE `NO`
- Discrepancy/stop reason (if any):

## Stage D — Transient approval

- Start/end time:
- Transient scope UUID:
- Request UUID:
- Initial `volume_down` result: expected `approval_required`
- Approval UUID:
- Pending expiry (bounded):
- Web decision/outcome/time (no session material):
- Approved dispatch deadline (bounded):
- Same-request retry result / receipt state:
- Same-request replay result:
- One-shot consumption / single-dispatch evidence classification:
- Re-block/re-suspend confirmed: PASS/FAIL
- MCP self-approval or replacement request used: MUST BE `NO`
- Discrepancy/stop reason (if any):

## Closeout summary

- Stage A: PASS/STOP/NOT RUN
- Stage B: PASS/STOP/NOT RUN
- Stage C: PASS/STOP/NOT RUN
- Stage D: PASS/STOP/NOT RUN
- All mutation ambiguity absent: PASS/FAIL
- No failure injection used: PASS/FAIL
- Operator-owned lifecycle actions, if any (semantic only):
- Remaining blocker:
- Independent closeout audit reference:
- Final authoritative gate reference:
- Phase 14 state: `PARTIAL` until every canonical closeout criterion is met
