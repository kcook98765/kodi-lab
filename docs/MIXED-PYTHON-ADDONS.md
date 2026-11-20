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

## Registered-source cold path

1. Register the complete reviewed source, create/capture its workspace and
   validate the exact generation. With retained profiles, review the identity
   preview and select that target/workspace before attachment or source copy.
2. Attach the source and copy it through the supported stopped source-copy path.
   Start Kodi and wait for native readiness. Neither a copy receipt nor process
   startup proves enabled state or service work.
3. In Manage Kodi, explicitly select “Allow this service to start now and on
   future Kodi starts while enabled” and choose **Enable service**. The matching
   Web request is `POST /api/instances/{target}/development-sources/{source}/enable`
   with `{"allow_service_start": true}`. Inspect the enabled inventory and service
   evidence independently.
4. Deploy the validated workspace generation through `workspace_deploy` in
   `direct` mode. The existing stop/copy/restart, exact-tree verification and
   recovery contract applies. Registered sources need their consented first
   enablement before this path can satisfy its enabled-inventory postcondition.
5. Invoke only an eligible script/plugin ordinal through `development_launch`.
   Services remain driven by enablement and Kodi lifecycle. Observe actual UI or
   plugin output and background work; accepted dispatch is not completion.
6. Use supported Stop/Start controls to qualify abort, reconstructed state and
   resumed work. An ambiguous operation must be inspected under its original
   identity before a new request is considered; never replay it to force success.

The canonical source baseline and a later workspace deployment are separate
proofs. After workspace code changes, an old registered-source enablement check
may correctly refuse its stale full-tree baseline. Do not adopt the changed
installed tree or strip extensions to bypass that refusal. Continue through the
workspace's exact current deployment/launch contract, or deliberately establish
a new stopped registered-source baseline when first enablement is required.

## Kodi rules and qualification

Kodi's [manifest documentation](https://kodi.wiki/view/Addon.xml) defines the
extension points and plugin content types. Its
[service documentation](https://kodi.wiki/view/Service_add-ons) describes service
startup and abort handling. The tagged
[Omega parser](https://github.com/xbmc/xbmc/blob/21.3-Omega/xbmc/addons/addoninfo/AddonInfoBuilder.cpp)
and [Piers RC2 parser](https://github.com/xbmc/xbmc/blob/22.0rc2-Piers/xbmc/addons/addoninfo/AddonInfoBuilder.cpp)
retain multiple types and derive the primary type from the first declaration.
Native results, rather than parser inference alone, determine the matrix below.

Native Linux amd64 qualification used seven inert add-ons in a separate Docker
Engine, fresh Lab data and profiles, and the supported authenticated Web/MCP
interfaces. It reused reviewed cached runtimes; this is not clean-host runtime
acquisition, a production upgrade, remote-client browser acceptance or TV Guide
application acceptance. Metadata and all executable declarations were retained.

| Representative declaration order / plugin content | Matrix 19.4 | Nexus 20.5 | Omega 21.3 | Piers 22 RC2 |
| --- | --- | --- | --- | --- |
| script + service | Pass | Pass | Pass* | Pass |
| plugin + service: video | Pass | Pass | Pass* | Pass |
| plugin + service: audio | Pass | Pass | Pass* | Pass |
| plugin + service: image | Pass | Pass | Pass* | Pass |
| plugin + service: executable | Pass | Pass | Pass* | Pass |
| script + plugin: video | Pass | Pass | Pass* | Pass |
| script + plugin: video + service | Pass | Pass | Pass* | Pass |

Each passing cell includes native manifest discovery, registered intake and
validation, stopped baseline copy, first enablement from disabled, matching
native enabled inventory, exact-generation workspace deployment, and every
script/plugin entry's execution log and native UI capture. Service cases also
include refusal without consent, explicit consented enablement, timed background
work and a reminder-model action, then reconstructed fixed reminder state and
new work after Stop/Start. Independently read and hashed stopped-profile archives
contain cooperative abort markers for all six services. These inert timers and
fixed reminder seeds do not test TV Guide's persistent reminders, scheduler,
providers or user data. Other declaration orders have policy tests, without a
claim of native coverage for every permutation.

*Omega's original seven-case run and final-candidate script/service follow-up
passed. Two supplemental repeat starts exited with native status 1, without an
OOM indication; their cause remains unresolved. One workspace operation stopped
at readiness and was reconciled through the supported Web endpoint as
`contained_requested_tree`, not success. A later explicit Start and new
workspace deployment succeeded. The original failure receipts and failed-boot
profiles remain preserved separately from the passing run; repeated Omega
startup is not an unqualified reliability pass.

Matrix, Nexus and Piers run the frozen implementation at `aa1e9a8`; the original
Omega matrix used its preceding candidate, before the additional refusal for
repeated script declarations. Installed Web and broker hashes match the final
changed Python modules; the final-candidate Omega follow-up covers script/service.
Project import/owned-project restrictions and unchanged single-extension behavior
have focused regression coverage; the native matrix uses registered sources.
Source validation is **FOCUSED PASS**. The full release gate was explicitly
skipped at the owner's request; no **RELEASE PASS** is claimed.
Private credentials, complete log scans, operation receipts, runtime/image
identities, UI captures and archive hashes are retained in the owner's private
`issue394-20261119` evidence directory, not published in this repository.

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
