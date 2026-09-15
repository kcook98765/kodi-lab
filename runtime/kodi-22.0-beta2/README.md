# Kodi 22.0 Beta 2 Preview runtime

This version-owned Preview image builds official Team Kodi tag
`22.0b2-Piers`, commit `e513e0ff4331fc25fd2454659a9dd3e6b7670146`,
from the SHA-256-pinned official GitHub source archive. The source identifies
itself as Kodi `22.0-BETA2` (`21.90.802`). It is a repository-controlled
Trusted Custom Build: global custom-build opt-in and per-revision confirmation
remain independent requirements in addition to Preview and validation policy.

The first stage contains the Ubuntu build toolchain and performs the X11/OpenGL
Release build. Internal FFmpeg, Exiv2, nlohmann-json, and patched TagLib 2.3.1
come from versions and hashes declared by the pinned Kodi source. The final
image copies only `/opt/kodi`. Ubuntu's Kodi packages are used transiently to
resolve the distribution runtime-library closure and are purged before the
source build is copied, leaving no second Kodi payload. The image does not
retain compilers or the build tree. Exact build and final runtime package lists
are embedded under `/opt/kodi/share/kodi-lab/` for provenance without claiming
bit-for-bit reproducibility.

Runtime behavior remains non-root Xvfb, bounded D-Bus, software rendering,
fixed process health, and a disposable seeded JSON-RPC profile. It copies no
legacy VNC/noVNC, ports, mounts, networks, profiles, or persistent state.
