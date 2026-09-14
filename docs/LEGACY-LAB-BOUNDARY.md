# Legacy lab coexistence boundary

The existing compatibility lab remains intact until the new Kodi Lab creates
and validates its first replacement runtime and retirement is separately
authorized.

Protected components include the four Kodi containers and active profiles,
their four images and networks, `<legacy-network>`, `<legacy-proxy>`, all
legacy VNC/noVNC/JSON-RPC/bridge publications, Nginx Kodi browser configuration,
`kodi-lab-broker.service`, `<legacy-lab-root>`,
`<legacy-runtime-root>`, `<legacy-broker-state>`, the stopped
`<legacy-sidecar>` container and volume, retained Hermes image/control-plane
paths and identities, and NAS preservation archives.

New code must not reuse legacy runtime-directory, service, container, or network
names during coexistence. The full port, network, path, profile-size, and
preservation inventory is authoritative in `PROJECT-HANDOFF.md`.
