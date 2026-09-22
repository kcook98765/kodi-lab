# Phase 13 — External targets

## Status

**Phase 13 — Remote targets: COMPLETE.** Independent formal review accepted
Slices 1 through 5 after the narrow D-0302 replay/deadline remediation.

Slice 1 implements Web-managed external-development registration, protected
credentials, native read-only Kodi verification, explicit confirmation and
enable/disable/deactivation. Slice 2 allows only the existing `target_list`,
`target_status`, `addon_list` and closed semantic `remote_action` MCP tools to
use eligible targets. It does not add a bridge, screenshots, deployment,
execution, promotion, lifecycle control or production targets. Slice 3 adds the
optional authenticated screenshot-only bridge and admits eligible external
targets to the existing `screenshot_capture` tool. Slice 4 admits exact
bridge-backed external deployment, fixed native execution and promotion
evidence through the existing Phase 12 tools. Slice 5 supplies the authorized
Kodi 20.5 LAN proof and compatibility/network hardening. External lifecycle
control and production targets remain absent.

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
receipts for 120 seconds. A live screenshot identity is never evicted: protected
capacity exhaustion rejects new work. Read-only and content-addressed deployment
entries may be displaced only because exact replay is independently idempotent
and conflicting reuse fails closed; legacy unclassified entries remain
protected until expiry. Kodi Lab keeps at most 256 durable screenshot receipts
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

Kodi 20.5 live proof established that `RenderCapture` is video-renderer-only
and cannot capture Home or other GUI screens. The corrected add-on invokes
`TakeScreenshot(<bridge-owned-path>,sync)` with an unpredictable name beneath
the fixed `special://temp/kodi-lab-bridge` namespace. Neither MCP nor Lab can
provide any path component. The bridge bounds file appearance, refuses unsafe
file identities, checks encoded size before bounded readback, validates and
normalizes the GUI image to one RGB, 8-bit, non-interlaced PNG, and deletes the
exact transient in `finally` before response construction. Lab applies the
strict normalized PNG parser for checksum, bounded observed geometry, decoded
length and filter under the 8 MiB encoded maximum. No animation, retained
metadata or multi-frame content is accepted.

Kodi 19.5, 20.5 and 21.3 implement the requested synchronous write. Kodi 22
accepts the built-in but ignores its removed `sync` option; the bridge's same
two-second deadline retries only the exact generated candidate until a complete
valid PNG is available. Timeout remains a bounded capture failure.

Crash residue is limited by a five-minute TTL, a 64-entry direct-child scan and
at most 16 deletions per pass. Only strict bridge-generated capture names are
eligible; cleanup never recurses or removes unknown files. Capture, validation,
size and cleanup failures are authenticated bounded classifications distinct
from bridge authentication failure. This local fixed lifecycle is not generic
filesystem authority.

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
schema remain unchanged.

## Slice 4 bridge-backed development deployment

**Slice 4 is COMPLETE; Phase 13 remains PARTIAL.** Migration
`0046_external_bridge_development` generalizes the Phase 12 deployment and
promotion-plan evidence shapes and adds only external source baselines and
bridge receipt correlation. It does not create a parallel external workflow.
The unchanged `workspace_deploy`, `workspace_execute` and `promotion_request`
schemas admit `external_development` only through the full client permission,
workspace ownership, source grant, target grant/policy, enabled native trust,
paired bridge trust, configured/verified capability, generation, validation
and source-provenance intersection.

Protocol 1 is additively compatible. A Slice 3 bridge that advertises only
`screenshot` remains valid for screenshots. A Slice 4 bridge explicitly adds
`workspace_deployment` and the closed `inspect_addon`, `deployment_begin`,
`deployment_chunk`, `deployment_commit` and `deployment_receipt` operations.
The bridge does not advertise `fixed_execution`: execution stays on the native
JSON-RPC adapter because the existing fixed `Files.GetDirectory` video-plugin
smoke is sufficient and avoids bridge execution authority.

Lab captures the exact protected workspace generation into a private temporary
tree. The canonical Phase 9 manifest permits only normalized relative regular
files/directories, caps each file at 2 MiB, the manifest at 16 MiB, a chunk at
256 KiB, files at 25,000, entries at 50,000 and included bytes at 1 GiB. It
excludes traversal, links, special objects, VCS administration and Python
bytecode caches. Each chunk is HMAC-authenticated and accepted only for an
already authorized deployment UUID and referenced manifest file. No request
contains the Kodi add-on destination. Lab serially paces transfer at no more
than 16 MiB/s and fails an invocation that exceeds two hours; one deployment is
active per target and the existing global heavy gate admits two transfers.

Kodi's external Python runtime may create `__pycache__` after fixed execution.
Installed-tree inspection therefore applies the same bytecode-cache exclusion
without weakening source provenance: it skips only a real direct
`__pycache__` directory containing bounded single-link regular `.pyc` files,
does not recurse beneath it, and rejects links, nested directories, unknown
files and over-limit caches. Direct `.pyc` and every non-cache destination
addition still fail closed. This canonical exclusion is not filesystem
authority, cleanup or adoption of external source drift.

The bridge derives the destination from its local Kodi add-on root and the
descriptor add-on ID. It stages into a bridge-owned directory, fsyncs file
content, re-hashes the full tree, confirms the copied Python video-plugin
descriptor, preserves enabled state, moves the prior tree to one bounded
rollback copy, replaces the directory, re-enables if needed and checks final
tree plus Kodi inventory. Failure after mutation attempts rollback; proven
rollback returns failed, while an unprovable final state remains replacing and
requires reconciliation. No process, host, profile or container lifecycle is
owned by the bridge.

Bridge deployment replay is independent of Lab replay. The Lab client/request
map prevents duplicate semantic dispatch across HTTP, stdio and legacy HTTP.
The bridge binds deployment UUID, MCP request UUID and exact begin fingerprint;
repeated chunks must match existing bytes, exact terminal commit replay returns
the same receipt, and conflicting reuse is rejected. Begin/chunk transfer may
retry idempotently before activation. Commit is attempted once. A lost commit
response is recovered only by receipt lookup and never by a second replace.

Before first use, Web must establish a DevelopmentSource baseline by comparing
the registered source capture with bridge add-on ID/version, tree digest, file
count and bytes. MCP cannot attach, establish or rebaseline. Each operation
compares the current bridge tree with the baseline or last successful exact
deployment. External change marks provenance stale, emits a content-free event
and blocks deployment, execution and promotion evidence.
After the operator restores the disposable target to the exact registered
source, a newer successful Web baseline supersedes older deployment lineage;
only a deployment completed after that explicit trust decision advances the
expected tree again.

Successful external deployment occupies the existing Phase 12 evidence row
with target class, bridge/request/receipt UUIDs, manifest digest and counts.
`workspace_execute` requires that exact evidence, current native inventory and
current bridge tree, then uses the unchanged fixed native smoke and Phase 12
at-most-once dispatch marker. `promotion_request` accepts the resulting exact
validation/deployment/execution evidence and produces one Web-review plan; MCP
still cannot approve or apply it. Existing managed deployment, execution and
promotion behavior is unchanged.

The Web-only baseline endpoint and deployment reconciliation endpoint require
the normal authenticated session, Trusted Host, same-origin and CSRF controls.
Reconciliation queries only the exact bridge receipt and accepts only matching
terminal evidence; there is no retry button. Disablement, deactivation or
bridge revocation denies new work immediately while operation history and
unresolved reconciliation remain visible.

Audit contains UUIDs, generation, semantic classification, manifest digest and
counts. It contains no file contents, destination/staging/backup path, secret,
HMAC, raw JSON or raw Kodi response. The bridge keeps at most 64 deployment
records, one active deployment per target, two Lab transfers globally and one
immediate rollback copy. Abandoned staging bytes become retained failed
receipts after 24 hours; terminal/unresolved receipt identity is never evicted
merely to recover capacity.

## Slice 5 authorized LAN proof

One dedicated Kodi 20.5 development target proved the complete registration,
native verification, exact client grant, pairing, GUI screenshot, source
baseline, deployment, fixed execution, promotion-evidence, drift rejection and
reconciliation paths over a real private-LAN connection. Native request replay
dispatched one Home action; cross-protocol deployment replay performed one
replacement; fixed-execution replay performed one invocation; and promotion
replay produced one review plan. Screenshot evidence retains only validated
geometry, encoded size and digest, never pixels.

Controlled source drift made the baseline stale and blocked deployment,
execution and promotion without adopting external bytes. After an out-of-band
maintainer restored the disposable fixture, a newer authenticated Web baseline
became the trust anchor and deployment recovered. A temporary endpoint change
and protected credential rotation each forced reverification and immediate MCP
denial. A maintainer-side Kodi restart produced the bounded
reachable/unreachable/reachable transition without adding lifecycle authority.
Bridge revocation removed only bridge capabilities while native trust remained
usable, and re-pairing required the normal possession and confirmation flow.

The LAN proof exposed and fixed narrow implementation defects without a schema
change: fresh migrated databases needed explicit parent flushes before target
subtype and pairing-event insertion; the Kodi 20.5 settings UI needed the
version-1 structured settings schema plus packaged localization; GUI capture
needed D-0299's bounded transient `TakeScreenshot` lifecycle; Kodi 20.5 add-on
inspection rejected a newer invalid `type` request property; and fixed
execution required D-0300's narrow interpreter-cache exclusion. D-0301 orders
a newer explicit Web re-baseline ahead of older successful deployment lineage.
Bridge releases 1.1.1 through 1.1.6 preserved protocol authority while applying
the incremental packaging, GUI capture, Kodi 20.5 inspection and provenance
corrections. The live-validated package is 1.1.6. Its deterministic ZIP
SHA-256 is `5a7707dacb92a2e6f576f97f990c1b743dbaf72d476538fd8049159745a086f3`;
its semantic build fingerprint is
`ab36d89415065dc9ff62abec480adaa7557324c15fc62e35d897da6a8741f9d7`.
These intentionally identify different concepts: exact archive bytes versus
the embedded release/protocol/capability identity.

Temporary direct SSH was used only as out-of-band maintainer setup to restore
the disposable fixture, enter a one-time bridge token and restart Kodi. Those
actions are not Kodi Lab product authority and add no MCP, bridge, filesystem,
installation or lifecycle surface. No other LAN host or production Kodi target
was contacted. This proof validates Kodi 20.5 only; deterministic compatibility
coverage remains authoritative for 19.5, 20.5, 21.3 and the Kodi 22 line.

## Formal completion review

The independent review reproduced one completion blocker in bridge 1.1.6: more
than 256 fresh authenticated messages could evict a still-fresh screenshot
request identity after its four-entry response cache rolled over. A captured
signed request could then invoke a second GUI capture. D-0302 fixes this in
bridge 1.1.7 by making live screenshot and legacy-unclassified replay entries
non-evictable, failing closed if protected capacity fills, and displacing only
operations whose handlers independently provide exact idempotence and conflict
rejection. The same remediation enforces the native adapter's documented total
deadline and rejects screenshot hardlinks. It changes no database migration,
protocol version, capability, MCP schema or tool count.

The deterministic 1.1.7 closeout package has ZIP SHA-256
`a632f26ac7d6f2fb908577d4c75c9afc344ab138c1803f87132abefcf9c99a5c`
and embedded semantic build fingerprint
`3f9eea79fcf03c3bd2d24e4aab8a61ffa98ed093e1c1185d9e9a3911d41b7486`.
Its archive contents exactly match the reviewed bridge source. It was not
installed on the LAN target; the retained live claim remains Kodi 20.5 with
1.1.6, while the remediation is covered deterministically.

Phase 13 is closed. External-production support, policy, approvals and
activation remain deferred to Phase 14.

Phase 14 Slice 1 now reuses only the external endpoint, protected-secret and
native-observation mechanics beneath a distinct immutable
`external_production` class and mandatory production policy. It does not change
the completed Phase 13 external-development authority, bridge protocol,
workspace semantics or live compatibility claims. Production remains excluded
from MCP and from every Phase 13 bridge/deployment path.

Phase 14 Slice 3 later reuses this exact bridge v1 protocol under a separate
production policy layer. Its implementation proof used the known final 1.1.7
identity, but Phase 14 closeout preparation found that the packaged README and
`addon.xml` describe external-development-only use. Correcting those archive
bytes changes the reviewed ZIP digest, so 1.1.7 remains exact Phase 13 evidence
and is not the corrected production distribution. A separately reviewed
packaging successor is required before production Stage B; 1.1.8 is the minimum
recommended transition. Operators must explicitly register the new reviewed
ZIP/fingerprint provenance in Web administration, enter a new show-once token
locally, inspect the observed bridge identity, and make the separate installed-
production-target confirmation. Migration never pre-approves a build, and
production policy ignores `workspace_deployment` capability.
