# Managed Bridge preparation architecture

Baseline: `ee66aae1545829c5dc4f94d1ea87ede861d5f74f`.
Implementation issue: https://github.com/kcook98765/kodi-lab/issues/300.
Owner request: https://github.com/kcook98765/kodi-lab/issues/299.

## Existing ownership and the selection dependency

Web Create has durable installation, registration, provisioning, startup and
native-readiness adapters. In the baseline, the default stopped path ends after provisioning;
it does not start Kodi or prove native readiness. Optional startup uses
`start_managed`, with a single-use, exact-container Start proof. Native readiness
is explicitly independent of Bridge readiness.

`ManagedBridgeTrustService` and `DockerManagedBridgeTrustProvider` implement
reviewed staging, encrypted credentials, fixed-path materialization, native
add-on verification and authenticated resident identity. The supported installer
uses a typed START lease around staging, Stop and Start. START admission, trust
admission, and broker Start admission all inherit development-session selection.
Consequently Nexus can pass native readiness through ordinary Managed Start and
still fail Bridge admission before any operation is claimed while Hub is selected.

Selection is both a development authorization boundary and an implementation
coupling in this administrative maintenance path. Removing the guard globally,
changing selection temporarily, copying trust, or reusing a consumed Start proof
would weaken existing guarantees. Development-agent and external paths must keep
their existing admission and permission checks.

## Corrected flow and authority

New Web Create records an explicit Bridge-required intent. Existing native-only
receipts retain their original meaning and are never silently upgraded. All new
Create requests prepare native Kodi even when the requested final state is
stopped. After native readiness, a separate durable maintenance receipt binds the
exact target, runtime, image and container. Ordinary Start, maintenance restart,
and final Stop have distinct deterministic child identities.

Maintenance reuses ordinary Managed Start admission and its exact-container,
lease-bound, single-use proof for its one internal restart. Trust preparation
accepts this authority only while that matching typed operation and lease are
live. It does not relax development-session admission. The maintenance owner
waits for native readiness, stages only the reviewed build, gracefully stops and
starts the same container when needed, then authenticates the resident Bridge
and verifies native installed/enabled evidence. Valid current trust is checked
and reused without staging, credential churn or restart. Final Stop has its own
receipt; preparation is complete only after the requested final state is proven.

An authenticated Web administrator may explicitly prepare/repair an existing
exact managed target through the same coordinator. No source, workspace, project
selection, profile replacement, add-on fixture, MCP credential, preset, target
policy or grant is part of this operation. Retained invalidated trust requires
this explicit maintenance intent; ordinary Start does not silently repair it.

## Compatibility

| Managed runtime | Reviewed build | Semantic fingerprint |
| --- | --- | --- |
| Matrix 19.4 | stable 1.1.31 | `e18b4176611506335316a41e4dee9ea2ed113f8b5e7a0b95f6c156597d1618be` |
| Nexus 20.5 | stable 1.1.31 | `e18b4176611506335316a41e4dee9ea2ed113f8b5e7a0b95f6c156597d1618be` |
| Omega 21.3 | stable 1.1.31 | `e18b4176611506335316a41e4dee9ea2ed113f8b5e7a0b95f6c156597d1618be` |
| Piers 22.0-rc.1 | preview 1.1.32 | `54bd62b63902fad5ad3528039b8ce36d2ec4ef7ed1dad68aa2020761beea418a` |

The stable package's Python API dependency and runtime eligibility cover Matrix,
Nexus and Omega. Piers uses the separately reviewed RC1 sources, descriptor and
provenance through `packaged_rc1_bridge_sources`. Stable 1.1.31 is not the approved
Piers artifact. Keep the existing version-specific selector; never substitute a
build based only on its add-on version string.

## Recovery and concurrency contract

Persist intent before dispatch and claim each child atomically with its lifecycle
lease. Lost responses and browser refresh read the original receipt; GET never
mutates. An accepted/dispatched child must never be dispatched a second time.
Identity drift, unproven staging, interrupted restart, expired ownership and
ambiguous native outcomes fail closed with durable recovery history. Preserve
the successful runtime installation and profile. A prepared trust row is intent,
not proof that staging or restart succeeded.

Maintenance uses existing Start capacity, reconciliation, mutation, remote-input,
viewer and reservation admission. Start/Stop/Reset cannot interleave with its
lease. Separate phases recheck exact binding and runtime observation revision;
an intervening action requires review instead of silently rebinding the request.
Trust activation never grants agent access. Stopped completion retains historical
native/trust evidence and must not advertise live Bridge availability.

## Acceptance boundary

This section records the original draft campaign's boundary. The final review and
application integration are separately authorized; native acceptance remains pending.

Focused fixture tests must cover version selection, fresh installation, valid
no-op, invalidated retained trust, stopped/default and optional running outcomes,
selection preservation, concurrent admission, interrupted dispatch and replay,
identity drift, staging/restart/verification failures, retained profile contents,
and accessible progress/recovery. No full regression, live lifecycle, production
deployment, merge or release acceptance is authorized for this work.

Native acceptance remains separate: isolated disposable profiles for each exact
runtime/build, real stage/readback, restart/authentication and final graceful Stop;
retained Nexus repair and Hub preservation require a later owner-directed run.

## Implemented Web and retained recovery

The coordinator is `application/managed_bridge_preparation.py`; positive trust
admission is `storage/bridge_preparation.py`. The additive
`0008_managed_bridge_preparation` migration stores immutable parent identity,
original child attempts and Bridge-required Create intent. It preserves every
existing native-only intent as `bridge_required=false`. Child history is excluded
from lifecycle pruning and protected by foreign keys and immutable linkage.

The normal workflow has no additional checkbox or Bridge approval. Web Create
always prepares native Kodi for new Bridge-required requests, then continues its
separate maintenance receipt. The browser's exposed version includes both
ledgers, so competing stale continuations cannot advance a later phase. A native
Complete checkpoint alone does not produce overall Prepared. Default success has
one provisioning, two native Starts, two graceful Stops and one reviewed staging;
optional running completion omits the final Stop. Each native Start has distinct
single-use authority. A current authenticated Bridge reuses its generation and
credentials, skips staging and skips the internal restart.

The existing Advanced instance page offers **Prepare or repair Bridge** for a
retained target; it can start a stopped instance without selecting its project.
It retains the original saved request on refresh/lost responses and offers
**Continue saved Bridge recovery** only after typed history permits recovery.
A prior uncertain child stays fenced. Explicit retries append distinct child
attempts under the original parent; they do not repeat a dispatched child.

A changed container/runtime/image never rebinds an old receipt. Once prior
history is terminal and current owned runtime admission succeeds, the panel can
offer **Prepare current Kodi** as a separate explicit current-identity intent.
The original failed receipt and its binding remain intact. Unknown ownership or
unresolved recovery offers review only. This transition is not inferred from a
lost response. Successful native installations and profiles are never removed
by this coordinator: it has no provision, reset, deprovision, source or workspace
mutation path.

Web version cards direct empty slots to Create rather than allowing cached
Start/Reset to create a native-only profile. Existing retained Start/Stop and
Reset behavior otherwise remains, including trust invalidation after Reset and
explicit retained repair. Agent lifecycle semantics and grants are unchanged.

## Focused validation result

**FOCUSED PASS**. All new/materially changed collected test files ran individually;
focused affected application, broker, schema and Web files also passed. There
were **350 selected passing tests** across the following files (repeated runs
are not counted twice):

| Test file under `tests/` | Passing cases |
| --- | ---: |
| `application/test_managed_bridge_preparation.py` | 20 |
| `storage/test_bridge_preparation_migration.py` | 2 |
| `storage/test_managed_setup_migration.py` | 3 |
| `storage/test_migrations.py` | 60 |
| `application/test_managed_bridge_trust.py` | 24 |
| `application/test_retained_bridge_lifecycle.py` | 23 |
| `application/test_create_kodi_runner.py` | 36 |
| `broker/test_managed_bridge_trust.py` | 32 |
| `web/test_create_kodi_api.py` | 50 |
| `web/test_create_kodi_continuation.py` | 37 |
| `web/test_create_kodi_wizard.py` | 7 |
| `web/test_bridge_preparation.py` | 5 |
| `web/test_managed_slots.py` | 34 |
| `web/test_managed_integrated.py` | 17 |

`./scripts/check-focused` was used with explicit file selections. The application,
broker and schema lane passed in 101.8 seconds; the Web lane passed in 275.4
seconds. Subsequent bounded checks covered the final additional authority,
recovery and accessible-progress cases. Ruff format/lint and strict mypy also
passed on all 29 changed/new Python files, including application services,
models, migration, routes and fixture helpers. The existing Starlette/AnyIO
BlockingPortal deprecation warning is non-failing. No full regression suite,
package/release gate, Docker acceptance, runtime acquisition or live Kodi ran.

Coverage includes exact reviewed source inventories for all four runtimes;
stopped/default and optional running completion; active valid no-op; missing
Bridge and generation-6 invalidated Nexus trust; current-container activation;
failed staging, lost restart response and failed resident authentication;
interruption and original receipt replay; concurrent continuation/Stop exclusion;
wrong-target HTTP rejection and live maintenance ownership; explicit retry with
retained child history; changed-binding refusal and explicit current repair;
unchanged selected Omega session, profile files, clients and grants; default
stopped Web completion; original Create replay; admin/Origin/CSRF protection;
passive refresh and browser storage recovery; Bridge/native progress and error
presentation; responsive layout and accessible status/alert markup.

Browser tests use isolated Node controllers and served markup/CSS. They do not
establish visual desktop/mobile browser acceptance or native Kodi UI acceptance.
The provider tests use isolated fixed-path program/container doubles; four-version
application tests use actual sealed reviewed recipe fixtures with native/broker
transports replaced. The retained-selection test uses a synthetic policy catalog,
with real lifecycle, Start proof, selection and trust gates; it does not claim
real retained Nexus acceptance.

## Security and preservation review

Maintenance requires the target to remain Managed and owned, an eligible pinned
runtime, exact container/image identity, current projection, matching live typed
START lease, matching maintenance-child linkage and existing managed Start proof.
The proof is consumed once by the internal restart. Native installed/enabled
readback, exact reviewed fingerprint and authenticated resident identity are
required before success. Ordinary development and background paths retain their
selection admission. Current input/viewer, resource capacity, pending reservation
and reconciliation protections remain; uncertainty cannot be cleared by a
favorable inventory read. Final Stop dispatch is explicitly pinned to the saved
container as well.

No MCP permission, preset, target policy, grant, credential issuance, navigation,
general control, arbitrary Kodi command, Managed Reset authority, source/workspace
authority or external/production path was added. Trust provisioning uses the
existing encrypted secret store and closed reviewed broker installer. Neither
public API nor browser persistence contains Bridge secrets or restart proofs.

All source work is confined to the new worktree
`/home/kyle/kodi-lab-worktrees/managed-auto-bridge-20261111`. The original checkout
remains clean at `36520fd34badee085601d74e141c6a86431c2f4a`. GitHub main was rechecked
at the owner baseline `ee66aae1545829c5dc4f94d1ea87ede861d5f74f` before publication.
The installed Claw application was not changed or used for acceptance. No live
Kodi lifecycle, Nexus profile modification, Omega/Hub selection change, add-on
fixture deployment/launch, existing workspace change, credential/grant mutation,
Docker cleanup, merge, deployment or release preparation occurred.

Earlier Nexus report remains at its original path, SHA-256:
`05e344158c8f00bb96e00ef5caa0a1cc4e09cbb0f7c35ec99cc4c031326c9949`.
Its protected fixtures and evidence were not modified. This source/fixture review
does not upgrade that earlier failed native acceptance result.

## Remaining owner-directed acceptance

Before any operational acceptance claim, use separately authorized disposable
native profiles for Matrix 19.4, Nexus 20.5, Omega 21.3 and Piers 22.0-rc.1 with
the exact artifact above. Observe real installation/readback, native readiness,
Bridge recognition and enablement, supported restart, authenticated resident
identity/capabilities, same-profile persistence, final graceful Stop and optional
running completion. Exercise real interrupted staging/restart recovery, saved
receipt reconnect and running-slot contention without stopping unrelated Kodi.

Real retained Nexus repair is separately pending: preserve its profile/add-on/
settings inventories, prove current container/runtime trust, keep Hub selected
continuously and retain recovery history. No fixture deployment or launch is
implied. Desktop/mobile visual, keyboard/focus and live progress timing checks
also remain; controller/markup assertions are not those acceptance claims.

The delivery endpoint is the issue, this architecture/validation assessment and
one draft PR. Merge, deployment, issue closure and release/native acceptance
require later owner review.

## Final review of PR #301

The owner separately authorized final review and supported application integration,
with no live Kodi operations. New setup success is labelled **Prepared**; historical
native-only Create receipts retain their original Ready/Installed semantics.

Maintenance now reuses `ManagedResourceService` guards before claiming a child,
before staging, immediately before the internal Stop, and before final Stop. This
protects keep-awake holds, viewer/input ownership, reservations and reconciliation.
Each Stop additionally requires native no-playback evidence for the exact container;
unknown playback fails closed. Late protection failures after staging retain the
uncertainty barrier and child history. A rejected final Stop retains verified Bridge
evidence but cannot complete setup. A definite pre-dispatch failure can resume the
same parent after the protection is cleared; a lost Stop response needs typed
reconciliation before another attempt.

Active stored trust no longer prevents explicit repair of a missing, disabled or
wrong-version native Bridge. A matching native installation and authenticated
identity remain a no-op. Transport failures, malformed details and unrelated method
errors preserve active credentials and do not stage replacement bytes. Only a
well-formed, matching `GetAddonDetails` InvalidParams response indicates native
absence ([Kodi implementation](https://github.com/xbmc/xbmc/blob/Omega/xbmc/interfaces/json-rpc/AddonsOperations.cpp)).
The shared JSON-RPC adapter retains its existing invalid-response superclass while
exposing a typed method error code without retaining server error text. Browser
continuations reject changed request/operation identities or older receipt versions.

### Clean Reset decision

Clean Reset retains its explicit destructive consent, receipts, recovery and
**stopped clean profile** result. It invalidates earlier Bridge trust; the new
profile is **not Bridge-prepared**. Ordinary Start never repairs revoked or
invalidated trust. The Reset notice links to this exact instance's existing
**Prepare/Repair Bridge** panel, which owns the explicit maintenance request.

Automatic preparation after Reset would add native Starts, a restart and capacity
requirements to an existing destructive consent contract. It is deferred to
[issue #302](https://github.com/kcook98765/kodi-lab/issues/302), which requires
consent-aware composition with immutable Reset/maintenance receipts and recovery.
This PR does not claim every replacement profile is Bridge-ready.

### Final focused verification

The final review reruns 260 selected cases: maintenance 29, native readiness 68,
managed resources 14, existing trust 24, retained lifecycle 23, populated-predecessor
migration/downgrade 2, Web Bridge 5, managed slots/Reset 34, integrated Web 17,
Create continuation 37 and wizard 7. The earlier 181 directly affected cases whose
implementation is unchanged retain their recorded passing evidence. These are
focused selections, not a full regression or release qualification. Ruff format,
lint and strict mypy cover all 31 changed Python files. Node syntax and both Create
and Bridge browser-controller harnesses also pass. Native and installed acceptance
are recorded independently in the authorized integration campaign report.
