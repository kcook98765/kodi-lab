# One-call native ZIP installation feasibility (#53)

Audit baseline: main `11ccb777598b754ad73cb691e6ac8489c052ff11`,
2026-10-29 UTC. This is a source/API feasibility finding. No installation,
runtime execution, prompt acceptance or new authority was exercised.

**Defer an unattended one-call installer.** The reviewed public interfaces do
not establish a small supported way to install an exact ZIP and return a native
completion receipt. Opening a chooser, extracting an archive, or observing an
enabled ID/version cannot satisfy that contract. A future upstream API or a
separately reviewed bounded interaction design would be needed.

## Kodi interfaces checked

The tagged implementations below all ignore the built-in's parameter vector
and invoke the Add-on Browser ZIP chooser. Passing a path to the built-in does
not select or install that package.

| Exact source | `InstallFromZip` behavior | Acceptance consequence |
| --- | --- | --- |
| [Matrix 19.5](https://github.com/xbmc/xbmc/blob/19.5-Matrix/xbmc/interfaces/builtins/AddonBuiltins.cpp) | Opens the chooser | No unattended exact-package contract |
| [Nexus 20.5](https://github.com/xbmc/xbmc/blob/20.5-Nexus/xbmc/interfaces/builtins/AddonBuiltins.cpp) | Opens the chooser | Same limitation |
| [Omega 21.3](https://github.com/xbmc/xbmc/blob/21.3-Omega/xbmc/interfaces/builtins/AddonBuiltins.cpp) | Opens the chooser | Same limitation |
| [Piers 22.0 rc1](https://github.com/xbmc/xbmc/blob/22.0rc1-Piers/xbmc/interfaces/builtins/AddonBuiltins.cpp) | Opens the chooser | Same limitation; no preview runtime acceptance inferred |

The [public built-in documentation](https://xbmc.github.io/docs.kodi.tv/master/kodi-base/d0/d3e/page__list_of_built_in_functions.html)
also describes a dialog and Unknown sources consent. Its current master version
is not evidence that an installed older runtime has a new API.

Omega's [JSON-RPC method schema](https://github.com/xbmc/xbmc/blob/21.3-Omega/xbmc/interfaces/json-rpc/schema/methods.json)
exposes add-on enumeration, details, enablement and execution, without a ZIP
installation method. `Addons.ExecuteAddon` executes installed code. It does not
establish package provenance or native installation. The corresponding
[operations implementation](https://github.com/xbmc/xbmc/blob/21.3-Omega/xbmc/interfaces/json-rpc/AddonsOperations.cpp)
does not supply an alternative ZIP installer.

The [Add-on Browser implementation](https://github.com/xbmc/xbmc/blob/21.3-Omega/xbmc/addons/gui/GUIWindowAddonBrowser.cpp)
owns source consent and file selection before passing a chosen path to Kodi's
installer. `InstallAddon(id)` instead addresses an add-on ID through the native
repository/install flow; an ID does not pin an arbitrary reviewed workspace ZIP.
The internal [C++ installer](https://github.com/xbmc/xbmc/blob/21.3-Omega/xbmc/addons/AddonInstaller.h)
accepts a path, but that internal method is not a documented Python/JSON-RPC
installation endpoint. Calling an internal C++ symbol from a new helper would
add a version-sensitive execution boundary rather than reuse the current Bridge.

## Current Lab paths and their limits

| Code / interface | Already provided | Missing for #53 |
| --- | --- | --- |
| `application/native_zip_installations.py`, authenticated `/api/native-zip-installations` | Reviewed immutable original artifact, managed disposable target, explicit `addon.install` permission, exact credential/grant/runtime/container checks, bounded expiring target-only delivery and verification | HTTP workflow, not an MCP installer; currently passive repository ZIPs, not the active executable development add-on |
| `development/repository_zip.py`, uploaded ZIP artifact services | Structure/path/type/size/digest and repository-URL admission | Does not confer workspace-generation lineage or broaden accepted package types |
| `broker/operations.py::observe_native_zip_installation`, broker installed-payload inspection | Read-only exact installed-payload/ownership evidence for the native delivery receipt | Cannot dispatch Kodi installation or prove a native installer event |
| `mcp/registry.py`, `application/mcp_workspace_development.py` | Generation-bound source workspace deployment and its existing authority | Source copying is not native ZIP package-install qualification |
| `bridge/service.kodi_lab_bridge/narrow_bridge.py` | Closed reviewed operations for observation, deployment, launch, logs and scoped settings | No arbitrary built-in, Python, shell, URL or native ZIP-install operation |

See [the existing native ZIP workflow](NATIVE-ZIP-INSTALLATION.md) for its actual
admission, limits and receipt states. A delivery request or download does not
prove successful installation. Its verified result requires enabled matching
repository inventory and independent complete payload/ownership readback on
the same runtime/container, and still is not an installation-event receipt.
`addon.install` is opt-in and absent from built-in presets; development,
inspection, lifecycle or viewer access does not imply it.

## Smallest supported alternative

Continue using exact-generation `workspace_deploy` for rapid source iteration.
For currently supported repository packages, an explicitly authorized operator
can use the existing reviewed artifact delivery and Kodi's ordinary Install from
ZIP controls, retaining consent and verification. This alternative does not
implement one-call native installation of the active development plugin.

Supporting executable development ZIPs through that operator-assisted workflow
would first need a focused artifact/admission extension, generation/digest binding,
replacement policy and separate installation authority review. Do not expose the
current repository-only endpoint as if it already admits such packages.

A UI automation wrapper would need bounded prompt/state handling, exact artifact
and installed-tree identity, cancellation/timeout receipts, operation ownership,
input exclusion and restart recovery across skins/platforms. That exceeds a small
safe addition. Native confirmation can require administrator action; no workflow
may enable Unknown sources, accept prompts or replace unrelated code implicitly.

## Remaining acceptance and decision

No implementation PR for an unattended installer is justified by this audit.
Keep #53 open for owner choice: retain the operator-assisted boundary, or scope
a separate executable-package/native-interaction design. External development
and household production targets remain separate, unqualified scope.

Before claiming the requested feature, prove exact reviewed ZIP/generation and
target binding, consent/cancellation, dependency and compatibility failure,
upgrade/enablement, repeat/lost-response behavior, full installed-tree evidence
and native completion semantics on each admitted runtime. Ubuntu source checks
cannot establish Windows/WSL2 or Kodi 19/20/21/22 native acceptance.

Documentation review and focused existing contract tests are campaign evidence
only. **FULL RELEASE GATE DEFERRED — NOT RELEASE QUALIFIED.**
