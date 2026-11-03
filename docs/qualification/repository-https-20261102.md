# Optional repository HTTPS focused qualification

Issue #193. Base: `1bc363d85cbff7b75e11d5c2d417e05cc6985527` (current main
when fetched). Independent branch `feat/optional-repository-https`; no commits
from open drafts #191/#192. Fresh installation configuration only.

## Result and implementation

**FOCUSED PASS**, with native device acceptance unrun. Existing generators,
bootstrap descriptors and the read-only server already carried HTTPS origins.
The missing pieces were portless HTTPS acceptance in bundle configuration and
stable generation, and external origin verification during host installation.

Default private-LAN HTTP, normally port 18120, retains its existing flow.
Optional `--repository-base-url https://repo.example.net` uses standard port 443;
explicit HTTPS ports also work. The backend retains its private HTTP listener,
read-only volume, exact route allowlist and existing package/provenance checks.
No Compose, Bridge protocol, publication authorization, migration, upgrade,
repair, certificate-management or production change was introduced.

After services start, HTTPS installation reuses the installed image's repository
verifier to obtain trusted local provenance. Public catalogs, checksum files,
bootstrap ZIPs and package ZIPs/sidecars for both feeds must match its SHA-256
values before `ready` is recorded. Downloads have byte/time bounds, normal TLS
certificate/hostname validation, no environment HTTP proxy, no redirects and no
HTTP fallback. Failure preserves the installation at `services_started`.
This external download check is specific to the host installer's fresh HTTPS
path; manual bundle configuration validates syntax and requires manual checks.

## Focused checks

Each listed file was invoked separately through `./scripts/check-focused`.
After review fixes, the new transport file and affected stable tests were
rerun; totals below count each selected case once. Final focused Ruff
format/lint and strict mypy cover all ten test files and the three changed
Python sources (including extensionless `scripts/linux-v1-bundle`).

| Test file | Passed |
| --- | ---: |
| `tests/packaging/test_repository_https.py` | 54 |
| `tests/packaging/test_host_installer.py` | 73 |
| `tests/packaging/test_linux_v1_bundle.py` | 45 |
| `tests/packaging/test_standalone_installer_bootstrap.py` | 12 |
| `tests/packaging/test_https_ingress_installer.py` | 54 |
| `tests/packaging/test_repository_listener_preflight.py` | 5 |
| `tests/repository/test_stable.py` | 13 |
| `tests/repository/test_development.py` | 24 |
| `tests/repository/test_server.py` | 2 |
| `tests/repository/test_address.py` | 9 |
| **Total** | **291** |

The new fixture uses a disposable Python TLS reverse proxy on an ephemeral
loopback socket and the real HTTP repository handler on another ephemeral
socket. It forwards the original Host and HTTPS forwarding headers. Tests
download the real reviewed Bridge, both bootstrap packages, both catalogs and
checksums, and an approved synthetic development publication. No production
configuration, socket, database, volume or credentials are used. A test-only
certificate is trusted only through the test process environment.

Coverage includes missing CA trust, expired certificates, wrong certificate
hostname, HTTPS-to-HTTP redirect rejection, missing routes and altered bytes.
Portless HTTPS is tested through actual fresh generation and verification of
both feeds. Compose is rendered without starting containers and retains the
exact private HTTP backend mapping with the HTTPS advertised origin.

Existing regression files preserve package integrity, publication approval,
repository route restrictions, listener preflight, default HTTP installation,
Windows loopback overlay and standalone bootstrap argument forwarding. Existing
test warnings concern Starlette's deprecated AnyIO alias and intentional
duplicate ZIP fixture entries. Initial fixture/style failures were corrected;
no test was skipped to obtain this result.

## Kodi compatibility and trust

Packaging checks exercise both transports and both descriptors against the
19/20/21/22 minimum-version contract, catalog SHA-256 and package sidecar URLs.
Upstream source inspection confirmed the repository parsing and HTTPS client
paths in:

- [Matrix 19.5 repository](https://github.com/xbmc/xbmc/blob/19.5-Matrix/xbmc/addons/Repository.cpp)
  and [CurlFile](https://github.com/xbmc/xbmc/blob/19.5-Matrix/xbmc/filesystem/CurlFile.cpp).
- [Nexus 20.5 repository](https://github.com/xbmc/xbmc/blob/20.5-Nexus/xbmc/addons/Repository.cpp)
  and [CurlFile](https://github.com/xbmc/xbmc/blob/20.5-Nexus/xbmc/filesystem/CurlFile.cpp).
- [Omega 21.3 repository](https://github.com/xbmc/xbmc/blob/21.3-Omega/xbmc/addons/Repository.cpp)
  and [CurlFile](https://github.com/xbmc/xbmc/blob/21.3-Omega/xbmc/filesystem/CurlFile.cpp).
- [Pinned Piers preview repository](https://github.com/xbmc/xbmc/blob/28ea2eac1eb7af8fdcbd2672d933ce87f594be79/xbmc/addons/Repository.cpp)
  and [CurlFile](https://github.com/xbmc/xbmc/blob/28ea2eac1eb7af8fdcbd2672d933ce87f594be79/xbmc/filesystem/CurlFile.cpp).

This supports protocol compatibility; it is not native acceptance. Kodi build
CA stores can differ from browser/system stores. Every device must trust the
certificate chain and use its matching hostname. No CA is installed by Kodi Lab.
Existing development publication compatibility labels remain Matrix/Nexus/Omega;
Piers transport support does not broaden that publication policy.

## Independent review and integration notes

OpenCode 1.19.3 performed an independent static read-only review. Its first pass
found the generation-time explicit-port requirement and a relative guide link
that would break in distributed `INSTALL.md`. Both were fixed, including real
portless feed-generation tests and an absolute documentation link. Follow-up
review returned **CLEAR**. Audited tool calls were only read/glob/grep; shell,
edits, MCP and delegation were denied.

Draft #191 changes Web probe behavior at the same installer readiness location;
integration must retain its Web probe arguments and this separate repository
check before `ready`. It also changes `scripts/linux-v1-bundle`, whose repository
validation change must be retained. Draft #192 edits README and installation
guides and introduces `WEB-NETWORKING.md`; retain the dedicated repository guide
and its explicit security boundary when reconciling those documentation edits.
Neither draft's commits or Web networking behavior are included here.

No full suite, native Matrix/Nexus/Omega/Piers campaign, native Windows/WSL2
installation, image build, Nginx activation, merge or deployment was performed.
Running Claw services were left untouched. No owner decision is required for
this draft; native device certificate trust remains an installation-specific
acceptance check.
