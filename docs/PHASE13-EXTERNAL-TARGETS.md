# Phase 13 — External targets

## Status

Phase 13 Slices 1, 2 and 3 are complete. Phase 13 overall is **PARTIAL**.

Slice 1 implements Web-managed external-development registration, protected
credentials, native read-only Kodi verification, explicit confirmation and
enable/disable/deactivation. Slice 2 allows only the existing `target_list`,
`target_status`, `addon_list` and closed semantic `remote_action` MCP tools to
use eligible targets. It does not add a bridge, screenshots, deployment,
execution, promotion, lifecycle control or production targets. Slice 3 adds the
optional authenticated screenshot-only bridge and admits eligible external
targets to the existing `screenshot_capture` tool. Deployment, execution,
promotion evidence integration, external lifecycle control and production
targets remain absent.

## Data and state

Every registration owns an existing `targets` UUID and one
`external_kodi_targets` subtype. `external_production` is absent from the closed
database enum. Administrative state (`disabled`, `enabled`, `deactivated`),
verification state (`unverified`, `verified`, `needs_reverification`,
`identity_mismatch`) and observation state (`reachable`, `unreachable`,
`authentication_failed`, `capability_missing`) are independent. Observations
and semantic audit events are durable and bounded.

Registration starts disabled and unverified. A successful probe creates a
pending observation but cannot enable the target. The administrator must
confirm that exact observation before enable. Endpoint or credential changes
require the target to be disabled and transition it to re-verification.
Important identity drift produces `identity_mismatch` until an administrator
confirms the new observation. Deactivation retires credentials and target
policy/grant assignments while preserving UUID and history.

## Endpoint and native verification

The endpoint schema is `jsonrpc_http` or `jsonrpc_https`, canonical IP literal
and one server-allowed port. Slice 1 intentionally does not resolve hostnames;
therefore mixed DNS answers and rebinding are unrepresentable. Default policy
allows RFC1918 IPv4 except managed Lab space. IPv6 requires an explicitly
configured ULA prefix. Public, loopback in normal mode, link-local, multicast,
unspecified, reserved, documentation, benchmark, CGNAT, metadata, Lab-managed
and IPv4-mapped IPv6 addresses are rejected. Test loopback is an explicit
off-by-default configuration.

The server constructs `/jsonrpc`, uses POST, ignores environment proxy
configuration and follows no redirects. The base requests are `JSONRPC.Ping`,
`JSONRPC.Version` and `Application.GetProperties` for name and version. Slice 2
may additionally make one bounded read-only `Addons.GetAddons` capability probe
and exact read-only `JSONRPC.Introspect` probes for the complete closed semantic
remote method set. It never sends a remote mutation to establish capability and
never infers support from Kodi version. Connect, response and total time,
response bytes, JSON depth, node count and string sizes are bounded. Supported
Kodi releases are exact 19.5, 20.5 and 21.3 plus the Kodi 22 compatibility line.
Evidence claims endpoint, observed Kodi application/version and JSON-RPC
compatibility, not strong machine identity.

HTTP is private-LAN-only and password authentication produces a Web warning.
HTTPS uses normal CA validation against the IP literal. Self-signed/SPKI pinning
is deferred; there is no certificate-verification bypass.

The bridge reuses the same IP-literal/address-class policy with a separate
server allowlist for its structured port. The server owns the fixed
`POST /kodi-lab/bridge/v1` path, follows no redirect, ignores ambient proxies
and never accepts a caller URL or path. HTTP HMAC authenticates request and
response integrity, but does not encrypt the PNG; the Web UI explicitly warns
that HTTP screenshots are cleartext on the LAN. HTTPS retains normal
certificate validation with no `verify=false` option.

## Secrets and capabilities

No-auth and username/password are the only authentication modes. Credentials
are encrypted with AES-256-GCM in UUID-named files beneath a `0700` directory;
files are `0600`, no-follow and atomically published. SQLite stores only the
opaque reference. The base64url 256-bit master key and positive key version are
deployment settings outside SQLite. Missing or wrong keys fail closed, rotation
publishes a new ciphertext/reference and retires the old file, and deactivation
retires the active file.

Configured maximum capabilities use the closed vocabulary `target_inspect`,
`addon_inventory`, `semantic_remote`, `screenshot`, `workspace_deployment` and
`fixed_execution`. Native verification establishes `target_inspect`, may
establish `addon_inventory` with its bounded read-only inventory probe, and may
establish `semantic_remote` only by proving every fixed method through
read-only introspection. Missing or partial proof leaves a capability
unverified. Effective MCP use remains configured intersected with verified;
neither field independently grants authority.

Bridge tokens and request-authentication secrets use the same protected AEAD,
no-follow and atomic-file facility in a separate `external-bridges` directory.
SQLite stores only opaque UUID references. A pairing token is random 256-bit,
target/purpose bound by AEAD, shown once, valid for ten minutes and retired
after one successful mutual possession proof. The bridge changes no state and
does not clear the token until the initiating Lab request authenticates. The
visible token is not the long-lived
secret: Lab and bridge derive that secret from token, target UUID, persistent
bridge UUID and fresh server/bridge nonces, then derive separately labelled
request and response HMAC keys. Rotation and revocation retire prior Lab secret
files.

## Slice 3 bridge protocol and identity

Migration `0045_external_bridge_pairing` adds independent bridge pairing state,
bounded replay receipts and content-free semantic events. Pairing state is
`unpaired`, `pairing_pending`, `paired`, `needs_reverification` or `revoked`;
reachability is independently `unknown`, `reachable`, `unreachable`,
`authentication_failed`, `identity_mismatch` or `protocol_unsupported`.
Populated state blocks destructive downgrade.

The new Kodi Python service add-on uses only standard-library cryptography and
ordinary Kodi APIs compatible with 19.5, 20.5, 21.3 and the Kodi 22 line. It
generates and preserves one random bridge UUID at first initialization.
Protocol 1 reports that UUID, protocol, add-on ID/version, Kodi version, exact
`screenshot` capability and canonical package/build fingerprint. Its closed
operations are `bridge_identity`, `bridge_capabilities` and
`capture_screenshot`; pairing is the only bootstrap exchange. It has no generic
RPC, caller path, filesystem, log, add-on administration, deployment,
execution, restart, profile or shell authority.

Every authenticated request HMAC covers target UUID, bridge UUID, protocol,
operation, request UUID, timestamp, 256-bit nonce and canonical payload digest.
Every response HMAC covers the corresponding identities, operation, request,
timestamp, nonce, status and payload digest. Clock skew is fixed at sixty
seconds and request UUID/nonce uniqueness is still mandatory. The bridge keeps
at most 256 durable replay identities for 24 hours and four exact response
receipts for 120 seconds. Kodi Lab keeps at most 256 durable screenshot receipts
per target and 4,096 globally for 24 hours and removes only expired terminal
rows; pending or ambiguous identity is never evicted to recover capacity.

Pairing begins only for an enabled, native-verified target with screenshot in
its configured maximum. The administrator generates and locally enters the
show-once token, asks Lab to perform the possession proof, inspects the observed
identity, and explicitly confirms the exact observation. Active use requires
the pinned bridge UUID/protocol/add-on/Kodi/build identity, unchanged endpoint
revision, verified `screenshot` capability and reachable state. Drift clears
effective screenshot capability and moves the bridge to
`needs_reverification`; authenticated Web reverification must reproduce the
pinned identity.

## External screenshot semantics

`screenshot_capture` retains its exact target-only schema and is still one of
fifteen tools. Effective external screenshot authority is configured maximum
intersected with native verified state, paired/reachable/pinned bridge,
bridge-verified screenshot capability, exact client target grant, preset
policy, `target.visual` permission and external-development class eligibility.
Pairing is irrelevant to native-only target list/status, inventory and remote
control.

The add-on calls Kodi RenderCapture with the server-owned 1280x720 geometry,
receives fixed BGRA bytes, converts them in memory to one RGB, 8-bit,
non-interlaced PNG and returns bytes directly. It never asks Kodi to write a
path and never reads one back. Lab applies the existing strict PNG parser: exact
signature and `IHDR`/`IDAT`/`IEND` layout, checksum, encoding, geometry,
decompressed length and filter, with an 8 MiB encoded maximum. No animation,
metadata or multi-frame content is accepted.

Screenshots are not persisted by the external path. Audit contains only target,
bridge and request UUIDs plus bounded outcome, byte count, dimensions and
SHA-256 digest. It never contains token, secret, signature or pixels. A
transport-loss retry repeats the exact request UUID, nonce, timestamp and HMAC;
the bridge returns the cached identical PNG or rejects the replay. A second
capture is never used to hide ambiguity.

## Web and MCP boundary

All status and action routes require the ordinary authenticated Web session;
mutations also require Trusted Host, same-origin and CSRF controls. Projection
contains safe structured metadata and never secrets or raw protocol/network
material. Audit records only semantic event and bounded classification.

Bridge status, token generation, possession-proof attempt, identity
confirmation, reverification, needs-reverification marking and revocation are
also Web-only. Token rotation, revocation and reverification use strict
confirmation bodies; the generated token exists only in the creation response
and is never recoverable from status or HTML. Deactivation revokes bridge
authority before retiring the native target.

The MCP lexical catalog remains exactly fifteen tools. Registration,
configuration, credentials, probe, confirmation, enablement, target policy and
client target grants remain Web-admin-only. MCP receives no endpoint, hostname,
port, username/password, secret reference, raw verification evidence or raw
JSON-RPC surface.

For each external use, authority is valid bearer credential, enabled client,
tool permission, exact common target grant, target preset policy, supported
external-development class, enabled and verified state, identity validity,
configured capability maximum and verified technical capability. Cross-client
UUID guessing therefore follows the same unavailable result as a nonexistent
target. Read-only Inspection naturally permits discovery, status and inventory
when the rest of the intersection passes, but not remote control. Add-on
Development and Full Lab Control can use the four tools only within the same
intersection; broader presets do not override class or capability restrictions.

One semantic target-adapter service selects the existing managed services or
the external native adapter below the registry and authorization layer.
`target_list` uses confirmed stored state and keeps an authorized target visible
during temporary unreachability. `target_status` performs an on-demand Ping and
identity check. `addon_list` performs current identity plus fixed paged
`Addons.GetAddons` and preserves existing bounds/schema. `remote_action`
performs a fresh identity check before the unchanged closed action mapping.
There is no background poller.

The existing Phase 11 durable client/request receipt is committed before
external dispatch. After the fresh identity check, current permission, grant,
policy, class/state and capability are re-authorized immediately before the
mutation. Completed duplicates replay; changed parameters conflict; pending or
post-send response loss remains ambiguous and never redispatches. A proven
connect failure is a pre-send failure. Once bytes may have reached Kodi,
failure is conservative ambiguity. Native operations are bounded to eight
globally and one per target, with at most 1,024 target gates.

Modern HTTP, modern stdio and bounded legacy HTTP enter the same registry,
authorization, adapter and receipt. External screenshot uses this shared path
and returns the existing metadata plus official image content. Discovery and
schema remain unchanged. Workspace deployment, workspace execution and
promotion remain internal-managed-only and reject an external UUID without
disclosing unauthorized existence.

## Remaining Phase 13 work

- workspace deployment
- fixed execution
- promotion evidence integration
- authorized LAN-development proof
- formal Phase 13 closeout
