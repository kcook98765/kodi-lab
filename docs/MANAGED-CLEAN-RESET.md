# Agent-controlled clean Reset

`managed_reset` completes the version-oriented agent lifecycle:
**Status → Start(version) → develop/test → Reset(version) or Stop(version)**.
**Stop retains the exact profile. Reset deliberately replaces its writable Kodi
settings, databases and installed add-ons with a clean approved-image baseline.**
Independent sources, agent workspaces, Git repositories/history and retained
Lab evidence are preserved.

## Authority and request

The administrator must explicitly add **both** `managed.versions` and
`managed.reset` to the acting client's custom preset and the canonical target's
policy, and grant that exact target to the client. Use a custom preset cloned
from Add-on Development or Full Lab Control. Existing built-in/custom presets,
credentials and target grants acquire no new authority automatically.

```json
{"name":"managed_reset","arguments":{"version":"omega","request_id":"25f7a7ae-c3a5-4536-9e30-bc46520be095"}}
```

The strict input contains only `version`, `request_id` and optional boolean
`restart` (default **false**). Versions are `matrix`, `nexus`, `omega` and `piers`.
There are no caller-selected containers, paths, profiles, images or runtime pins.
Persist the UUID before sending. The explicit destructive permission is the
agent's authority; the server requires no additional interactive Web confirmation.
MCP discovery honestly marks Reset as destructive.

The canonical target must already exist and be granted. An absent managed
registration can be composed from the administrator's approved cached runtime.
Existing registrations retain their exact pin. Catalog eligibility, sealed
provenance, cached image presence, legacy/preview requirements and disposable
instance policy remain mandatory. Reset never downloads, approves, repins,
adopts a historical/noncanonical target or removes a duplicate profile.

Reset grants no source/workspace access, other-client workspace authority,
external Kodi access, arbitrary Docker management, shell, screenshot or human
input authority. Native development and Bridge preparation keep their independent
grants and checks.

## What happens

1. Bind the principal/request/version/restart intent to the exact approved pin,
   image, old container (if any), observation epoch and deterministic child IDs.
2. Check users, independent holds, playback, active viewing/input, mutation
   leases, reservations, legacy reset and reconciliation. Release only the
   caller's own use/hold. Running Kodi stops through the existing guarded
   resource/lifecycle authority; an already stopped profile needs no Stop child.
3. Retire the exact stopped container through the existing lifecycle broker.
   A confirmed retirement requires its original identity-bound removal reply
   and successful owned lifecycle terminalization. Inventory absence alone
   cannot confirm it.
4. Invalidate profile-bound evidence and close the disposed Current Kodi
   selection after confirmed retirement. Preserve its historical binding and
   independent workspace/source/grant records. Do not select a successor.
5. Provision a distinct clean container from the same approved cached image.
   This path does **not** call the old fixture-initializing reset service,
   apply an assigned fixture, initialize test libraries or copy old add-ons.
   Fixture assignment metadata is preserved without applying it or temporarily
   clearing it to evade admission.
6. Leave the replacement stopped by default. With `restart=true`, request
   ordinary native Start under the existing global one-running-instance cap.
   Another running version is never evicted. If Start is blocked, the clean
   prepared profile remains available for a later authorized Start.

For an empty slot, no removal is attempted; the same approval, safety,
invalidation and clean-provision checks apply. Replacement accepts only its own
successful typed provision result, with a new container identity, the bound
runtime/image and broker postcondition proof.

The ordinary single-running policy remains global. Preparing a stopped clean
slot may coexist with another running version; optional Start still cannot
oversubscribe capacity or perform automatic switching.

## Results and readiness

The result includes parent `operation_id`, original `request_id`, `phase`,
`code`, the typed Stop/retirement/provision/Start identities that actually exist,
and separate outcomes:

- `retirement`: `not_required`, `not_confirmed` or `confirmed` for this original
  intent's disposal.
- `replacement`: whether this intent's own provision receipt was confirmed.
- `replacement_current`: whether that replacement is still the currently
  observed exact profile. Historical successful provision is not current proof.
- `start`: `not_requested`, `not_confirmed` or `accepted`.
- `profile_bound_state`: whether retirement invalidation completed.
- `status`: current native readiness, stored Bridge prerequisites and unassessed
  workspace/agent authority, using the existing version Status contract.

A complete Reset with restart means Start acceptance, not native or agent
readiness. Wait for `status.native_ready` before native use. An image can be
native-ready while Bridge is blocked; a new pairing/preparation and a fresh
workspace deployment remain separate. Reset does not manufacture those grants
or declare the target fully agent-ready.

## Replay and recovery

Repeat the **same UUID, version and restart value** after a lost response.
Changing the bound intent conflicts. A Reset UUID cannot be reinterpreted as
Start/Stop, nor can a Start/Stop UUID become Reset. Current credentials and exact
authority are rechecked on every call and before consequential dispatch.

A single durable parent fences the whole replacement, including gaps between
children. SQLite admission rejects unrelated lifecycle/workspace mutations,
new viewer/input ownership and Web keep-awake requests while Reset is active.
The existing cross-process lifecycle lock also serializes agent requests.
Read-only Status exposes an active Reset identity only to its owning client.
Neither lease expiry nor elapsed time clears an uncertain Reset fence.

- **Lost response after a confirmed Stop or retirement:** replay resumes only
  the next undispatched stage. It does not issue another successful Stop/removal.
- **Lost broker Stop/removal reply or uncertain outcome:** replay observes the
  original child. It never issues a second removal or creates a replacement
  while original retirement is unconfirmed. Inspect the retained typed history;
  administrator review/recovery is required. This API offers no force, timeout
  unlock, inventory-only confirmation or retry-under-a-new-UUID escape.
- **Known failed replacement after confirmed retirement:** the old retirement
  remains confirmed and inspectable. A failed child with no remaining lease or
  reservation becomes a terminal blocked receipt. Restore the prerequisite and
  use a fresh authorized Start or Reset intent. An absent slot needs no removal;
  replaying the original Reset never repeats destruction or its failed child.
- **Uncertain replacement create:** its original provision reservation and the
  Reset fence remain. Do not start a new request to adopt or duplicate it.
- **Approval/cache/permission loss:** refuse before dispatch. Proven retirement
  cleanup remains committed even if later provisioning is denied. A disabled
  client may need administrator restoration of its exact authority to observe
  or continue an original pending intent.

A pre-dispatch refusal is distinguished from a committed removal-dispatch intent.
Once removal might have been dispatched, failure stays fenced unless its exact
original retirement receipt proves success. An inconclusive removal without
that receipt can remain blocked indefinitely; a broader recovery/force-disposal
workflow is deliberately not added here.

Receipts are retained, with quotas of 1,024 Reset parents per client and 32,768
globally, and the existing retirement service's 256-receipt ceiling. Original
child history is protected from normal lifecycle pruning. New Reset requests
share the five-second client/version/action limiter; same-request replay is
exempt. Saturation requires administrator review, not silent replay-map deletion.

## Invalidation and development continuation

Bridge trust is invalidated and its old credential reference retired. Typed
retirement uses the configured secret store/root. Source deployment baselines
become stale and ZIP installation evidence is invalidated before removal. Native
readiness/tasks and observation epochs change through the existing lifecycle.
Old installed/enabled-state and workspace verified-deployment receipts retain
historical identity; they are not proof of files in the successor. Existing
fresh no-op/deploy/execute/launch checks require current container/Bridge lineage.

Current screenshot metadata is cleared while old retained files/evidence remain.
An in-flight screenshot cannot publish old pixels over a Reset: publication
checks the original observation token and active Reset fence under the SQLite
writer transaction. Historical remote observations remain bound to their original
container/epoch. Existing noVNC tickets bind exact container/epoch; old tickets
cannot attach to the replacement. Active viewers and input ownership block
admission rather than being forcibly revoked.

After replacement, explicitly select/bind the new profile through existing
Current Kodi development preparation when that policy applies, prepare Bridge,
and perform a fresh authorized deployment. Existing `managed_select` retains its
separate `managed.lifecycle`, workspace inspection and binding requirements;
Reset does not grant them. Reset does not transfer an old
workspace's runtime authority to the new container. Native Start and ordinary
screen viewing remain independent of workspace selection.

## Delivery and qualification boundary

This phase stacks on PR #240 and adds migration `0004_clean_reset`, parent
receipts/guards, the MCP tool and explicit permission picker. Migration changes
no existing profiles, grants, presets or running policy. Downgrade refuses once
Reset receipts exist. Earlier migrations remain frozen.

Isolated SQLite, fake broker/runtime and authenticated MCP checks establish the
source-level contract. Native Linux and Windows/WSL2 qualification is still
required for real approved-image clean recreation, identity/recovery fencing,
filesystem/secret permissions, Bridge re-preparation, fresh workspace deployment
and noVNC viewing/ticket cleanup. No native platform, installed upgrade, browser
visual acceptance or full release qualification is claimed by these checks.
The [four-version Web page](MANAGED-KODI-WEB.md) now exposes the same clean Reset
with human confirmation and separate Web administrator authority. Automatic
version switching and development reattachment remain follow-ups. Issue #238
remains open.


## Focused validation for this phase

**FOCUSED PASS:** 548 unique cases passed and 9 packaged-root-identity
transition cases skipped across 29 individually executed files. Ruff format/lint
and strict mypy passed on all 35 changed Python files. Diff whitespace checks
passed. The complete release suite and expensive native testing were not run.

| Selection | Result |
| --- | --- |
| Clean Reset service | 42 passed: all four cached versions, stopped/running replacement, optional restart, fixture avoidance, exact identity/grants, concurrent actions, lost replies, safe failure recovery, current-selection/profile isolation, Bridge/screenshot invalidation, private source/workspace/Git/evidence preservation, configured secret root and retained child history |
| Authenticated Reset MCP | 7 passed: explicit destructive opt-in, both permissions, strict three-field input, default stopped behavior, schema/destructive annotation and replay |
| New migration | 13 passed: direct 0003 upgrade, preserved authority/history, immutable intent, no lossy downgrade, admission backstop and functional Reset on actual Alembic schema |
| Existing lifecycle/development/viewing checks (11 files) | 210 passed: version Status/Start/Stop, original retirement/replacement, source-baseline/runtime replacement, workspace no-op authority, screenshots, noVNC authority, legacy agent and fixture reset |
| Additional resource and existing MCP checks | 12 + 24 passed |
| Affected schema/transition/package/registry checks (13 files) | 240 passed, 9 gated skips |

Initial failures exposed test-fixture result/state requirements, a stale expected
busy code and a mistaken test-only pruning method name; final affected files
passed after correction. The first synthetic future-migration test still
branched from 0003 and was updated to follow the new head. No production
protections were relaxed to pass those checks.
