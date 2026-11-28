# Managed Flathub Omega 21.3 pilot

The Claw pilot is **partial**. The broker built the image and both native validation starts reached health and both stops exited zero. Catalog registration failed because the installer used Piers' prerelease tier for stable Omega. The source correction retains exact pilot consent and image admission. A fresh managed Install and all 18 managed reliability cycles remain pending. [Qualification evidence and disposition](https://github.com/kcook98765/kodi-lab/issues/482)

The pilot adds an opt-in Linux amd64 Omega recipe to the existing reviewed OSTree acquisition path. The normal Omega recipe remains Ubuntu Resolute r6. Piers maintains its exact RC2 inputs, consent and admission checks. Kodi Lab consumes the externally built Kodi executable and its matching Freedesktop loader, CPython and graphics stack. The packaged Web and broker include the recipes and assembly assets, and the ordinary binary-free bundle still carries the standard Ubuntu recipe inventory.

On a separate empty site, set `KODI_LAB_FLATHUB_OMEGA_PILOT=true` for the packaged Web and broker. In Kodi Runtimes select the Flathub pilot and accept its exact recipe acknowledgement. Install uses the existing detached broker worker, signature and inventory checks, offline image assembly, two-cycle native validation and image-bound installation receipt. Prepare uses the resulting reviewed image. The setting is part of this pilot site's configuration while it retains this runtime. Ordinary users need Docker and the regular Kodi Lab bundle. The acquisition workflow requires no host Flatpak package.

## Package inputs

| Input | Pinned identity |
| --- | --- |
| Installation recipe | `omega-21.3-flathub-pilot` revision 5, `sha256:284dcf3a926a6cb3e7e42e1a9ca61564363fade256ac3e3e3b067c088e231144` |
| Application | `app/tv.kodi.Kodi/x86_64/stable`, `f125d3e688cc8c69b11163b5b55cd080847fbdcb77ba437607f5c32e2da0f071` |
| Platform | `runtime/org.freedesktop.Platform/x86_64/26.08`, `df26daaf9d07610b4a72cc4585566194051275ac68396597b06474396f66427c` |
| Software GL | `runtime/org.freedesktop.Platform.GL.default/x86_64/26.08`, `5aec1b8281d53c91bdac451eb577ec5331ca0e34d89cf0dcc8dba2360171eae6` |
| Signing key | Flathub primary `6E5C05D979C76DAF93C081354184DD4D907A7CAE`, signing subkey `54A6CDDD8919FB204200D8AC562702E9E3ED7EE8` |
| Kodi | 21.3 stable, Team Kodi source `a3a448d26b8d560a65655dab2cd122994dc4e146`, native revision `20251031-a3a448d-dirty` |
| Kodi executable | SHA-256 `726ef8c726aeb4fa4cb3321b9e8e68026c4412520f2d41ca5978b7d20c55b82c`, ELF build ID `d2faedeab86696e85aa9cdaa1a014e93a3de7d1c` |
| Interpreter and media | CPython 3.14.9, `libpython3.14.so.1.0`, Kodi Python API 3.0.1, FFmpeg 6.1.6 |
| Docker support | Same reviewed 116-package Ubuntu Resolute support closure as Piers RC2, including Xvfb and TigerVNC |

The Flathub source includes the three upstream thread-state corrections in the reviewed packaging commit. Identical signed input commits and package checksums reproduce the same input closure while those objects remain available. Operation IDs and build metadata are part of each receipt, so independently assembled OCI image IDs require their own validation and pin. [Flathub packaging commit](https://github.com/flathub/tv.kodi.Kodi/tree/3c4b3b82ca8d10f16520ec84d7c6e6992f6f523e) · [Flathub backport #825](https://github.com/flathub/tv.kodi.Kodi/pull/825) · [Kodi startup issue #28381](https://github.com/xbmc/xbmc/issues/28381)

## Updates, migration and rollback

Review every app, platform and GL refresh as a new pinned recipe. Verify ref, collection, signing key, critical binaries, upstream source and applicable corrections. Re-run cold acquisition, native validation and managed acceptance before promotion. Check platform support lifecycle and artifact retention, and fail closed if an upstream pin is unavailable. Piers and Omega share the worker and Ubuntu support closure. Their exact OSTree commits differ, and the worker uses per-operation acquisition staging. Budget for both payloads and temporary assembly artifacts on sites that retain both images.

Before proposing an ordinary-install default change, complete Linux and Windows/WSL2 Docker Desktop acceptance. Windows needs its own cold worker and Docker storage measurements, mount and UID checks, network policy, Bridge, noVNC, recording, Stop and checkpoint acceptance. Require separate owner approval for changing the default.

For an existing site, retain the exact Ubuntu r6 image, verified profile backup and original checkpoint bindings. Qualify the successor in a fresh profile or reconstruct it through supported package and data transfer. Use the original r6 pin and its verified backups to roll back. Named checkpoints require their original image and valid archive digest. Reinstalling a recipe can create another image identity, so preserve the actual image referenced by recovery assets. [Managed profile recovery](MANAGED-V2-FRESH-INSTALL.md) · [Windows/WSL2 deployment](WINDOWS-WSL2-INSTALL.md)
