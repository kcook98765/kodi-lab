# Phase 13 — External targets

## Status

Phase 13 Slice 1 is complete. Phase 13 overall is **PARTIAL**.

Slice 1 implements Web-managed external-development registration, protected
credentials, native read-only Kodi verification, explicit confirmation,
enable/disable/deactivation and Web-only status. It does not authorize MCP use
of external targets, a bridge, screenshots, deployment, execution, lifecycle
control or production targets.

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
configuration and follows no redirects. The only requests are
`JSONRPC.Ping`, `JSONRPC.Version` and `Application.GetProperties` for name and
version. Connect, response and total time, response bytes, JSON depth, node
count and string sizes are bounded. Supported Kodi releases are exact 19.5,
20.5 and 21.3 plus the Kodi 22 compatibility line. Evidence claims endpoint,
observed Kodi application/version and JSON-RPC compatibility, not strong
machine identity.

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
`fixed_execution`. The native Slice 1 probe verifies only `target_inspect`.
Neither configured nor verified capability gives MCP authority in this slice.

## Web and MCP boundary

All status and action routes require the ordinary authenticated Web session;
mutations also require Trusted Host, same-origin and CSRF controls. Projection
contains safe structured metadata and never secrets or raw protocol/network
material. Audit records only semantic event and bounded classification.

The MCP lexical catalog remains exactly fifteen tools. MCP target listing,
status, add-on inventory, remote control, screenshot and development workflows
remain managed-target-only. Generic association foreign keys preserve current
managed UUID assignments but do not change application eligibility.

## Remaining Phase 13 work

- existing MCP read/control tools over verified external targets
- bridge pairing and stronger bridge identity
- screenshots
- workspace deployment
- fixed execution
- authorized LAN development proof
- formal Phase 13 closeout
