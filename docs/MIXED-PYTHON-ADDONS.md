# Mixed Python add-ons (#394)

Kodi Lab preserves manifest order and every extension declaration. Common
Python combinations can use stopped registered-source copy/restart, followed by
explicit administrator first enablement. A service always needs
`allow_service_start: true`, including when Kodi reports the primary type as
script or plugin. This intent is stored on the original enablement operation;
it is not a new MCP permission or an execution receipt.

The supported shape is at most one each of `xbmc.python.script`,
`xbmc.python.pluginsource` and `xbmc.service`, with safe relative `.py` libraries,
no platform-library overrides and each executable library present in the exact
verified installed tree. Metadata is retained. Broader passive, binary, skin,
repository and repeated executable declarations are preserved by registered
source capture but remain outside this automatic activation qualification.

The complete source descriptor hash must match the installed descriptor obtained
from a digest-verified full tree. Host edits cannot hide an installed service.
Native inventory and details must agree with the descriptor's first non-metadata
primary type. That projection never replaces complete descriptor verification.
Dependencies, runtime/image/container identity, workspace generation, grants,
credential revocation, human priority, leases, request replay and containment
retain their existing checks. Old brokers without descriptor-hash proof refuse
first enablement; upgrade Web and broker together.

Scripts use their declared ordinal and the existing exact-library launch.
Repeated script declarations retain individual ordinals but receive
`multiple_script_extensions`; repeated plugin declarations similarly remain
ineligible until separately qualified.
Plugins use their declared ordinal and existing content-specific window route.
Services run through Kodi enable/start/stop lifecycle and are never directly
invoked as scripts or plugins. Single video/audio/image/executable plugin content
types have separate launch contexts. Multi-content plugin declarations are valid
intake but retain `unsupported_plugin_content` for the bounded single-window
launcher. Fixed video smoke eligibility is independent of whole-addon validity.

Editable `project_import` preserves these common mixed Python shapes under the
existing size/file/path/dependency bounds. Script + plugin can use owned-project
first enablement. Private service projects require a separately reviewed consent
workflow: deployment refuses `project_service_activation_requires_consent`
before attaching, copying or restarting. Import and workspace editing cannot
acquire background service authority. Use registered sources and Web service
consent for this campaign's service lane.

## Kodi rules and qualification

Kodi's [manifest documentation](https://kodi.wiki/view/Addon.xml) defines the
extension points and plugin content types. Its
[service documentation](https://kodi.wiki/view/Service_add-ons) describes service
startup and abort handling. The tagged
[Omega parser](https://github.com/xbmc/xbmc/blob/21.3-Omega/xbmc/addons/addoninfo/AddonInfoBuilder.cpp)
and [Piers RC2 parser](https://github.com/xbmc/xbmc/blob/22.0rc2-Piers/xbmc/addons/addoninfo/AddonInfoBuilder.cpp)
retain multiple types and derive the primary type from the first declaration.
Native results, rather than parser inference alone, determine the matrix below.

Qualification is in progress. No native completion is claimed by this source
checkpoint. The final matrix must distinguish manifest validity, registered
intake, stopped deployment, consented enablement, enabled inventory, each
script/plugin launch, service work and stopped/restarted activity on Matrix
19.4, Nexus 20.5, Omega 21.3 and Piers 22 RC2.

Live service reload, hot replacement, automatic obsolete-file deletion, arbitrary
service health and other platforms remain unqualified. Successful enablement
continues to say `service_execution=not_observed`; native fixture evidence is
separate. This work does not install or test TV Guide Fullscreen and does not
upgrade Claw production.

The existing script launcher uses a library path. Kodi can warn that the script
has no implicit add-on context; add-ons relying on an implicit `xbmcaddon.Addon()`
context remain unqualified by these fixtures. Explicit-ID access is separate.
This campaign does not redesign that pre-existing Bridge launcher. The TV Guide
checkpoint uses an explicit add-on ID, but its own native behavior still requires
application acceptance after the Lab support is deployed.
