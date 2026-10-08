# Add Kodi installation instructions

This uncommitted presentation slice adds one native disclosure, **Install Kodi
Lab Bridge**, above the unified mode selector. It applies to both Home and
Development. The existing pairing scripts, recovery keys, endpoints, target
classes and authorization are unchanged.

## Verified existing delivery

The configured File manager source URL is:

`http://192.168.2.191:18120/install/`

The existing stable generator creates the repository bootstrap ZIP and reviewed
feed in a generation directory selected by `CURRENT`. The existing read-only
repository server exposes that ZIP through its `/install/` listing and exact
download route. Add Kodi does not generate, publish or serve those packages.

Read-only inspection of the existing repository container's configuration and
HTTP responses verified the following path:

1. `/install/` lists `repository.kodi-lab-1.0.0.zip`.
2. That ZIP installs `repository.kodi-lab`, displayed as **Kodi Lab**.
3. Its descriptor points to `/stable/addons.xml`, its SHA-256 checksum and
   `/stable/zips/` on that same configured repository origin.
4. The stable index supplies `service.kodi_lab_bridge`, displayed in Kodi as
   **Kodi Lab Narrow Bridge**. This is the Kodi Lab Bridge add-on; its
   `narrow_bridge.py` implementation is inside that one package.
5. Its service extension runs `service.py` at Kodi login. Its separate
   **Enable bridge** setting defaults to false. Instructions therefore cover
   enabling the add-on, enabling the Bridge in Configure, saving and restarting
   Kodi locally to start the service.
6. The Configure action **Pair Kodi Lab** invokes the package's `pairing_ui.py`.
   It shows the current code; **Generate new code** is used when no code is
   active. The four-character code expires in five minutes.

The repository ZIP, index and actual published Bridge ZIP were downloaded only
for read-only verification. The index/package checksums matched. No Kodi was
installed, restarted, probed or paired. No repository generation or publication
was performed. Full HTTP statuses, paths, hashes and extracted metadata are in
`repository-verification.json` in the evidence folder below.

**Version limitation at installation-guidance implementation:** the live stable feed publishes Bridge
**1.1.14**. The checkout and its Home/production onboarding version check require
**1.1.15** (`SUPPORTED_V1_BRIDGE_ADDON_VERSION`). The older feed does not satisfy
that check, irrespective of a correct repository installation. A separately
authorized reviewed repository/build update is needed before real Home pairing.
This slice does not publish or approve a new build, change the version check, or
claim that the older published package meets current Home trust requirements.
Development continues to use its original service and verification contract.
The later official-build recognition correction removes the normal administrator
review wording. Official compatible builds now register automatically from the
verified local stable feed at startup, while version/custom-build information is
under Advanced compatibility details. The live 1.1.14 feed remains unchanged;
publication of the staged 1.1.15 artifact and deployment of the new backend still
need separate authorization. See [Official Bridge recognition](OFFICIAL-BRIDGE-RECOGNITION.md).

## Presentation and URL handling

Add Kodi reads the same `KODI_LAB_REPOSITORY_BASE_URL` used by repository delivery.
It appends the existing `/install/` route and uses the delivery module's
`REPOSITORY_ID`/`REPOSITORY_VERSION` constants for the repository ZIP name.
The URL comes from deployment configuration, not the browser's Web origin.
An absent or invalid origin shows an explicit unconfigured notice, with no
invented URL or Copy button. Credential-bearing, loopback, path/query/fragment,
invalid-port and whitespace origins are rejected for display. Page rendering
does not contact Kodi or the repository server and does not claim live health.

The preview's previously absent repository base configuration now points to the
verified existing repository. Only that preview environment file was updated;
no publisher, production service configuration or repository data was changed.
The preview is not another repository distribution system.

The disclosure contains nine numbered Kodi-side steps, the source URL and
**Copy URL**, Unknown sources guidance, the actual repository/add-on names,
Bridge start and pairing actions, plus native web-server setup. Kodi's
**Settings → Services → Control → Allow remote control via HTTP** exposes its
HTTP JSON-RPC connection. Port 8080 and optional existing native credentials
match the current forms; authentication stays enabled where available.
Settings level Standard or higher makes those controls available.
The instructions mention reachability of the native port and Bridge port 8765
on the private network. They do not require the unauthenticated application
control/TCP interfaces. See the official
[Kodi Control settings](https://kodi.wiki/view/Settings/Services/Control) and
[Add-on manager](https://kodi.wiki/view/Add-on_manager).

Copy URL first uses the clipboard API when available, then user-gesture selected
text copying for the existing private-LAN HTTP preview. A denied/unavailable
clipboard selects the URL and gives manual copy instructions. The status is
announced; successful copy restores focus to the button. Without JavaScript,
the native disclosure and selectable readonly URL remain available, and the
nonfunctional Copy button stays hidden. No persistent browser storage is used
by this script. URL and Copy button stack on phones/tablets at narrow widths.

## Files in this slice

- `src/kodi_lab/web/app.py`
- `src/kodi_lab/web/templates/add-kodi.html`
- `src/kodi_lab/web/static/add-kodi-installation.js` (new)
- `src/kodi_lab/web/static/style.css`
- `tests/web/test_add_kodi_installation.py` (new)
- `tests/web/add_kodi_installation_browser.cjs` (new)
- `docs/WEB-ADD-KODI-INSTALLATION.md` (new)
- Outside Git: `/home/kyle/kodi-lab-web-preview/preview.env`, only the existing
  repository-base setting added.

Earlier uncommitted Implementation 6A and unified onboarding files are retained.
HEAD remains `3ff38c45d5ac3bb99e75a7736122b8f88e3810d2`.

## Validation and read-only review

FOCUSED PASS: 71 tests cover the new controller file individually (14 tests), existing
Add Kodi (4), unified onboarding (5), legacy pairing (19), navigation (24),
and repository administration (5). Ruff format/lint, strict mypy and JavaScript
syntax checks run on the affected source/tests. The existing workflow harnesses
and real browser regression use fixture responses and never send pairing to a
Kodi. This is focused presentation validation, not real pairing acceptance.

The new browser runner verifies both modes at 1440, 820 and 375 px, shared nine
steps, exact configured source URL, keyboard disclosure/focus, 44 px controls,
overflow, WCAG A/AA axe, no-JavaScript selection, actual Chromium clipboard
readback after HTTP Copy URL, denied API fallback and failed-copy selection.
It records seven checks/screenshots and six zero-violation axe scans. The
clipboard API success branch is also exercised with a mock because this preview
uses ordinary HTTP.
The existing unified browser runner rechecks both workflows and recovery at
all three widths with mocked responses, including legacy pairing compatibility:
33 checks, 30 screenshots and 30 zero-violation axe scans. Combined browser
evidence has 37 screenshots and 36 zero-violation axe scans.

Evidence folder:
`/home/kyle/kodi-lab-web-preview/evidence/add-kodi-installation/`

It contains the repository verification, controller/static logs,
`browser-report.json`, screenshot gallery `index.html`, and the
`onboarding-regression/` browser report/screenshots. Preview business-state row
counts and hashes are compared before/after browser review, excluding normal Web
authentication sessions. The target inventory stays empty.

```console
./scripts/check-focused tests/web/test_add_kodi_installation.py tests/web/test_add_kodi.py tests/web/test_unified_add_kodi.py tests/web/test_pair_kodi_web.py --source src/kodi_lab/web/app.py
KODI_LAB_BROWSER_TOOLS=/home/kyle/kodi-lab-web-preview/browser-tools/node_modules node tests/web/add_kodi_installation_browser.cjs
```

Actual review URLs:

- http://192.168.2.191:8180/add-kodi
- http://192.168.2.191:8180/add-kodi?mode=development

Both preview systemd services remain active. Only the isolated Web preview was
restarted for the new presentation context; production/repository services were
not restarted. For visual review, sign in, expand Install Kodi Lab Bridge, copy
the URL, switch modes, resize and use the keyboard. Do not perform the Kodi-side
installation or submit connection/recovery forms during this read-only review.
Changes remain uncommitted for feedback.
