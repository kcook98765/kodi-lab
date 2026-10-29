# Explicit Python service enablement (#112, bounded slice)

The existing copied-source enablement route now accepts an opt-in for one Python
service. Default requests still refuse service startup. This does not implement
live replacement, service stop/reload, or a combined MCP apply operation.

## Admission and evidence

The Web administrator explicitly checks “Allow this service to start now and on
future Kodi starts while enabled.” The request contains a strict Boolean;
the server records `service_start_authorized` on the exact durable operation
before calling the existing literal-true `Addons.SetAddonEnabled` adapter.
This is operation intent, not a reusable grant. Existing source/deployment,
identity, native inventory, dependencies, reservations and reconciliation checks
remain required. No generic command, filesystem, disable or toggle input is added.

Admission requires a single non-metadata `xbmc.service` extension with a safe
relative Python library, no platform-library overrides, and the existing regular
library file in the verified installed manifest. The broker derives eligibility
from installed descriptor bytes during the existing complete digest-matched
readback; repairing the host descriptor at the same ID/version cannot qualify an
ineligible installed service. Both native inventory and details must identify a
service. Mixed or duplicate entrypoints remain unsupported by this slice.

The runtime policy requires stable Omega 21.3, a non-root runtime and SUPPRESS
autostart mode. This is a conservative admission policy, not qualification of
every image/platform matching those fields. The UI projection is advisory;
the server performs authoritative checks. A successful receipt says **enabled**
and `service_execution=not_observed`. Already-enabled services need no mutation.

Malformed RPC errors remain ambiguous; only a structurally valid correlated
JSON-RPC rejection is a definite rejection. Ambiguity retains durable intent and
the reconciliation blocker, with no automatic retry or restart workaround.

## Persistence and compatibility

Migration `0088_service_enable_start` adds a non-null false-default Boolean.
Existing operations acquire no service authority. Downgrade refuses while any
retained true row exists. Successful/failed operations retain the existing
twenty-operation pruning policy; this is not permanent audit retention.
Reconciliation-required operations remain outside pruning.

Known supported historical schemas read absent authority as false only after
checking the exact historical column layout, revision, lease vocabulary and
settings-storage era. Their unresolved operations still block. Missing safety
storage in current or unknown layouts fails closed. No running service database
was migrated during this campaign.

## Verification

FOCUSED PASS includes each changed test file, the full migration test file
(111 cases), exact schema-head contract, direct 0087 upgrade/default/downgrade
proof, installed-versus-host descriptor mismatch, malformed RPC uncertainty,
historical reconciliation and current-schema corruption. Focused Ruff and mypy
pass. The settled interaction gate is INTEGRATION PASS in 94.4 seconds, including
182 interaction cases after the focused phase. Independent review findings about
descriptor binding, predecessor readers and malformed errors were corrected
before final review.

The final native run used a fresh disposable container on the recorded approved
Omega r4 image (`sha256:8df4cbf1988514ac9713fab7702b0d5f04483f2a167ea95507d9ade66032403b`),
runtime revision `9ee490cd-8eee-424b-b3b2-06c2757ccaa8`, default Estuary,
1 GiB/1.5 CPU limits and a 360-second bound. It used the real application
enablement service with a private ORM-fixture catalog and native broker
adapters/RPC. Schema migration was tested separately. **This is not HTTP or
authenticated MCP acceptance, and does not upgrade the running Claw stack.**

| Generated service iteration | Copy/sync (s) | Start/readiness (s) | End to end including stop (s) | Payload |
| --- | ---: | ---: | ---: | ---: |
| Initial | 0.169 | 7.178 | 9.291 | 1,008,888 B |
| One-file edit | 0.096 | 6.685 | 8.594 | 421 B |
| Unchanged | 0.061 | 6.683 | 8.399 | 0 B |

Default service enablement was refused. Explicit initial enablement changed
native enabled state; later iterations were already enabled. Each full restart
produced the expected source nonce and a newly observed monotonic startup marker
after that boot, including the unchanged iteration. This proves the generated
service's startup smoke only, not arbitrary service health. The ordinary
application receipt correctly remains execution-unobserved. The fresh profile
was archived and the exact new container retired. Earlier harness setup failures
occurred before service mutation and their created container was also retired.

Full stopped deployment remains the copy path. Readiness dominates these small
native iterations. Live lifecycle qualification, exact obsolete-file removal,
other service implementations/platforms, dependent-module reload and the new
one-call agent executor remain separate campaign work. Final ZIP/repository
testing remains required.
