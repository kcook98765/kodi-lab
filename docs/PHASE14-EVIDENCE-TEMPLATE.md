# Phase 14 production validation evidence template

Classification: redacted semantic evidence only. Never include passwords,
Authorization headers, pairing tokens/secrets, HMAC material, raw screenshot
pixels/base64, native filesystem paths, raw protocol/JSON-RPC payloads or full
network responses.

## Validation identity

- Date/time window (bounded):
- Validator:
- Repository commit:
- Alembic head:
- Logical target UUID:
- Logical MCP client UUID:
- Kodi version:
- Transport classification (`http`/`https`, no endpoint):
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
- `target_list` outcome and expected target present:
- One `target_status` outcome:
- One bounded `addon_list` page outcome/count:
- Endpoint/credential/path disclosure absent: PASS/FAIL
- Discrepancy/stop reason (if any):

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
