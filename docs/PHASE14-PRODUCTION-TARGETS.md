# Phase 14 — External Production Targets

## Slice 1 through Slice 4B status and scope

**Slices 1, 2, 3, 4A and 4B: COMPLETE. Phase 14: PARTIAL.** Slice 1
establishes production representation and trust policy only. Slice 2 adds
bounded read-only inspection. Independent Slice 2 completion audit found and
remediated one generation-downgrade hazard by making production identity
generation database-monotonic; retained old scopes therefore cannot regain
effect through malformed rollback. No real Kodi or LAN host was contacted
during implementation or formal review. Independent Slice 3 completion audit
likewise found and remediated bridge-revision deletion/reuse, cross-client
bridge replay aliasing, volatile-pixel retention on metadata-commit failure and
failed production token retention. Slice 3 is formally complete only with those
fail-closed defenses.
Slice 4A adds only constrained production navigation under the accepted action-
aware architecture. Its independent completion audit found and remediated the
receipt cleanup boundary: only coherent completed base/binding/audit evidence
can now expire atomically, while pending, ambiguous and malformed relationships
remain protected. Its deterministic proof likewise contacted no LAN or
production Kodi.
Slice 4B adds exact one-shot Web approval for the seven transient playback/
track/volume semantics. Approval intent and consumption are durable, bounded
and replay-safe; receipt claim is atomic with consumption before the one native
send. Deterministic proof contacted no LAN or production Kodi.

Included:

- immutable `external_production` target class;
- shared external endpoint, encrypted credential and native observation state;
- mandatory one-to-one production policy;
- separate Web-only native confirmation and production designation ceremonies;
- monotonic identity generation and explicit reverification;
- global default-blocked mutation control with monotonic policy epoch;
- per-target deny-only mutation suspension;
- semantic audit events; and
- one centralized MCP class veto shared by target and workspace-target paths;
- generation-bound production `inspect` scopes;
- the exact immutable Production Inspection preset;
- centralized stored/pre-read/post-read production authorization;
- stored-only private `target_list` plus fresh native `target_status` and
  bounded `addon_list`;
- production-only read rate and concurrency admission; and
- Web-only production preset/grant/scope administration.
- explicitly reviewed bridge-build provenance and Web-only production pairing;
- independent monotonic bridge trust revision and exact HTTP-risk history;
- generation/revision-bound visual scopes and Production Visual Inspection;
- replay-safe, rate/concurrency-bounded memory-only production screenshots;
- immutable navigate scope and exact Production Remote Kodi maximum;
- replay-safe, veto/rate/concurrency-bounded seven-action navigation;
- separate exact native-HTTP control acknowledgement; and
- immutable transient-request scope and generic grant identity;
- Web-only pending/approve/deny/cancel administration;
- atomic one-shot transient approval consumption and Phase 11 receipt claim;
- protected approval/receipt/audit retention and durable admission; and
- exactly five production-eligible tools in the unchanged fifteen-tool catalog.

Excluded:

- production `context_menu` and `select`;
- DevelopmentSource baseline, workspace deployment/execution or promotion;
- support-addon deployment;
- lifecycle, restart, shell, SSH, generic filesystem or raw JSON-RPC; and
- DNS, public/WAN addressing or real-host validation.

## Representation and invariants

`targets.target_type` admits `external_production`, but target type is immutable
after insertion. Reclassification requires deactivating the old target and
creating a new UUID. A production row shares `external_kodi_targets` for the
structured address, encrypted secret reference and observed native identity,
then requires exactly one `external_production_policies` row. Database triggers
reject policy attachment to another class; application reads fail closed if a
production policy is absent.

Production creation explicitly flushes the target, external subtype, policy and
event in dependency order. This is the supported completion boundary: the
transaction either commits the complete aggregate or rolls it back.

## Lifecycle and designation

The projected lifecycle is:

1. `registered_disabled_unverified`;
2. `observed_pending_confirmation`;
3. `native_identity_confirmed`;
4. `production_designation_pending`;
5. `production_designation_confirmed`;
6. `enabled_read_only`;
7. `needs_reverification` or `identity_mismatch`; and
8. `deactivated`.

Probe stores an observation but cannot establish trust. Native confirmation
pins that observation through the Phase 13 trust mechanism. A distinct Web
request must then acknowledge that this is a production Kodi target whose
future mutations may affect real user state. The production policy records the
administrator, timestamp, exact observation UUID/digest and policy revision.
Only then may the target be enabled read-only; enablement adds no MCP authority.

## Identity generation and reverification

Each production policy starts with `identity_generation = 1`. Changes to IP,
port, endpoint protocol or Kodi credential advance it. A newly observed Kodi
identity/version mismatch also advances it exactly once for that invalidation.
These changes disable the target, clear the confirmed designation reference,
mark designation invalidated, suspend target mutations and require explicit
native plus production reconfirmation. Endpoint reachability never restores
designation automatically.

Future production grants and approvals must bind the exact identity generation.
Bridge-specific generation effects are deferred to the bridge slice.

## Mutation controls

`production_mutation_control` is a singleton seeded with
`production_mutations_blocked = true` and policy epoch 1. Authenticated Web
state transitions advance the epoch. Each policy also starts with mutations
suspended. These are deny-only intersections: clearing either state creates no
grant, eligibility or operation and cannot bypass the target-class MCP veto.
There is deliberately no persisted generic “production unlocked” state.

## Network and secret boundary

Production uses the Phase 13 structured endpoint validator: literal private IP,
fixed protocol and allowlisted ports; no DNS, arbitrary URL, redirects, ambient
proxy, public/loopback/link-local/multicast/reserved address or managed-Lab
network. Registration does not probe. Kodi credentials remain AES-GCM-protected
outside SQLite and are never returned through Web projections or audit.

Audit extends the existing semantic vocabulary with production registration,
observation, native confirmation, designation confirmation/invalidation,
enable/disable, reverification, configuration/credential change, deactivation
and global/per-target suspension events. Events contain semantic IDs, bounded
state and digests only—not credentials, authorization material, secret refs,
paths, raw JSON-RPC or bridge secrets.

## MCP and workspace boundary

The registry remains exactly fifteen tools. Slice 2 replaces only the direct
target blanket veto with the production inspection evaluator described below.
The workspace-target veto remains absolute, so indirect workspace deployment,
execution and promotion stay unavailable. Production Remote Kodi is assignable
only as the exact Slice 4A maximum and grants no operation without independent
scopes. Existing managed and external-development behavior is unchanged.

## Slice 2 production inspection authority

Migration `0048_production_inspection_scopes` adds an immutable scope row bound
to exact client, production target, current identity generation and closed
`inspect` value. The generic target grant remains mandatory. Active scope
uniqueness is per client/target/generation/value; revocation is terminal and a
later grant uses a new UUID. Stale rows remain visible durable history.

The new exact immutable built-in `Production Inspection` preset is assignable,
contains only `target.inspect` and cannot be cloned. Both the client and target
must use that exact built-in. Existing built-ins, custom presets and dormant
Production Remote Kodi gain no production eligibility.

One shared evaluator reloads current credential/client, exact presets and
permission, generic grant, active current-generation scope, mandatory policy,
enabled/verified state, confirmed native identity and designation, exact
confirmed-observation digest/coherence, configured/verified capabilities and
registry eligibility. It is used for stored list filtering, direct-read
preflight and post-read publication. Global mutation blocking and target
mutation suspension are intentionally not read criteria.

Only `target_list`, `target_status` and `addon_list` admit
`external_production`. Listing performs no network activity, omits every
ineligible target and exposes only logical identity, class, trusted Kodi
version, non-fresh inspection readiness and bounded supported read semantics.
It exposes no endpoint, port, authentication, secret, observation or bridge
material.

Status performs `JSONRPC.Ping`, `JSONRPC.Version` and
`Application.GetProperties` for only name/version on each request. Inventory
performs the same preamble followed by fixed 64-record `Addons.GetAddons` pages
up to 2,048 items, with only name/version/enabled requested and Kodi-required
ID/type fields normalized. MCP pages remain at most 100 and semantic responses
remain below 64 KiB with existing raw JSON and path-leakage bounds. Connect,
response and aggregate deadlines are one, two and four seconds. There is no
bridge, filesystem read or automatic retry.

Live name/version/protocol drift atomically records one bounded mismatch
observation, disables the target, advances generation once, invalidates
designation and suspends target mutation authority, then returns
`identity_mismatch`. This changes only local trust. Any grant, scope,
generation, credential, preset, state or designation change during a native
read suppresses the result at the post-read check.

Production list admission is two requests/second/client with burst two; status
is one request/five seconds/client-target; inventory is one request/thirty
seconds/client-target. Maps use server-derived UUIDs, cap at 1,024 entries,
retire only idle entries and reject new cardinality without evicting active
buckets. Native reads serialize per target and permit four production reads
globally beneath the existing eight-operation external limit.

Web administration assigns the exact presets, manages the generic grant and
creates/revokes current-generation scopes under the existing authenticated
session, Host, Origin and CSRF controls. MCP has no authority-administration
surface. Scope projections distinguish active, stale and revoked generations
without exposing endpoint, credential or observation content.

## Migration behavior

Migration `0047_external_production_policy` preserves existing target rows,
rebuilds the closed target-type constraint, creates production policy and
singleton mutation-control tables, installs class-binding and immutability
triggers, extends semantic event checks and seeds the global state blocked.
Downgrade refuses while production targets, policy/security history or
production semantic events exist.

Both an Alembic-created database and an upgrade from
`0046_external_bridge_development` must support deterministic target → external
subtype → production policy → observation/event insertion without relying on
ORM relationship ordering.

Migration `0048_production_inspection_scopes` preserves every Slice 1 row,
creates the generation-bound scope table and indexes/triggers, seeds Production
Inspection plus its sole permission, constrains production target-preset
assignment, rejects identity-generation decreases, and adds bounded
scope/generation fields to semantic MCP events. Downgrade refuses when scope
history, new-preset assignments or scope events would be lost.

Migration `0050_production_navigation` preserves existing scopes while adding
the closed `navigate` value and bridge binding, activates the exact remote
preset permissions and production-only assignment guards, and creates native-
control acknowledgement, receipt-binding and bounded audit storage with
immutability/protected-retention triggers. Slice 4A changes only the existing
`remote_action` production eligibility in application registry metadata. It seeds no scope,
grant, assignment, acknowledgement or mutation authority and refuses downgrade
while any Slice 4A authority or history would be lost.

Migration `0051_production_transient_approvals` preserves every 4A receipt,
adds deterministic immutable identities to existing generic grants, positive
client authority revision, the `request_transient_control` scope, durable
approval/event/audit/rate storage, monotonic revision/epoch guards and the
closed transient receipt-binding shape. It seeds no scope, approval, grant,
assignment or authority and refuses destructive downgrade while 4B authority
or history would be lost.

Migration `0052_production_policy_revision_binding` remediates the Stage A
successful-observation publication boundary. One coherent material production
transition advances target `policy_revision` exactly once, and the native row
binds that revision before publication. Database guards reject unbound native
state, material designation changes without the exact increment, decrements,
and delete/reinsert or SQLite conflict-replacement attempts that would restore
an older revision. Successful
reverification does not advance the already-changed identity generation.

Migration `0053_production_screenshot_capability_amendment` adds the narrow
authenticated-Web action for explicitly adding or removing only `screenshot`
from the configured production maximum. It adds immutable exact add/remove
audit history, a database guard requiring every material configured-set change
to match that history, and a deeper guard requiring downstream bridge-derived
authority to be tombstoned before removal. It seeds no capability, bridge,
scope, acknowledgement, approval or other authority.

Adding screenshot changes only the configured maximum and advances/binds policy
revision once. Native verified evidence, identity generation, designation,
enablement and current native inspection remain unchanged. Pairing, reviewed
bridge verification, visual scope and transport acknowledgement are still
independent requirements; the amendment alone cannot produce pixels.

Removing screenshot creates or advances a monotonic bridge tombstone, clears
Lab pairing references, revokes bridge-derived scopes and cancels live transient
approvals while retaining receipts, acknowledgements, grants, the preset,
native inspection and reviewed-build history. Remove/re-add cannot revive any
retained row. This amendment adds no MCP administration and does not change the
fifteen-tool catalog or five-tool production subset.

## Slice 3 reviewed bridge and visual inspection

Migration `0049_production_bridge_visual` reuses Phase 13 bridge protocol v1,
`external_bridge_pairings`, protected AEAD secret files, replay receipts and
semantic events. It adds no parallel protocol and no automatic bridge update.
Production accepts only an administrator-created active reviewed-build record
binding exact ZIP SHA-256, semantic fingerprint, add-on/protocol versions,
declared capabilities, supported Kodi versions, source reference, reviewer and
time. No build, including deterministic 1.1.7, is migration-approved.

Production bridge trust has a positive monotonic revision independent of native
identity generation. Pairing requires enabled coherent production-native trust,
configured screenshot maximum, a show-once target/purpose-bound 256-bit token,
the existing mutual possession exchange, exact active reviewed-build match and
a separate explicit installed-target/provenance confirmation. HTTP additionally
requires immutable exact configuration-bound risk acknowledgement; HTTPS uses
normal CA validation. Pairing grants no MCP authority.

The revision-bearing production pairing row cannot be deleted or replaced;
malformed direct state therefore cannot roll the revision back and revive an
old visual scope. Repeated drift invalidation advances once when current bridge
authority is first suspended, not again for the same already-suspended state.

Bridge UUID change or native endpoint/credential/generation change deletes the
Lab-held bridge secret and requires fresh pairing. Build, add-on, protocol or
Kodi drift advances bridge revision and requires explicit review. Screenshot
capability loss advances revision and denies capture. Extra advertised
capabilities are recorded but production policy verifies only `screenshot`.
Bridge-only failure leaves native inspection effective; native drift invalidates
all native and visual bindings.

`mcp_production_target_scopes` now admits separate immutable `inspect` and
`visual` rows. Visual binds exact client, target, native generation and current
bridge revision and requires a current inspect row both when granted and used.
Inspect revocation atomically terminates dependent visual rows. The immutable
`Production Visual Inspection` built-in has exactly `target.inspect` and
`target.visual`, is production-only and non-clonable, and is the only production
screenshot preset. Inspection-only clients may still inspect a target assigned
the visual preset through the safe effective intersection.

The existing `screenshot_capture` becomes the fourth production-eligible tool
without changing its adapter identity or read-only classification. Its sole
schema addition is optional `request_id`; production requires it while managed
and external-development remain backward compatible. Complete preflight and
fresh post-capture checks intersect credential, exact presets, generic grant,
inspect and visual scopes, current generations, production/native state,
reviewed paired reachable bridge, screenshot capability and current HTTP
acknowledgement. Authority loss during capture suppresses pixels.

Client-scoped durable production replay binds request UUID, target and canonical
parameters before dispatch. Replay lookup precedes one-per-30-second
client-target admission and the one-global-production-capture gate. Exact live
replay returns identical memory-cached pixels without another bridge capture;
terminal identity whose pixels are unavailable returns
`screenshot_replay_unavailable`. Pending/ambiguous identities cannot be evicted
or converted into fresh capture. Existing per-target bridge serialization and
bridge protected replay semantics remain authoritative.

Each client receipt owns a distinct durable internal bridge request UUID, so an
equal caller UUID from another client cannot address or receive the first
client's cached bridge response. Failure to commit required terminal production
metadata discards the volatile pixels while retaining the protected bridge
identity, preventing both publication and recapture.

Kodi Lab stores no production pixels, base64, thumbnail, preview or raw bridge
response. Durable receipt/audit contains only bounded UUID/generation/revision,
dimensions, size, digest, outcome and timestamp metadata. The Web warns that
screenshots can contain personal/media information and the receiving MCP client
may persist them outside Kodi Lab.

## Slice 4A constrained production navigation

Migration `0050_production_navigation` extends the immutable scope vocabulary
with `navigate`. Each row binds exact client, target, native generation and
current reviewed bridge revision, requires active current inspect when created
and used, and is terminal after revocation. Inspect revocation atomically
revokes active visual and navigate dependents. Rollback of generation or bridge
revision cannot make retained history effective.

The exact built-in Production Remote Kodi UUID is activated as immutable,
assignable, non-clonable and production-only with maximum permissions
`target.inspect`, `target.visual` and `target.control`. It is not self-granting.
Inspection, screenshots and navigation still require their independent scope
intersections. Narrower Production Inspection or Production Visual Inspection
clients can safely use only their smaller intersection against a target
assigned the remote maximum.

The existing sixteen remote actions are centrally classified. Production
allows only `up`, `down`, `left`, `right`, `back`, `home` and `info`.
`context_menu` and `select` are prohibited, and `play_pause`, `stop`,
`previous`, `next`, `volume_up`, `volume_down` and `mute_toggle` are hard denied
until separately reviewed Slice 4B. External development remains unchanged.

Every new navigation claim binds current credential/client, exact presets,
generic grant, inspect/navigate scope UUIDs, native generation, bridge revision,
target policy revision, mutation epoch and applicable native-HTTP control
acknowledgement into the existing Phase 11 receipt. The required bounded audit
is created in the same transaction. Terminal exact replay is historical lookup
for the same enabled authenticated client; changed target/action conflicts and
pending/ambiguous work never redispatches.

Both mutation vetoes are checked before claim and after fresh bridge/native
preflight immediately before send. The bridge supplies authenticated identity
only and receives no control command. Native preflight uses Ping, protocol and
application identity plus exact semantic-method introspection under one
aggregate deadline, then sends one fixed mapped method without retry. HTTP
requires separate immutable Web acknowledgement that bridge HMAC does not
protect native JSON-RPC and HTTP adds no transport security. HTTPS retains
ordinary certificate validation.

Replay lookup precedes new-work rate charge. Navigation is two actions/second
per durable client-target with burst three, one mutation per target and four
globally beneath the eight-operation external-native ceiling. Admission is
immediate with no queue. Pending, ambiguous or incoherent production evidence is
protected and continues to consume capacity. Only a coherent completed base
receipt, binding and matching audit expire atomically after 30 days; cleanup
cannot prune uncertainty into a duplicate, and capacity exhaustion rejects new
work.

Web administration exposes only preset assignment, navigate grant/revoke,
dependency/revision/veto status and native-HTTP acknowledgement. There is no Web
remote button or MCP grant management. The production registry is exactly
`target_list`, `target_status`, `addon_list`, `screenshot_capture` and
`remote_action`; the other ten tools remain denied. Local/mock tests only were
used and no production Kodi or LAN host was contacted.

## Slice 4B one-shot transient production control

Production now classifies the seven playback/track/volume actions as exact
one-shot approval operations. The seven navigation actions remain standing;
`context_menu` and `select` remain prohibited. A new approval request requires
current inspect and `request_transient_control` scopes but neither navigate nor
visual. Inspect revocation atomically revokes every active dependent scope.

Each pending approval binds the initiating credential and immutable generic
grant UUID plus client authority, client/target presets, native generation,
bridge revision, policy revision, mutation epoch, exact scope UUIDs, reviewed
build and applicable native-HTTP acknowledgement. Client authority revision
advances only for enabled-state and preset changes. Target policy and global
mutation revisions are monotonic, so disable/re-enable, suspend/resume and
block/unblock cannot resurrect an old approval.

MCP creates a pending row and same-transaction requested event only after full
authority and veto admission. It creates no receipt, performs no mutation and
runs no dispatch preflight. Exact duplicate returns the same bounded
`approval_required`; changed target/action/digest conflicts. Web alone displays
the exact action and bounded current trust/veto state and may approve, deny or
cancel under the existing authenticated session, Trusted Host, Origin and CSRF
controls. Approval cannot succeed while either veto is set; denial/cancellation
remain available.

Pending expiry is at most fifteen minutes. Web approval creates at most a five-
minute dispatch deadline from that decision; neither deadline extends. Before
claim, approved retry performs authenticated bridge identity and read-only
native Ping/version/application/method preflight. Temporary reachability leaves
approval approved; definitive identity/build/protocol/capability or durable
authority change cancels it.

Consumption, base pending receipt, transient production binding, semantic
dispatch audit, consumed transition and durable admission commit in one SQLite
transaction. Only then may the adapter recheck immediate vetoes and send the
single fixed native mapping. Crash after commit but before send intentionally
leaves a protected receipt and never dispatches on retry. Response-loss
ambiguity also never redispatches; completed results replay historically across
modern HTTP, stdio and compatibility HTTP.

New approval admission is one/30 seconds/client-target, eight live/client and
128 live/global. Transient dispatch is one/5 seconds/client-target, burst one,
one target and one global in flight. Terminal history is bounded to
1,000/client and 10,000 global at approximately 30 days. Only coherent terminal
approval/receipt/binding/audit/event bundles expire atomically. Live, pending,
ambiguous, missing, malformed or mismatched evidence remains protected and
capacity-consuming. Durable audit contains bounded semantic identifiers,
digests, revisions, transitions and outcomes only—never secrets, endpoints,
raw JSON/JSON-RPC, responses, paths or pixels.

## Production bootstrap and deferred deployment

Production support-addon deployment is deferred post-V1 and is not a Phase 14
completion blocker. V1 uses a release ZIP with published checksum/provenance,
manually installed by the operator through ordinary Kodi procedures. Kodi Lab
then owns only Web reviewed-build registration, a one-time pairing token,
explicit installed provenance confirmation and revision-bound scopes. This
keeps installation and Kodi/host lifecycle under operator authority and keeps
the bridge an identity/screenshot component rather than an arbitrary control or
software-management channel. Production control still uses bounded native
semantic calls.

Any future bridge updater/deployer requires a new post-V1 architecture and
security gate. Automatic bridge upgrades and generic production add-on
deployment remain outside product authority. A hosted Kodi repository is
optional and is not required for Phase 14 or V1 production validation.

## Bridge 1.1.7 history and 1.1.8 candidate

The deterministic 1.1.7 archive includes the bridge README and `addon.xml`.
Editing either changes ZIP SHA-256
`a632f26ac7d6f2fb908577d4c75c9afc344ab138c1803f87132abefcf9c99a5c`.
Their wording is not an input to the hard-coded semantic fingerprint
`3f9eea79fcf03c3bd2d24e4aab8a61ffa98ed093e1c1185d9e9a3911d41b7486`,
but production reviewed-build identity binds both digests. The package README
explicitly says never to use 1.1.7 with production and `addon.xml` describes
external-development-only use. Consequently the existing 1.1.7 package/hash
remains exact Phase 13 review evidence but cannot remain the authoritative
corrected production Stage B artifact.

The minimal recommended transition is a packaging-only 1.1.8 successor with
synchronized descriptor/code version, recomputed semantic fingerprint and ZIP
checksum, independent package/source review and a new Web reviewed-build
record. That local successor is now built with no protocol, capability or
substantive executable-behavior change. Its exact ZIP SHA-256 is
`20a14873a46969cd3f1f962875a3905caa56ce6f088e95f6d3b2b8de6e666762`
and its semantic fingerprint is
`4a055b9479aeb72d2f8fe16d317ed525f69991908b28580d8a071be33aa26e5a`.
It has not been published, Web-registered, installed on production or used for
Stage A-D. Stage B must use these exact reviewed 1.1.8 bytes after acceptance.

## Validation and closeout preparation

The operator guide is [PRODUCTION-OPERATIONS.md](PRODUCTION-OPERATIONS.md).
The non-executed validation procedure is
[PHASE14-VALIDATION-RUNBOOK.md](PHASE14-VALIDATION-RUNBOOK.md), with the
redacted [PHASE14-EVIDENCE-TEMPLATE.md](PHASE14-EVIDENCE-TEMPLATE.md).

Phase 14 may close only after V1 boundary reconciliation, production/manual
bridge documentation, resolution and review of the exact production artifact,
later explicitly authorized successful Stage A-D execution, an independent
Phase 14 closeout audit and one final authoritative repository gate. Phase 14
remains **PARTIAL**. Slice 4B does not authorize deployment, execution,
lifecycle, shell, filesystem, SSH, raw JSON-RPC, live validation or Phase 15.
