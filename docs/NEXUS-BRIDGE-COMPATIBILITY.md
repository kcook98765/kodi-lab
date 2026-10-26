# Reviewed managed Nexus Bridge compatibility

The closed installer admits the exact managed Nexus **20.5** runtime to the
current stable Narrow Bridge **1.1.27**, without a Nexus-specific byte, version,
fingerprint, dependencies or authority. This extends managed bootstrap eligibility;
it does not admit other Nexus patch releases or a caller-selected service ZIP.

Initial managed Nexus support used unchanged stable 1.1.25. The separately reviewed
1.1.27 [file-mode correction](FILE-MODE-CONTRACT.md) applies to Nexus and Omega
together. Its new artifact identity does not change Nexus runtime eligibility.

## Runtime and API contract

The Bridge requires `xbmc.python >= 3.0.0` and a login-started `xbmc.service`.
[Kodi 20.5 provides xbmc.python 3.0.1](https://github.com/xbmc/xbmc/blob/20.5-Nexus/addons/xbmc.python/addon.xml).
Its standard-library HTTP/HMAC/JSON/filesystem implementation needs no external
Python packages. The reviewed Ubuntu Noble Nexus recipe uses non-root UID/GID
1000:1000 and the Python 3 runtime already used by the fixed broker establishment
program. RC1-only exact-release identity handling is not applied to stable Nexus.

The stable Bridge uses Kodi 20 APIs: `xbmc.getInfoLabel`, `getCondVisibility`,
`executeJSONRPC`, `executebuiltin`, `Monitor`, `Player`, `xbmcaddon.Addon` settings,
`xbmcvfs` translation/file operations and `xbmcgui` window/control inspection.
The [20.5 Python bindings](https://github.com/xbmc/xbmc/tree/20.5-Nexus/xbmc/interfaces/legacy)
and [GUI built-ins](https://github.com/xbmc/xbmc/blob/20.5-Nexus/xbmc/interfaces/builtins/GUIBuiltins.cpp)
provide the required operations, including synchronous `TakeScreenshot` and
`ActivateWindow`. Native JSON-RPC readiness, add-on enablement and inventory retain
their existing exact-version and identity checks. API availability is source
compatibility evidence; it does not establish native authentication or GUI success.

## Artifact and trust contract

`managed_bridge_build.json` binds the stable version, protocol, semantic
fingerprint and executable source hashes. `managed_bridge_release_inventory.json`
and `packaged_stable_bridge_sources()` verify every one of the ten release files.
The deterministic bundle ZIP and `BRIDGE.json` retain their checksum, exact
inventory and committed-source provenance. Nexus selects these existing stable
bytes; it does not select the RC1 overlay or loosen any checksum check.

Application candidate status, lifecycle installation, broker resolver and Web
availability each admit exact 20.5. Installation still requires the live typed
lifecycle owner and the approved runtime/image/container with its non-root
ownership. The broker verifies fixed reviewed material before writing it, rejects
unknown or changed existing trees, and never accepts paths, ZIPs, programs, UID/GID
or credentials from the caller.

Each profile establishes its own random secret and resident Bridge UUID. Staging
records only prepared intent. Native discovery/enablement and authenticated
resident identity must confirm the exact build and Nexus version before trust
becomes active. Continuation rotates the prepared secret and fences its original
generation. Old Omega trust, a stale container/runtime/generation, altered build,
or authentication failure cannot supply Nexus identity. Ordinary start can reuse
matching active trust; it cannot bootstrap missing files or repair invalid trust.

## Qualification boundaries

Focused regressions cover clean Nexus staging/continuation, independent secret
rotation, stable artifact materialization/readback, wrong Omega/build/authentication
refusal, lifecycle lease ownership, stale generation/container refusal, exact
release exclusion, and matching Web/status eligibility. Existing drift, unsafe
tree, ownership and authorization tests remain active.

Release qualification belongs to the final settled source and matched application
artifacts. Native acceptance is a separate installed test: supported installation
in one clean Nexus profile, independent authenticated identity, MCP inspection,
screenshots, exact deployment, GUI observation, logs, iteration and Stop/Resume.
An installed file, prepared state or passing fixture does not establish that
acceptance. Campaign evidence records the achieved level and immutable identities.
