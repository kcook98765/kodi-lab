# Piers Preview action consent (#248)

The Standard/Development view cookie is a presentation preference, not Preview
authorization. The consent foundation supersedes the view-mode admission
requirement in D-0425. The subsequent [unified navigation](UNIFIED-NAVIGATION.md)
removes the global toggle while preserving this consent contract.

## Admission audit and replacement

| Layer | Previous dependency | Current boundary |
| --- | --- | --- |
| Runtime page, matrix, history and list API | Four cookie-dependent filters in `web/app.py` | Authenticated runtime administration includes Piers in either view; explicit matrix filters still apply. GET does not grant consent. |
| Install / Retry / Reacquire POST | Development cookie plus exact acknowledgement | Authenticated administrator, same origin, session CSRF and a strict action-specific approval envelope. |
| Create instance POST | Development cookie plus exact acknowledgement | The same mutation protections, with a separate `create` approval. |
| `RuntimeInstallationService` | `development_mode` boolean and exact acknowledgement | `PreviewActionApproval` binds the requested action and reparses the existing exact acknowledgement, including nested fields. It must match the current reviewed recipe and eligible operation phase. |
| Create Managed Kodi registration | Internal unconditional development-mode argument | The existing owner-bound Install-and-prepare operation must have its own exact Preview approval and eligible stage/version/runtime. Registration carries only that original compound intent's Create approval. |
| Broker and runtime | Exact recipe, operation-owned admission, sealed installation proof, image presence, capability and worker/lock admission | Unchanged; the new request envelope never replaces broker evidence or installation attestations. Historical r2/r3 evidence remains readable and independently eligible under its original rules. |
| Roles, grants and target authority | Administrator session and service authorization; no role encoded by view cookie | Unchanged. MCP, Bridge, external-target grants, OTP and screenshot consent are untouched. |

No `development_mode` argument or unconditional substitute remains in the
application service or its callers. The remaining view flags in templates and
navigation continue to filter presentation and need their own #248 review.

## Request contract

Preview runtime POSTs require this envelope around the **existing** reviewed
`PreviewAcknowledgement` (the full current values are shown on the runtime card):

```json
{
  "action": "install",
  "approved": true,
  "acknowledgement": {
    "recipe_id": "piers-22.0-rc1-r3",
    "recipe_revision": 3,
    "recipe_digest": "sha256:77d02aed048def17482f6b225fccf37670ca35d441bdf1e898c58acb46fdb7e8",
    "release": "22.0-rc.1",
    "support_tier": "preview",
    "acknowledgement": "development-only-prerelease"
  }
}
```

The action must match the URL's `action` query (`install`, `retry`, `reacquire`)
or be `create` on `/api/runtime-installations/piers/instances`. Missing, extra or
malformed fields are rejected. True must be a JSON boolean. Old flat bodies are
rejected rather than silently treated as fresh approval. Routes and stable
Matrix/Nexus/Omega request bodies are unchanged. Python service callers now pass
`approval` instead of `development_mode` / `acknowledgement`.

The browser opens the selected action review, clears earlier checks, and requires
both review confirmation and a fresh Preview check. The exact release, tier,
recipe revision and digest are inspectable. Submission, completion, cancellation,
failure, page restoration/hiding, version changes and observed phase/operation/
recipe changes clear review state. No cookie, storage or blanket enablement saves
Preview approval. Status polling only reads; an uncertain POST is never replayed.
An already pending installation rejects a new Install request; inspect the
original operation rather than redispatching its launch.

The existing Create Managed Kodi wizard is an explicitly approved compound
Install-and-prepare operation, not a runtime-admin Install followed by an implicit
Create. Its immutable owner-bound intent and continuation checks remain intact.
It grants no later independent runtime-admin action. This PR does not change its
onboarding, lifecycle or request schema.

## Navigation follow-on

The [unified-navigation follow-on](UNIFIED-NAVIGATION.md) removes template view
filters, preset visibility restrictions and the global toggle. Its separate
presentation review retains session, CSRF, capability, target-grant and production
protection boundaries. Neither change includes deployment or release qualification.
