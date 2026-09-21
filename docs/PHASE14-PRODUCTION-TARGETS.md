# Phase 14 — External Production Targets

## Slice 1 status and scope

Slice 1 establishes production representation and trust policy only. Phase 14
remains partial. No real Kodi or LAN host was contacted during implementation.

Included:

- immutable `external_production` target class;
- shared external endpoint, encrypted credential and native observation state;
- mandatory one-to-one production policy;
- separate Web-only native confirmation and production designation ceremonies;
- monotonic identity generation and explicit reverification;
- global default-blocked mutation control with monotonic policy epoch;
- per-target deny-only mutation suspension;
- semantic audit events; and
- one centralized MCP class veto shared by target and workspace-target paths.

Excluded:

- production MCP grants or tool eligibility;
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

The registry remains exactly fifteen tools. One central target-class veto runs
for direct target authorization and workspace-target authorization before
adapter dispatch. `external_production` is therefore unavailable for every
existing tool, including indirect workspace deployment, execution and
promotion. The dormant Production Remote Kodi preset remains unassignable and
has no effective access. Existing managed and external-development behavior is
unchanged.

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

## Remaining Phase 14 work

Later independently scoped work may add read-only production inspection,
reviewed bridge/screenshot architecture, constrained control and transient
approval semantics. Production support-addon deployment remains subject to a
separate architecture review. This slice does not authorize Slice 2 or any real
production contact.
