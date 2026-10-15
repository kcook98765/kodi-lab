# Window identification and skin mappings

`inspect_ui_state` and the authenticated HTTP route
`GET /api/instances/{id}/ui-state` automatically return semantic resolution for
both the base `window` and foreground `dialog`. No request option or client
configuration is needed. Summaries use the resolved names.

UI response **schema version 2** keeps Kodi's original `id` and `label` alongside
a `resolution` object: semantic `name`, `role`, `source`, `status`, and active
`skin_id`/`skin_version` when relevant. Null names and explicit statuses preserve
uncertainty. The single semantic group avoids confusing reported and resolved
names; long source revisions belong in mapping data rather than routine tool
responses. MCP advertises this schema and HTTP exposes the same model in OpenAPI.
Resolution remains central server metadata. The Bridge wire contract,
authorization, navigation and focus/container association are unchanged.

```json
{"target_id": "the authorized target UUID"}
```

```json
{
  "id": 11112,
  "label": null,
  "resolution": {
    "name": "Volume control",
    "role": "playback",
    "source": "skin_mapping",
    "status": "verified",
    "skin_id": "skin.estouchy",
    "skin_version": "3.0.8"
  }
}
```

This example corresponds to the bundled Estouchy 3.0.8 mapping. The base window
and foreground dialog resolve separately. Summaries
use resolved names deterministically, while existing named-window summaries
retain their wording.

## Lookup and observation

The lookup order is meaningful Kodi-reported name, matching core definition,
matching active-skin definition, reserved-range classification, then unknown.
Roles are `home`, `library`, `dialog`, `settings`, `playback`, `navigation`,
`system`, and `unknown`. A role describes purpose; `dialog.active` continues to
describe whether a modal dialog is in the foreground.

Blank/numeric/placeholder names and XML basenames are not semantic names.
`System.CurrentWindow` uses Kodi's global localization table. In dynamic ranges
this can yield unrelated words: 13000 produces "System" and 14000 produces
"Stack". Those reported labels are preserved but excluded from identification.
See the pinned [label implementation](https://github.com/xbmc/xbmc/blob/28ea2eac1eb7af8fdcbd2672d933ce87f594be79/xbmc/guilib/guiinfo/GUIControlsGUIInfo.cpp)
and [global strings](https://github.com/xbmc/xbmc/blob/28ea2eac1eb7af8fdcbd2672d933ce87f594be79/addons/resource.language.en_gb/resources/strings.po).

`verified` means that the ID and fixed semantic purpose were reviewed in pinned
upstream source. It does **not** mean that every mapping was exercised live.
`ambiguous` and `unverified` entries return no resolved name or role. Configurable
hubs, selectors, and helpers cannot reliably identify their current content from
an ID. A useful reported label still wins. Range classifications are explicitly
`classified`, never verified: 11100–11199 custom skin, 13000–13099 Python,
14000–14099 add-on. No add-on identity is inferred from dynamic allocation.
Other unknown IDs are retained with `name: null`, `status: "unknown"`. Enriched
summaries explicitly say `Unknown dialog 29004` or `Ambiguous dialog 29003`
instead of presenting a raw placeholder or the base-window name as the dialog.

Mapping JSON is loaded once at application startup, indexed in memory, and never
downloaded automatically. Ordinary named/core observations add no remote calls.
An unnamed ID with available skin candidates triggers fresh GUI skin/foreground
and add-on version reads around an authorized full observation. On the first
candidate observation the original sample is discarded and recollected inside
those checks. A bounded, 60-second scheduling hint then lets subsequent candidate
observations bracket their **first** full collection, avoiding duplicate work.
No cached skin ID, version, mapping or returned observation is trusted.

Hints are scoped to existing target/container/runtime/endpoint/version metadata,
plus external identity generation where available, and cleared on mismatches,
errors, no applicable skin, expiry or application restart. The cache holds at
most 1024 target hints. Skin changes, add-on updates, profile transitions,
reconnects and in-process restarts cannot reuse old semantic identity because
both sides of every enriched observation still read the actual active skin ID
and version. There is no complete ordered invalidation stream for these events,
so caching ID/version as authoritative metadata was rejected. A stale scheduling
hint can only cause extra reads. A transition back to a core window in a known
skin may add a GUI and add-on-version read before collection clears the hint.
Warm checks cannot exclude a custom base window just because a core dialog is
in the foreground; the fresh full observation checks both windows.

Four context calls remain because the Bridge payload lacks skin identity and
GUI skin properties lack its version. Unsupported skin IDs stop after one GUI
read, incompatible versions after two reads. Optional context failures retain
an already successful observation; an initial collection still propagates its
own failures. Provider authorization and identity failures still propagate from
collection. Missing identity or changed collection state disables skin
resolution. Sampling remains bounded/non-atomic; an unobservable
change-and-return between samples cannot be excluded.

## Data and version matching

Built-in data lives in `src/kodi_lab/window_mappings/`: `schema.json`, four
`kodi/*.json` files, and version-scoped `skins/*.json` files. The runtime Pydantic
`MappingDocument` validator defines the JSON schema plus relational checks for
duplicate IDs, supported majors, and required exact skin versions. Each document
records schema version, Kodi majors, exact skin add-on ID/versions where
applicable, immutable source revision/URLs, and reviewed entries. Each entry has
numeric runtime ID, semantic name/role, verification, and optional source files,
symbolic name, aliases, ambiguity notes, and minimum Kodi release.

Skin versions match **exactly**, including suffixes such as `~nexus` or `~beta6`.
A version omitted from the data does not inherit another version's mapping.
Compatibility lists apply per document, not to the cross-product of every
version/major in the coverage table below. Core data is major-scoped against
19.5, 20.5, 21.3 and 22 RC1 snapshots; 19.0–19.5, 20.0–20.5 and 21.0–21.3 header
definitions were compared and unchanged. Later releases still need source
review when definitions change. Six late Kodi 22 preview additions have
`kodi_min_version` floors (`22.0-beta.1`, `22.0-beta.2`, `22.0-rc.1`). Earlier
previews and incomplete version strings cannot use those entries. Routing target
12998 is ambiguous because its destination is configured; virtual keymap
contexts carry explicit notes in the data.

The [Kodi window reference](https://kodi.wiki/view/Window_IDs) and
[Skinning Manual](https://kodi.wiki/view/Skinning_Manual) are useful guides;
pinned `WindowIDs.h`, `WindowTranslator.cpp`, the custom-window loader, and actual
skin XML determine these mappings. Skin compatibility derives from official
Kodi add-on repository branches or maintained source and the relevant `xbmc.gui`
ABI contract. It establishes source compatibility, not full runtime acceptance.

## Extract and contribute

Obtain a local source checkout or unpacked released source archive from the skin
maintainer. Identify the add-on by the `addon.xml` `id` attribute, rather than its
display name. Record its exact version and immutable upstream commit. Review
maintained branches, release metadata, or official repository distribution before
filling `kodi_versions`; a minimum `xbmc.gui` dependency alone is insufficient.

```console
uv run --locked python scripts/extract_skin_windows.py /path/to/skin-source \
  --source-url https://github.com/maintainer/skin.example \
  --source-revision EXACT_COMMIT > /tmp/skin.example-candidates.json
```

The extractor records identity/version, source hashes, declared resolution
folders, XML/runtime IDs, filename/heading name candidates, activation references,
and duplicate/conflicting definitions. Attribute and child-element ID/type
declarations are supported; attributes take precedence. Only top-level `custom*`
XML files in declared resolution folders create candidates. Legacy skins use
Kodi's recognized resolution folder names. Other XML supplies reference context;
nested includes and dormant `extras` copies do not create windows.

The custom-window loader **always adds 10000** to an explicit XML ID, including
IDs already above 10000: XML 1100 becomes runtime 11100; XML 11901 becomes runtime
21901. Numeric activation references above 9999 already use full runtime IDs.
Do not derive IDs from filenames or blindly offset API-reported IDs. See the
[pinned loader](https://github.com/xbmc/xbmc/blob/28ea2eac1eb7af8fdcbd2672d933ce87f594be79/xbmc/application/ApplicationSkinHandling.cpp)
and [translator](https://github.com/xbmc/xbmc/blob/28ea2eac1eb7af8fdcbd2672d933ce87f594be79/xbmc/input/WindowTranslator.cpp).

Reports use `kind: "skin_mapping_candidates"`, empty Kodi compatibility and
`verification: "unverified"` throughout. They cannot be loaded as mappings.
Review window controls, localization strings, activation references and visibility
conditions, including conflicts across resolutions. Deliberately convert reviewed
entries into a mapping document with exact compatibility, names, roles, provenance
and uncertainty notes. Update an existing mapping by reviewing the new pinned
source and adding its exact version; never extend compatibility speculatively.

The extractor reads XML only. It never executes add-on code or fetches URLs. It
rejects DTD/entities (including UTF-16), symlinks and unsafe resolution paths;
skips nonregular files; and bounds count, bytes, XML depth/nodes and references.
Malformed individual XML yields an explicit partial report. Invalid metadata or
global budget exhaustion fails the command. Review every warning.

```console
./scripts/check-focused tests/kodi/test_window_resolver.py \
  tests/kodi/test_skin_window_extractor.py \
  --source src/kodi_lab/kodi/window_resolver.py \
  src/kodi_lab/kodi/ui_window_resolution.py \
  scripts/extract_skin_windows.py
```

The regular test suite validates every bundled JSON against the schema and
runtime model and exercises every entry, version gates, conflicts and fallback.
To regenerate the schema after a deliberate validator change:

```console
uv run --locked python -c 'import json; from pathlib import Path; from kodi_lab.kodi.window_resolver import MappingDocument; Path("src/kodi_lab/window_mappings/schema.json").write_text(json.dumps(MappingDocument.model_json_schema(), indent=2) + "\n")'
```

Submit focused [GitHub pull requests](https://github.com/kcook98765/kodi-lab/pulls)
following [CONTRIBUTING](../CONTRIBUTING.md). Include immutable upstream revisions,
compatibility evidence, uncertain cases, tests, and whether validation was source
review or an isolated live instance. Exclude credentials and private profile data.

## Local extensions

Place reviewed JSON documents directly in
`${KODI_LAB_DATA_ROOT}/window-mappings/` (default
`/var/lib/kodi-lab/window-mappings/`). Use the same schema and exact skin/version
matching. The directory need not exist; Kodi Lab does not write built-in or local
mapping files. Restart the application to reload. No request-time filesystem scan
or administration UI is needed.

Only skin extensions are accepted. Any file containing a core ID from any
bundled version is rejected as a whole. Within a document duplicate IDs are
rejected. Across valid local files, lexicographically later filenames override
earlier entries with the same skin/version/Kodi-major/ID key; local entries
override built-in skin entries with that exact key. This permits reviewed
corrections, including marking an earlier claim ambiguous. Unrelated versions or
skins never collide. Built-in core definitions always win.

Malformed, oversized, linked, duplicate-key or future-schema files are ignored
individually. Limits are 2 MiB/file and 256 JSON files. Rejections are retained in
`WindowResolver.errors` and logged as a count without file contents. A missing
directory is normal. Validate an extension with `read_document(Path(...))` and
instantiate `WindowResolver(local_root=...)` to inspect rejection diagnostics
before restarting. Local `verified` assertions are administrator-reviewed data,
not cryptographically attested upstream facts.

## Initial source coverage

Core: **560 entries, 556 verified definitions and 4 ambiguous routing targets**.
Skins: **825 entries, 558 verified and 267 ambiguous**, in 37 pinned documents.
Counts include repeated IDs in distinct release documents. Every requested skin
has at least one pinned release, plus Arctic Fuse 3. Unlisted versions, absent
generation-specific releases (for example fTV 20 and Mimic-LR 20), dynamic add-on
identity, and configurable window content remain gaps. No popularity ranking is
inferred. All table entries are source-reviewed; live tests, performance,
packaging and automated results are recorded in `WINDOW-MAPPING-VALIDATION.json`.

Native observations cover Kodi 19.4, 20.5, 21.3 and 22 RC1 with Estuary.
Real custom-window MCP/Bridge validation uses an isolated Kodi 22 RC1 profile;
community-skin mappings remain source-reviewed. No production Kodi preferences
were changed. Exact versions, source pins, gate results and measured samples are
recorded in the validation report.

Duplicate GUI-thread label/boolean collection caused the earlier approximately
130 ms overhead. Interleaved native benchmarks on Kodi 19.4/20.5/21.3 measured
118–120 ms baselines, 253–255 ms cold candidate observations (14 calls), and
123–125 ms warm observations (9 calls). These transport-specific measurements
are separate from full MCP/Bridge requests and CPU-only resolver measurements.
The current report compares default-on timings with this accepted baseline.
It excludes test proxy startup and records cold and warm samples separately.

The source-only [acceptance skin](../tests/fixtures/window-mapping-skin/README.md)
uses supported custom XML and fixed Home-button `ActivateWindow` actions. It
avoids changing production navigation transports or enabling generic execution.
Install it only in a fresh disposable isolated profile, with private local
mapping extensions. It includes base/dialog, ambiguous and unknown IDs; use the
existing MCP grants and ordinary inspection requests. No test mappings enter the
bundled upstream collection. The manifest targets Kodi 21/22; see its README for
generation-specific preparation and reproducibility requirements.

On Kodi `22.0rc1-Piers`, fixture XML IDs 19001–19004 produced runtime IDs
29001–29004 with null reported labels. Live MCP/Bridge observations resolved the
base window and first dialog from the exact local skin/version mapping; the
other dialogs returned ambiguous and unknown without semantic names. The base
window remained separately identified while dialogs were open. Wrong-version
and unrelated-skin extensions could not identify the active fixture window.
Malformed and core-colliding extension files were rejected without breaking
inspection. Default MCP and HTTP responses expose schema version 2 directly.
A core yes/no dialog over the custom base retains both identities and stable
cold/warm summaries. Full RELEASE validation, clean installation, wheel data
inclusion and local-extension persistence checks are recorded in the validation
report. This does not qualify every community skin for live operation.

| Skin ID | Exact versions | Kodi majors | Version-scoped entries (verified / ambiguous) | Primary sources |
| --- | --- | --- | --- | --- |
| `skin.aeon.nox.silvo` | 8.1.0; 9.1.8; 10.0.3; 11.0.0~beta6 | 19, 20, 21, 22 | 66 / 11 | [MikeSiLVO/skin.aeon.nox.silvo](https://github.com/MikeSiLVO/skin.aeon.nox.silvo), [xbmc/repo-skins](https://github.com/xbmc/repo-skins) |
| `skin.aeon.tajo` | 5.4.0; 6.7.1; 9.2.5 | 19, 20, 21 | 75 / 76 | [xbmc/repo-skins](https://github.com/xbmc/repo-skins) |
| `skin.amber` | 3.4.11; 3.50.02; 3.99.02; 4.0.0 | 19, 20, 21, 22 | 78 / 29 | [MikeSiLVO/skin.amber](https://github.com/MikeSiLVO/skin.amber), [xbmc/repo-skins](https://github.com/xbmc/repo-skins) |
| `skin.arctic.fuse` | 0.7.39~omega | 21, 22 | 43 / 13 | [jurialmunkey/skin.arctic.fuse](https://github.com/jurialmunkey/skin.arctic.fuse) |
| `skin.arctic.fuse.2` | 2.12.12 | 21, 22 | 40 / 13 | [jurialmunkey/skin.arctic.fuse.2](https://github.com/jurialmunkey/skin.arctic.fuse.2) |
| `skin.arctic.fuse.3` | 3.2.16; 3.3.12 | 21, 22 | 81 / 20 | [jurialmunkey/skin.arctic.fuse.3](https://github.com/jurialmunkey/skin.arctic.fuse.3) |
| `skin.arctic.horizon.2` | 0.8.30~nexus | 20 | 24 / 5 | [jurialmunkey/skin.arctic.horizon.2](https://github.com/jurialmunkey/skin.arctic.horizon.2) |
| `skin.arctic.zephyr.mod` | 1.1.72; 2.0.5; 3.0.3 | 19, 20, 21 | 54 / 70 | [xbmc/repo-skins](https://github.com/xbmc/repo-skins) |
| `skin.confluence` | 4.7.13; 4.8.4; 5.0.9; 5.1.2; 5.1.3 | 19, 20, 21, 22 | 16 / 3 | [xbmc/repo-skins](https://github.com/xbmc/repo-skins), [xbmc/skin.confluence](https://github.com/xbmc/skin.confluence) |
| `skin.estouchy` | 3.0.6; 3.0.8; 4.0.0 | 19, 20, 21, 22 | 6 / 0 | [xbmc/skin.estouchy](https://github.com/xbmc/skin.estouchy), [xbmc/xbmc](https://github.com/xbmc/xbmc) |
| `skin.estuary` | 3.0.5; 3.0.10; 4.0.0; 4.1.0 | 19, 20, 21, 22 | 31 / 4 | [xbmc/xbmc](https://github.com/xbmc/xbmc) |
| `skin.ftv` | 7.1.2; 8.0.1; 8.3.1; 8.4.0 | 19, 21, 22 | 30 / 21 | [Hitcher/skin.ftv](https://github.com/Hitcher/skin.ftv), [xbmc/repo-skins](https://github.com/xbmc/repo-skins) |
| `skin.mimic.lr` | 7.0.7; 8.2.2 | 19, 21 | 14 / 2 | [xbmc/repo-skins](https://github.com/xbmc/repo-skins) |
