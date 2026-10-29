# Fast-loop lifecycle and owned-removal qualification (#109–113)

This is an implementation-boundary audit, not a live-update acceptance receipt.
The owner approved exact owned removals, opt-in live update and safe selected
add-on lifecycle operations. Existing authorization, exact identity, provenance,
leases, persistent receipts and reconciliation remain requirements. ZIP and
repository release testing remains separate.

## Current executable foundations

`McpWorkspaceDevelopmentService` claims durable request/target/global-heavy
reservations, locks the exact workspace, proves current running container and
profile, stops the container, copy/syncs, restarts, waits for native readiness and
checks inventory. Failed/ambiguous post-mutation outcomes remain fenced. The
copy adapter requires an exact stopped container and full authenticated prior
destination digest. It retains destination-only members and refuses type changes.
Selective staging changes only payload materialization, not those guards.

The native campaign proves generated video and Python-service fixtures on Linux
amd64 Omega 21.3 r4 / Estuary through canonical Docker adapters and native RPC.
The service additionally uses the real application enablement service with a
private ORM-fixture catalog; migration is tested separately. It does not
qualify authenticated MCP, all add-on types, deletion, live apply or another
Kodi generation/platform. Installed Claw services, grants, selected sessions,
workspaces, external Kodi and household production were preserved.

## Why owned removal is a separate capability

The managed Docker interface currently provides fixed-root `get_archive` and
`put_archive`; these extract/copy members, with no selective unlink operation.
[Docker's archive API](https://docs.docker.com/reference/api/engine/version/v1.51/)
documents that interface. The approved entrypoint exits when Kodi exits; a
stopped container cannot host a resident Bridge or Docker-exec removal worker.
The existing fixed exec helpers perform screenshots, Bridge establishment and
viewing, not stopped add-on filesystem maintenance.

Read-only native Omega introspection exposed `Addons.ExecuteAddon`,
`GetAddonDetails`, `GetAddons` and `SetAddonEnabled`, with no add-on stop/reload
method. Absence from this API is not proof that every other lifecycle mechanism
is impossible; an alternative still needs bounded dispatch and termination proof.

The resident Bridge **does** already implement staged full-directory replacement
with backup/rollback for the external-development video-plugin deployment path
(`narrow_bridge.py`, `_deployment_commit`). This is not a managed stopped-only
selective-removal contract: it runs in Kodi, disables/enables the add-on, and its
descriptor guard requires a video-plugin marker, but can also accept additional
service/script/native extensions. It is not a pure-video eligibility guarantee.
Do not borrow that path to
claim all-type managed quiescence, change external/production authority, or
replace an entire selected tree to evade the exact obsolete-file requirement.

A tar that replaces the whole add-on root to induce recursive deletion removes
unchanged files too, and is not the proposed exact-obsolete-member operation.
Whiteout/layer tricks, host rootfs access, arbitrary exec, mutable host binds and
helper containers with broad mounts are outside this campaign's boundary.
Unknown destination members, links or drift must block before any removal.

One candidate stopped-only design is a pinned runtime maintenance capability:

1. Server derives the exact obsolete members from a freshly read complete
   preimage matching retained authenticated deployment evidence; bind workspace
   generation, add-on, target, container, image/runtime and operation UUID.
2. A trusted runtime helper opens only that fixed add-on root without following
   links, rechecks complete content/mode/ownership and exact member preimages,
   removes only approved obsolete files and then safe empty owned directories,
   and never opens userdata/another add-on as a mutation root.
3. Keep Kodi stopped throughout. Starting a maintenance worker must not start
   Kodi, run an add-on/service, or permit arbitrary command/path inputs. A new
   trusted startup/maintenance protocol would be needed for this design with the
   current entrypoint. A one-shot mode could avoid an always-on daemon, but still
   requires a new trusted mode, image pin and dispatch/recovery protocol.
4. Persist an armed/dispatch barrier before mutation, bounded lifetime, exact
   completion receipt and cryptographic final-tree proof. Interrupted removal
   fences the original operation; startup/recovery must not blindly consume or
   replay a stale request. Retain preimage/plan evidence for recovery.
5. Reconcile unknown outcomes through the same exact stopped identity and
   receipts before normal startup. Qualify interruption, receipt loss, helper
   termination, same-name container replacement, reset/restore, concurrent
   requests and platform semantics before admission.

This candidate introduces durable runtime startup/maintenance coordination and
recovery behavior, beyond an archive optimization or a small Bridge verb. Other
architectures require separate evaluation; this is not a proof that this design
is the only possible one. Under the
campaign's stop rule for substantial new infrastructure, this is a **specific
implementation blocker for stopped owned-file removal**. This is an engineering
assessment; AGENTS.md does not impose a new approval gate, and the owner's
in-principle removal/live-update approval is recognized. No maintenance protocol,
new runtime pin, generic exec fallback or deletion is silently introduced.
Independent safe optimization and feasibility work can proceed.

## Type-aware route and evidence matrix

The route below separates a safe byte-copy fallback from activation or functional
acceptance. A full Kodi restart is the conservative candidate for unqualified
live types; it does not by itself prove that a service ran, a resource consumer
refreshed, or a repository index changed. All types still need exact descriptor,
dependency, baseline, grant and version/platform admission.

| Declared type | Existing safe byte route / activation limits | Live qualification in this campaign |
| --- | --- | --- |
| Video plugin | Stopped copy + restart; fixed video directory smoke and qualified manifest GUI launch exist. Fresh generated fixture code observed on native Omega through adapter/RPC only. | Unqualified; full fallback retained. |
| Audio/image/executable plugin | Stopped copy + restart; manifest GUI launch recognizes one supported content type. Fixed video smoke must not be used as their success proof. | Unqualified; no native category acceptance. |
| Python script / custom window, weather, lyrics, library | Stopped copy + restart; safe `xbmc.python.script` manifest GUI entrypoints exist. Custom window closure and completion are not proved by launch dispatch. Other extension entrypoints need their own supported route. | Unqualified; no automatic live stop/restart. |
| `xbmc.service` | Stopped copy + restart; explicit administrator enable-only opt-in for one installed-tree-verified Python service on stable non-root SUPPRESS Omega 21.3. Default requests refuse startup. Generated service startup smoke passed; ordinary receipt proves enabled state only. | Stop/reload/live replacement and arbitrary-service health unqualified. See [service evidence](FAST-LOOP-SERVICE-ENABLEMENT.md). |
| `xbmc.python.module` | Stopped copy + whole process restart avoids reusing the old process interpreter. Fresh dependent behavior must be observed separately. | No dependent-interpreter invalidation capability qualified. |
| Skin | Stopped copy + restart is the conservative route; selected-skin identity/recovery and input ownership still matter. Enabled inventory does not prove active skin selection. | `ReloadSkin` exists upstream, but no reviewed exact selected-skin reload/recovery operation qualified here. |
| Resource add-on | Stopped copy; restart/reload of the consuming feature may be necessary, and persistent consumer caches are not proven fresh by inventory or process readiness. | Consumer-specific reload and observation unqualified. |
| Repository add-on | Stopped copy; installed descriptor does not prove refreshed catalog/index. Global repository-update builtins are not an add-on-specific qualified activation receipt. | Exact repository refresh unqualified. |
| Binary/native (PVR, inputstream, decoder/encoder, visualization, screensaver, game, etc.) | Candidate for compatible built payloads: stopped-only copy + full process restart. Source copy does not compile native code. ABI/platform/dependencies and actual consumer loading must be qualified; no universal native-source support is claimed. Never overwrite loaded native code. | Live replacement excluded; full restart required. |
| Mixed/unknown extension sets; manifest/dependency/reusable-invoker changes | Evaluate every extension and dependency, choose the strictest route before mutation; unknown combinations remain unqualified. | No single primary-type shortcut or silent activated success. |

The [Kodi add-on descriptor documentation](https://kodi.wiki/view/Addon.xml)
explains reusable invokers; changing bytes is insufficient evidence of a fresh
interpreter. The pinned Omega 21.3
[`StopScript` implementation](https://github.com/xbmc/xbmc/blob/21.3-Omega/xbmc/interfaces/builtins/AddonBuiltins.cpp#L335)
selects one library path, ignores the stop result and contains a mixed-extension
limitation. Its
[invocation manager](https://github.com/xbmc/xbmc/blob/21.3-Omega/xbmc/interfaces/generic/ScriptInvocationManager.cpp#L321)
defaults to a non-waiting stop. Therefore dispatch acknowledgment does not prove
all selected executions ended. This is a source-backed reason to keep live
replacement unqualified, not native evidence that every stop fails.
[Upstream skin reload documentation](https://xbmc.github.io/docs.kodi.tv/master/kodi-dev-kit/page__list_of_built_in_functions.html)
likewise establishes builtin existence, not current Bridge authority or a
qualified skin rollback protocol.

The descriptor parser now records platform-library presence for service admission;
the stored extension-only preflight projection does not establish those details
or reusable-invoker behavior. Bounded regular-file copy rules also exclude linked
or special payload members. The matrix is candidate guidance, not a universal
admission promise. Qualified manifest launch dispatch remains useful without
claiming invocation completion or hot replacement.

## Agent orchestration and phase boundaries

#113 depends on completed #111/#112 contracts. Do not add a success-shaped
`workspace_apply` wrapper which hides retained files, unsupported service
topologies, unobserved fresh execution, or unqualified activation. Its durable
fingerprint must eventually bind requested mode, selected workspace/generation
and target; its receipt must separately state file application, activation
requested/dispatched, fresh code observation and functional smoke. A partial or
unknown mutation must not trigger fallback/restart automatically.

A future qualified route should minimize agent calls by composing existing
preflight and deployment state, not add arbitrary commands, filesystem paths,
implicit grants or generic execution. Required acceptance still includes repeated
edits/no-op, exact removals, active scripts/services, skin recovery, dependency
changes, native-code fallback, lost responses, concurrency/identity drift, and
sequential supported versions/platforms. Only the recorded Omega video and
generated Python-service fixtures have native evidence in this campaign.
Matrix/Nexus/Piers and Windows/WSL2 remain
unqualified for this new workflow; no release or cross-platform claim is made.

#109 remains open. #110 measurement/admission, #111 selective staging and
#112/#113 conservative planning/historical evidence have merged; the safe service
enablement slice builds on those foundations. Exact removal, qualified live
activation, one-call orchestration and full new-workflow runtime acceptance remain
outstanding. The phase-specific stop applies to the new maintenance mechanism and
unproved live mutations, not all lifecycle or orchestration work. Safe independent
slices proceeded. #113 full orchestration depends on unfinished #111/#112 contracts;
its current receipt/planning slice must not be presented as completed acceptance.
The unchanged full `scripts/check` is the final integrated source gate and cannot
turn missing native capabilities into a pass.
