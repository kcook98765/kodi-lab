# Matrix 19.4 resident Bridge compatibility

Matrix 19.4 needs an explicitly installed, independently authenticated resident
Bridge for manifest launch and MCP log reads. Native JSON-RPC readiness and the
fixed video-plugin smoke do not establish either capability. An absent resident
returns `managed_bridge_upgrade_required` for launch and `bridge_not_paired` for
logs. Missing managed installation eligibility returns
`managed_bridge_candidate_unavailable`; changing MCP grants cannot repair it.

The minimum candidate for separate qualification is the exact packaged stable
Bridge 1.1.31, protocol 1,
fingerprint `e18b4176611506335316a41e4dee9ea2ed113f8b5e7a0b95f6c156597d1618be`.
Its entire frozen inventory is checked by the existing broker materializer.
Source inspection identifies no necessary Matrix overlay for launch/log reading.
That is a compatibility hypothesis, not reviewed native support. Ordinary Matrix
installation eligibility remains closed in status, prerequisites, lifecycle,
broker and UI. There is no alternate pairing ceremony, copied trust or
caller-supplied program. External reviewed-build eligibility is unchanged.

The compatibility basis is the tagged [19.4 Python API manifest](https://github.com/xbmc/xbmc/blob/19.4-Matrix/addons/xbmc.python/addon.xml)
(`xbmc.python` 3.0.0), the tagged
[builtin interface](https://github.com/xbmc/xbmc/blob/19.4-Matrix/xbmc/interfaces/legacy/ModuleXbmc.h),
and [VFS interface](https://github.com/xbmc/xbmc/blob/19.4-Matrix/xbmc/interfaces/legacy/ModuleXbmcvfs.h).
Manifest plugin launch uses `xbmc.executebuiltin` with the existing server-derived
`ActivateWindow(videos,plugin://<verified-addon-id>/,return)`; script launch uses
the existing checked `RunScript` path. Identity uses `getInfoLabel`. Log reading
uses `xbmcvfs.translatePath("special://logpath")` plus bounded standard-library
descriptor reads. These APIs are present in Matrix; no v20 Settings API is
needed by these paths. The reviewed Ubuntu Matrix image embeds Python 3.10,
which supports the Bridge's syntax and required standard library. This does not
claim compatibility with every historical Matrix interpreter or platform.

`Addon.getSettings()` is absent in Matrix. Managed settings writes remain limited
to independently qualified Nexus 20.5/Omega 21.3 by `settings_binding` and the
resident's capability check. This change does not enable Matrix settings writes.
The add-on under development must itself use Matrix APIs, such as the typed
`Addon.getSetting*` getters, and separately matched static tooling.

Regression coverage exercises stable candidate materialization in a temporary
filesystem, exact Matrix/adjacent-release installation denial, manifest dispatch
replay/pending barriers and log rotation using Matrix-shaped doubles without
`getSettings`. The existing trust/authentication/lease tests remain unchanged.
This is source qualification; it is not native candidate installation or GUI
acceptance. The helper materializer test does not admit a production request.

Before any admission change, separately authorized native candidate qualification
must establish actual resident startup/authentication, fixed plugin/script launch,
current/previous bounded log reads, response loss and durable pending-receipt
behavior on the exact reviewed Matrix image. Use isolated resources under explicit
native-test authority; do not bypass the installed production candidate policy.
Only independently reviewed native evidence can justify a subsequent focused
admission change. Tagged APIs and doubles are insufficient by themselves.

After that change is independently approved and qualified, a separate authorized
production upgrade and **Install reviewed Bridge** operation would still be
required. Each target must establish its own secret, native identity, trust
generation and signed managed-launch marker. Exact container/runtime/generation
checks, authorization, deployed-tree verification and response-loss reconciliation
remain mandatory. Final installed acceptance must verify GUI behavior, bounded
current/previous log reads and a changed second iteration. A menu fallback cannot
establish direct `workspace_launch` or MCP log acceptance.
