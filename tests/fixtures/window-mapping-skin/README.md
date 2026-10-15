# Isolated custom-window fixture

This source-only Kodi skin uses fixed Home buttons and Kodi's native XML loader.
It runs no scripts, services, or setting mutations. Select the fixed
buttons with ordinary `Input.Down` / `Input.Select` after activating Home. They
call supported `ActivateWindow` skin built-ins for runtime IDs 29001–29004,
derived from explicit XML IDs 19001–19004. The visible control labels deliberately
do not define a Kodi window title. Verify the actual reported window label is
empty before claiming unnamed-window acceptance.

Use only a freshly created, disposable managed profile in an isolated preview.
Install/enable as an ordinary skin add-on and select it through Kodi settings.
The manifest targets the Kodi 21/22 GUI ABI. For Kodi 19/20, prepare a separate
local copy with the reviewed GUI dependency for that generation; never assume
the 21/22 manifest is installable there. Kodi's core Arial font fallback avoids
bundling any font binaries.

Mapping fixtures belong only in that preview's local mapping directory, with
exact `skin.kodilab.windowfixture` / `1.0.0` scope: 29001 verified base,
29002 verified dialog, 29003 explicitly ambiguous, and 29004 deliberately absent.
These are test definitions, not third-party skin mapping contributions. Keep
upstream bundled mappings unchanged. Any mismatched-skin/version scenario must
return no verified semantic name. Record the fixture's source hashes, runtime
version, Bridge identity, MCP authorization and raw/resolved responses.

Close test windows before retaining the preview. Existing household profiles,
production permissions and production navigation transports are never changed.
