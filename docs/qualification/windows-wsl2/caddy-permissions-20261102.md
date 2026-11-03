# Windows Caddy installation permissions

Base: `f5eb49a174f0253934b79d3e86f5001a132404a1`, including merged
PRs #186 and #187. Qualification: **FOCUSED PASS** on Claw, Ubuntu 24.04.

## Cause and correction

The owner reported that Windows acceptance needed two scoped corrections:
the generated Caddyfile was `1000:1000`, mode `0600`, and new Caddy volume
roots were `0:0`, mode `0755`. Caddy runs as `10003:10003`. The source wrote
the Caddyfile using the caller's umask and delegated volume initialization to
Docker, without preparing either mount for that identity. The pinned image
also contains empty root-owned `caddy` directories with mode `1777`.
The Windows logs are not present on Claw; their reported permissions are
consistent with these source and Linux Docker findings.

Fresh and successor configuration now create the canonical Caddyfile as
`0640`. One shared preparation function preserves its installation-owner UID
and assigns group `10003`. A disposable helper uses the existing pinned Caddy
image, no network, no Docker socket, a read-only root filesystem, all
capabilities dropped except `CHOWN`, and no-new-privileges. It mounts only
the site Caddyfile and the two verified Caddy volumes.

Volume names, Compose project/role labels, local driver, local scope and
absence of driver options must match before mounting. Only empty `0:0:0755`
roots, optionally containing the official image's empty `0:0:1777` skeleton,
may be initialized. The roots and their empty `caddy` directories become
`10003:10003:0700`. Modes are set before ownership changes; no recursive
ownership operation is used. Creating the private `caddy` directories also
prevents later Docker image copy-up into empty volumes.

Initialized roots and `caddy` directories must already be
`10003:10003:0700`. Reuse does not chmod/chown them or touch certificates.
Unexpected top-level contents, symlinks, identities or permissions fail
instead of authorizing broad repair. A second helper with UID/GID `10003`,
no capabilities and a read-only Caddyfile bind verifies its actual SHA-256
and effective volume access through the Docker engine. This checks the
Docker Desktop view rather than relying solely on WSL host metadata.

Fresh installation and a successor first enabling HTTPS may create missing
volumes. HTTPS successor reuse and normal startup refuse missing volumes,
preventing silent CA replacement. Successor configuration prepares only
Caddy filesystem permissions; it does not activate services or perform the
application transition. Startup, including Caddy-only `compose up`, repeats
the shared validation without changing established permissions.

Canonical Caddyfile/overlay bytes and hashes, site receipt schema/ancestry,
volume names, network addresses, image pin, proxy trust and running Caddy
capabilities are unchanged. The Linux default takes no Caddy permission path.

## Focused verification

149 selected pytest cases passed, each file run separately:

- `test_caddy_permissions.py`: 18, including seven opt-in disposable Docker cases.
- `test_https_ingress_installer.py`: 54.
- `test_host_installer.py`: 65.
- `test_standalone_installer_bootstrap.py`: 12.

Ruff format/lint and strict targeted mypy passed on those four files and
`scripts/install.py`. The final permission lane passed in 19.3 seconds.
Real Docker checks cover empty and image-initialized roots, effective
nonroot access, repeated preparation without metadata changes, rejection
of unexpected state, and an actual Caddy restart with unchanged root and
intermediate certificates/keys and saved configuration. Existing successor
preflight regressions retain #187's allowed-host environment correction.

Independent OpenCode review: **CLEAR**, read-only tools only. Native Windows
compatibility remains a separate acceptance requirement. Detailed logs and
review events are retained in
`/home/kyle/kodi-lab-audits/windows-caddy-permissions-20261102/`.

## Remaining native acceptance

On a separately authorized disposable Windows/WSL2 installation, verify the
owner UID and group/mode through both WSL and Docker Desktop, fresh HTTPS
startup, successor reuse, Caddy-only stop/up and restart, and unchanged CA
fingerprints, canonical files, receipts, network identities and database.
Confirm existing manually corrected volumes match the accepted private
ownership/modes; other states deliberately require explicit investigation.
Repeat the existing HTTPS/login/browser-trust acceptance there.

No full release gate, production deployment, Windows installation change,
successor bundle rebuild, managed Kodi startup, merge or pruning was performed.
