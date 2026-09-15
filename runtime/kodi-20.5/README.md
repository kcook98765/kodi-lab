# Kodi 20.5 runtime

This version-owned image installs the exact official Ubuntu Noble package
`2:20.5+dfsg-1ubuntu1` on the pinned linux/amd64 Ubuntu 24.04 base. It retains
the minimal Phase 3 Xvfb, D-Bus session, software-rendering, process-health, and
disposable JSON-RPC profile architecture. It adds no ports, volumes, mounts,
VNC, devices, capabilities, or caller-controlled command surface.

Kodi 20.5's Estuary skin requests Roboto Thin, but the Noble Kodi package does
not install that font beneath Kodi's media font directory. The image therefore
owns one compatibility symlink from
`/usr/share/kodi/media/Fonts/roboto-thin.ttf` to the file provided by the exact
`fonts-roboto-unhinted` package. This accommodation is local to Kodi 20.5.
