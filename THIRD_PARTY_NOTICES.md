# Third-party notices

Kodi Lab's original application and Bridge code are MIT licensed. Keep the
copyright and license in [LICENSE](LICENSE) and
[bridge/service.kodi_lab_bridge/LICENSE](bridge/service.kodi_lab_bridge/LICENSE).
The distributed wheel includes the Bridge license and this notice. These terms
do not relicense Kodi or any third-party dependency.

## What Kodi Lab distributes and downloads

| Component | Distribution relationship | Applicable notices and source information |
| --- | --- | --- |
| Kodi Lab application, Web scripts/styles, Bridge and generated fixture assets | Project source/assets included in the wheel | MIT; original copyright/license texts are retained. Fixture media are generated project assets, not copies of commercial programmes. |
| Code of Conduct | Adapted documentation included in the project source | Contributor Covenant 2.1 by Coraline Ada Ehmke and contributors, CC BY 4.0. Attribution, license link and the adapted reporting instructions are recorded in [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). |
| Python application dependencies | Installed from upstream wheels into application images; not vendored into the Kodi Lab wheel | The exact dependencies are recorded in `uv.lock` and installed package metadata. Retain their upstream license/copyright files, including `.dist-info/licenses` and any NOTICE files. |
| Pillow13.0.0 and its wheel libraries | Optional Web-only JPEG preview dependency; absent from broker images | Pillow uses MIT-CMU; retain the installed wheel LICENSE and incorporated-library notices in its `.dist-info/licenses` directory. Exact artifacts and hashes are in `uv.lock`. |
| Python and Debian base packages | Included in locally built Web/broker/worker images | Python uses the PSF license and incorporated-software notices; Debian packages retain their individual terms and `/usr/share/doc/*/copyright`. An image is not licensed wholly under MIT. |
| OSTree, GnuPG and supporting system tools | Included in broker/worker images for acquisition and verification | libostree is LGPL-2.1-or-later; GnuPG is GPL-3.0-or-later; their dependent libraries have individual licenses. Keep packaged notices and provide corresponding source if redistributing these images where required. |
| Kodi 19/20/21 binaries and dependencies | Downloaded from signed Ubuntu repositories only when the user installs a reviewed runtime | Kodi is GPL-2.0-or-later, with additional component licenses. Stable recipes record exact package identities and source/license references; downloaded package notices remain in the locally assembled image. |
| Kodi 22 RC1, Freedesktop Platform and Mesa extension | Exact signed Flathub OSTree revisions downloaded on explicit user installation | Kodi is GPL-2.0-or-later; the runtime is a collection of separately licensed components. The recipe and upstream build manifests identify their sources. Preserve acquired license texts and attribution in `/app`, `/opt/fdo` and `/opt/mesa`; common Freedesktop license texts are under `/opt/fdo/share/licenses/common`. |
| FFmpeg, TagLib, Mesa, Xvfb and fonts in managed images | Obtained with upstream runtime artifacts or reviewed OS support packages | FFmpeg is LGPL-2.1-or-later or GPL-2.0-or-later depending on enabled components; Mesa is predominantly MIT; X.org/Xvfb contains MIT/X11 and other permissive notices; TagLib offers LGPL-2.1-or-later or MPL-1.1. Fonts and auxiliary libraries retain their own package licenses. |
| pytest, Ruff, mypy, Hatchling, uv, Playwright/Chromium and other development tools | Development/build/test dependencies, not part of the Kodi Lab application wheel | Their licenses apply to copies actually distributed. Do not describe development-only tools as application runtime dependencies; retain notices if shipping a development environment. |

Application dependency licenses include MIT/MIT-0, BSD-2-Clause/BSD-3-Clause,
Apache-2.0, PSF-2.0 and MPL-2.0. Examples include FastAPI/Pydantic/SQLAlchemy/MCP
(MIT), Starlette/Uvicorn/HTTPX/Jinja (BSD), Docker SDK/python-multipart/Requests
(Apache-2.0), cryptography (Apache-2.0 OR BSD-3-Clause), typing_extensions
(PSF-2.0), greenlet (MIT AND PSF-2.0), and certifi (MPL-2.0). The installed
artifacts' full license texts, copyright statements and additional notices are
authoritative; this summary is not a substitute for them.

Kodi Lab distributes reviewed recipes, code and trust materials, **no Kodi
executable binaries**. Installation downloads exact approved upstream artifacts,
verifies them and assembles a Docker image in the user's environment. Additional
managed instances reuse that installed image. Kodi remains available separately
and free of charge from [the official download page](https://kodi.tv/download/).

## License requirements

Keep required copyright, license and attribution notices with copies. MIT/BSD
notice retention, Apache-2.0 license/NOTICE and modification provisions, Python's
incorporated-software notices, and applicable MPL/GPL/LGPL obligations still
apply to the components actually distributed. On-demand acquisition does not
remove obligations for bundled dependencies or for anyone subsequently
redistributing an assembled image.

Local private use/assembly does not require publishing private changes under the
GPL. Anyone distributing GPL/LGPL binaries must satisfy the relevant
corresponding-source provisions using source that matches the distributed
binaries and build modifications; a generic project link or an SBOM alone is
not sufficient. LGPL redistribution also requires the applicable replacement/
relinking rights. Upstream package/build manifests and source repositories supply
provenance; preserve a compliant source delivery mechanism before distributing
images. Kodi Lab's current runtime workflow does not distribute those images.

This is factual component/distribution documentation, not a recurring manual
license-review requirement for each ordinary Kodi release. A change in what is
actually bundled or distributed can create different obligations.

Primary references: [Kodi licenses](https://github.com/xbmc/xbmc/blob/master/LICENSE.md),
[GPLv2](https://www.gnu.org/licenses/old-licenses/gpl-2.0.html),
[LGPLv2.1](https://www.gnu.org/licenses/old-licenses/lgpl-2.1.html),
[MIT](https://opensource.org/license/mit),
[BSD-3-Clause](https://spdx.org/licenses/BSD-3-Clause.html),
[Apache-2.0](https://www.apache.org/licenses/LICENSE-2.0),
[Python license](https://docs.python.org/3/license.html),
[FFmpeg licensing](https://ffmpeg.org/legal.html),
[Mesa licensing](https://docs.mesa3d.org/license.html), and
[TagLib licensing](https://taglib.org/).

## Branding and any future binary redistribution

Kodi is a trademark of the XBMC Foundation. Kodi Lab is independent of, and is
not affiliated with, endorsed by or representative of Team Kodi or the XBMC
Foundation. Downloading Kodi does not require obtaining Kodi Lab.

The Foundation's [trademark policy](https://kodi.wiki/view/Official:Trademark_Policy)
specifically restricts third-party addon/repository wordmarks and software that
reconfigures Kodi installations. No permission for the **Kodi Lab** application/
Bridge branding is recorded in this repository. The application name and the Bridge/repository addon names need a recorded
resolution before the first public release: obtain Foundation permission or
review a rebranding plan. An independent-development source checkpoint is not
a public release or an assertion of permission; reassess before making a private
repository public. This question is separate from copyright licensing and is
not a per-Kodi-version review.

Historical prebuilt runtime archives are private development artifacts, absent
from the distributed bundle/wheel. Redistributing prebuilt images would require
matching source delivery and compliance with the applicable
[Kodi distribution policy](https://kodi.wiki/view/Official:Distribution_Policy),
including branding. That is not the on-demand installation workflow qualified here.

The optional experimental noVNC client vendors noVNC 1.7.0 from the upstream tagged
release. Its MPL-2.0 license and component notices accompany the static assets
under `src/kodi_lab/web/static/vendor/novnc/`; `SOURCE.json` records the archive
and individual-file SHA256 identities. The optional runtime recipe installs
TigerVNC from signed Ubuntu packages. TigerVNC and dependency licenses and
source-offer obligations remain those supplied by Ubuntu and upstream; no
prebuilt runtime image is published by this prototype.
