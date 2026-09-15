# Kodi 19.5 runtime

This version-owned image installs exact Team Kodi `kodi-old-stable` Jammy
packages `6:19.5+git20221224.1006-f8fdeb6b1b-0~jammy` on the pinned linux/amd64
Ubuntu 22.04 base. Launchpad identifies the PPA signing fingerprint as
`04AE50726660A0D55355E966C8B28F649961BE33`; its signed Jammy package index
publishes the two SHA-256 values enforced by the Dockerfile.

The runtime retains the minimal non-root Xvfb, D-Bus session,
software-rendering, process-health, and disposable JSON-RPC profile model. It
does not copy the protected lab's VNC, noVNC, ports, mounts, networks, profile,
or persistent state. No Kodi-19-specific runtime workaround beyond its pinned
Jammy package/dependency set was required by the protected known-good runtime.
