# Phase 14 — External Production Targets

## Slice 1 and Slice 2 status and scope

Slice 1 establishes production representation and trust policy only. Phase 14
Slice 2 adds bounded read-only inspection. Phase 14 remains partial. No real
Kodi or LAN host was contacted during either implementation.

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

Excluded:

- bridge pairing, screenshots or navigation/control;
- `remote_action` and production approvals;
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
execution and promotion stay unavailable. The dormant Production Remote Kodi
preset remains unassignable and has no effective access. Existing managed and
external-development behavior is unchanged.

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
assignment, and adds bounded scope/generation fields to semantic MCP events.
Downgrade refuses when scope history, new-preset assignments or scope events
would be lost.

## Remaining Phase 14 work

Later independently scoped work may add reviewed bridge/screenshot architecture,
constrained control and transient approval semantics. Production support-addon
deployment remains subject to a separate architecture review. Slice 2 does not
authorize Slice 3 or any real production contact.
