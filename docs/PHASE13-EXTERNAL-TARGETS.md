# Phase 13 — External targets

## Status

Phase 13 Slices 1 and 2 are complete. Phase 13 overall is **PARTIAL**.

Slice 1 implements Web-managed external-development registration, protected
credentials, native read-only Kodi verification, explicit confirmation and
enable/disable/deactivation. Slice 2 allows only the existing `target_list`,
`target_status`, `addon_list` and closed semantic `remote_action` MCP tools to
use eligible targets. It does not add a bridge, screenshots, deployment,
execution, promotion, lifecycle control or production targets.

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

## Web and MCP boundary

All status and action routes require the ordinary authenticated Web session;
mutations also require Trusted Host, same-origin and CSRF controls. Projection
contains safe structured metadata and never secrets or raw protocol/network
material. Audit records only semantic event and bounded classification.

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
authorization, adapter and receipt. Screenshot, workspace deployment,
workspace execution and promotion remain internal-managed-only and reject an
external UUID without disclosing unauthorized existence.

## Remaining Phase 13 work

- bridge pairing and stronger bridge identity
- screenshots
- workspace deployment
- fixed execution
- authorized LAN development proof
- formal Phase 13 closeout
