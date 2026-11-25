# Disposable Matrix 19.5 qualification

The explicit Linux amd64 candidate recipe is `ubuntu-jammy-matrix-19.5-candidate`
revision 5. Its audited acquisition manifest seals the Team Kodi Jammy packages
and the Ubuntu dependency closure. The ordinary Matrix selection remains 19.4.
The candidate uses the same base and exact Matrix r4 display and supervisor assets.
A dedicated broker may accept the candidate only after an operator admits the
exact image ID against the sealed candidate recipe. The working profile and
named snapshots continue to bind to that image ID.

## Acquire and assemble

Use a fresh disposable Docker Engine and the `linux/amd64` manifest of
`ubuntu:22.04@sha256:281c5745f657873d78e5531fc5ba8575f46ab7769b94550ac99543f122679986`.
Extract the Ubuntu archive keyring from the reviewed image. The acquisition
command will verify the keyring bytes before using it. The host tools are Python,
GnuPG's `gpgv` and Docker. All package installation runs in the image build.

```sh
uv run --locked python -m kodi_lab.runtime.matrix_candidate acquire \
  "$trial/debs" "$trial/base-keyring.gpg"
uv run --locked python -m kodi_lab.runtime.matrix_candidate context \
  "$trial/debs" "$trial/matrix-context.tar"
docker build --platform linux/amd64 --network none --pull=false --no-cache \
  --tag kodi-lab-matrix-19.5-disposable:qualification - < "$trial/matrix-context.tar"
```

The acquisition checks the exact signed PPA InRelease and compressed binary
index. It accepts only `kodi` (all) and `kodi-bin` (amd64) from that archive,
both `6:19.5+git20221224.1006-f8fdeb6b1b-0~jammy`. The other packages come
from the signed 2026-11-24T12:00:00Z Ubuntu Jammy snapshot. Fetching each DEB
checks its signed index entry, size and SHA-256. The installer receives only
those local DEBs and checks both the base and complete installed inventories.
A fresh acquisition and assembly must produce the same closure, package bytes,
Kodi identity and runtime assets. Docker image IDs may differ between builds.

Use controlled local add-ons and media. The exact PPA binary embeds FFmpeg
4.3.2-Matrix-19.2. Jammy package updates cannot refresh that embedded copy.
An owner decision on media exposure, patch coverage and source retention is
required before the candidate can replace the production slot. Preserve the
19.4 image, working profile and snapshots together for rollback.

## Acceptance expectations

Use a fresh profile, authenticated Narrow Bridge 1.1.35 and the ordinary
managed lifecycle. Record stable native Kodi 19.5 at upstream
`f8fdeb6b1b1a7b753e1559d34a98cea62b4920d9`, the embedded interpreter ABI
`libpython3.10.so.1.0`, and the actual Python, OpenSSL and renderer versions.
Verify Prepare, Start, Web and authenticated MCP status, live viewer and control,
native screenshot, private catalog dependency resolution, higher-version native
update, direct deployment, Python execution, snapshot save and restoration,
and Stop and ownership release. A 19.4 snapshot must remain bound to its original
image. Exercise TV Guide Fullscreen with exact approved ZIPs and local fixtures.
Record a crash once with its native exit evidence and the last successful step.

The Kodi Agent Toolkit already qualifies Kodistubs 19.0.3 against the exact
19.5 source in its CPython 3.12 static lane. Future cutover should refresh the
native-runtime evidence references and version expectations while retaining the
stub lock and historical 19.4 records.

## Windows and WSL 2 qualification plan

On a dedicated Windows host with an AMD64 WSL 2 Linux distribution, use a fresh
Docker Engine and verify the same base manifest, closure, image labels and native
Linux Kodi/Python identities. Confirm installer capacity and ordinary startup,
verify local Web/MCP authorization and Chromium noVNC ownership, and exercise
the same private-feed install, update, direct deployment, snapshot round trip and
graceful Stop. Use a disposable WSL filesystem and preserve the existing Windows
and Linux profiles. Repeat the bounded local TV Guide startup and reopen check.

This plan covers the same Linux binary under WSL 2. The Windows Kodi executable
requires its own signed artifact and native ABI qualification.
