# Third-party notices and runtime distribution status

Kodi Lab's original application code is MIT licensed; see [LICENSE](LICENSE).
The original Kodi Lab narrow bridge is also MIT licensed and carries its own
license text inside the add-on source and ZIP. This notice does not change the
licenses of third-party software.

The V1 release bundle carries reviewed Matrix 19.4, Nexus 20.5, and Omega
21.3 recipes and notices, not Kodi binaries. Signed Ubuntu binary packages
are acquired in the user's Docker environment and assembled locally. Each
packaged recipe records its upstream provider, suite, package identities, and
source/license references; these do not replace per-package license review.

| Component | Relationship to this project | License and review status |
| --- | --- | --- |
| Kodi Lab application | Original source | MIT; root `LICENSE` |
| Kodi Lab narrow bridge | Original add-on source | MIT; `bridge/service.kodi_lab_bridge/LICENSE` and `addon.xml` |
| Kodi | Acquired in user-assembled runtime images | Third-party Kodi project; per-runtime license, copyright, source, and trademark mapping pending |
| Ubuntu and base packages | Base OS and packages in runtime images | Third-party package terms; per-runtime inventory and corresponding-source review pending |
| Python | Interpreter and packages in application and runtime images | Third-party terms; exact version and dependency license mapping pending |
| Mesa and Xvfb | Display and rendering packages in runtime images | Third-party package terms; per-runtime inventory pending |
| Other bundled/runtime dependencies | Python, system, and add-on dependencies | Per-artifact SBOM and license mapping pending |

The historical prebuilt Kodi runtime archives remain private development
artifacts and are absent from the V1 bundle. A separate compliance gate still
owns per-runtime SBOMs, license and copyright mapping, corresponding source,
source retention, and presentation/trademark review. No license label on Kodi
Lab original code relicenses Kodi or a runtime dependency.

Kodi Lab is independent of and not affiliated with the Kodi project. The use
of Kodi in project names and documentation remains subject to a separate naming
and trademark review before public visibility.
