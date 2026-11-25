# Managed Matrix 19.5

Kodi Lab selects the official Team Kodi Ubuntu Jammy build for its one managed
Matrix slot on Linux amd64. Kodi Lab assembles a container from published
binaries. The Stage 21 recipe still uses its original ID
`ubuntu-jammy-matrix-19.5-candidate` revision 5. Keeping that identity preserves
the reviewed recipe digest, display assets and package closure.

## Legacy-runtime security

**Use Matrix for legacy development with reviewed add-ons and trusted media.**
The Team Kodi binary embeds FFmpeg 4.3.2-Matrix-19.2. The reviewed source lacks
the fixes for CVE-2024-7055, CVE-2025-0518 and CVE-2023-49502. Ubuntu FFmpeg
updates cannot refresh the embedded copy. The historical PPA has no established
maintenance commitment. The owner accepted these limitations for the managed
Matrix lane. [Owner decision and Stage 27 assessment](https://github.com/kcook98765/kodi-lab/issues/415)

## Acquisition and acceptance

Use **Kodi Runtimes → Matrix → Install** to acquire and validate the runtime.
The recipe pins the Ubuntu 22.04 linux/amd64 base manifest, both Team Kodi DEBs
at `6:19.5+git20221224.1006-f8fdeb6b1b-0~jammy`, and the complete closure from
the signed Ubuntu Jammy snapshot dated 2026-11-24T12:00:00Z. The acquisition
helper verifies the pinned signing keys, signed metadata and package hashes.
The offline worker rechecks the package bytes. The network-disabled build
compares the complete base and installed package inventories. The ordinary
installer validates and records the resulting Docker image ID before handoff.
Repeat builds can yield different image IDs.

Stage 21 verified the sealed package closure and the representative managed
Linux workflow. Read Kodi's native version and upstream revision
`f8fdeb6b1b1a7b753e1559d34a98cea62b4920d9` for the selected image. Verify
ordinary Prepare, Start, authenticated Bridge and MCP, Web/noVNC and screenshot,
Python add-on execution, dependency installation and update, stopped deploy,
snapshot save/load and graceful Stop. Retain focused checks tied to the selected
image and use prior exact evidence for repeated coverage.
[Stage 21 results](https://github.com/kcook98765/kodi-lab/issues/415#issuecomment-7154971721)

## Windows and WSL 2

Matrix 19.5 is qualified for Linux amd64. Windows/WSL 2 needs separate native
acceptance before Lab claims support for this runtime. Use a disposable
Windows 11 / Ubuntu WSL 2 Docker Desktop environment. Verify installer
capacity, package acquisition, exact Kodi/Python identity, authenticated Web
and MCP, noVNC, dependency install/update, stopped deploy, snapshot round trip
and graceful Stop. Record the Windows, WSL, Engine and Compose versions.
Windows-native Kodi uses a separate binary and has its own provenance.
