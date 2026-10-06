# Foreground remote-Kodi host from a reviewed wheel

This is the portable artifact foundation for a Kodi Lab host that connects to
an existing Kodi runtime over the LAN. The isolated installation procedure
below targets Ubuntu with Python 3.12. Windows and macOS host execution remain
**feasible / untested**. Managed Kodi, containers, the broker service, and
development host deployment are separate Linux functionality. A remote Kodi host does not
need a local Kodi installation or a Docker daemon.

The separate [remote-mode container foundation](REMOTE-MODE-CONTAINER.md) uses
this wheel and state contract for a Docker-hosted development deployment.

## Install and identify the artifact

Obtain the exact reviewed wheel and its SHA-256 digest through an approved
local handoff. Keep the wheel outside the data root. From any working directory:

```sh
export KODI_LAB_VENV="$HOME/.local/share/kodi-lab-venv"
python3.12 -m venv "$KODI_LAB_VENV"
"$KODI_LAB_VENV/bin/python" -m pip install /path/to/kodi_lab-1.0.0rc1-py3-none-any.whl
"$KODI_LAB_VENV/bin/python" -m pip check
"$KODI_LAB_VENV/bin/kodi-lab-admin" version
```

The current package version is `1.0.0rc1`, a local unpublished prerelease.
Earlier artifact checkpoints used `0.0.0` as an unreleased-development sentinel.
The `package-sha256` in `version` identifies installed runtime bytes. Record it
along with the wheel SHA-256 before and after any replacement. No source checkout,
repository `alembic.ini`, systemd, or nginx is required.

## Initialize private state

Choose a private, absolute, normalized directory for this host. These commands
are examples; use the same environment on each invocation and after restart.

```sh
umask 077
export KODI_LAB_DATA_ROOT="$HOME/.local/share/kodi-lab"
export KODI_LAB_RUNTIME_ROOT="$KODI_LAB_DATA_ROOT/run"
"$KODI_LAB_VENV/bin/kodi-lab-admin" migrate
"$KODI_LAB_VENV/bin/kodi-lab-admin" generate-master-key
export KODI_LAB_EXTERNAL_TARGET_MASTER_KEY="$(cat "$KODI_LAB_DATA_ROOT/master-key")"
"$KODI_LAB_VENV/bin/kodi-lab-admin" set-password
```

`migrate` creates the private data, database, and encrypted-secret directories,
then advances the new or existing SQLite database to the packaged Alembic head.
It is safe to repeat at head. It refuses an existing unversioned database. It
does not replace or delete an existing database. Migration failure requires
operator review and backup before a retry; Web startup does not migrate.

`generate-master-key` uses 32 random bytes and writes one base64url key to
`$KODI_LAB_DATA_ROOT/master-key` with private file permissions. It does not print
the key, replace an existing key file, or run when one is already configured.
Keep this exact key and the data root across restarts and upgrades. Losing the
key makes stored external target and bridge credentials unreadable. Do not put
the key in a command argument or repository file. The current secret store uses
POSIX private-directory and file-mode checks; a security-equivalent Windows
storage port remains a separate task.

`set-password` prompts twice without echo. It requires a migrated database.
For this installed flow, the admin and Web launchers take configuration from
explicit environment variables; they ignore a `.env` in the current directory.
The default listener is loopback only. The runtime root is set explicitly for
future managed-feature calls; no broker or runtime socket is needed to sign in
or pair a remote Kodi target.

## Run, restart, and retain state

```sh
"$KODI_LAB_VENV/bin/kodi-lab-web"
```

Open `http://127.0.0.1:8100/login`, sign in with the administrator password,
then open `/pair-kodi`. The browser and Kodi Lab host may be on the same Ubuntu
machine; the Kodi target is a separate LAN device. Pairing uses the four-character
OTP shown by its Kodi Lab bridge. Stop the foreground process with Ctrl-C. To
restart, restore the same three environment variables and run `kodi-lab-web`
again. The database, master key, and encrypted credentials must remain in the
data root.

For an upgrade, back up the private data root, install the next reviewed wheel
into the same venv, run `kodi-lab-admin migrate`, and compare the new wheel hash
and installed `package-sha256`. When deliberately replacing reviewed wheel bytes
that retain the same metadata version, use
`"$KODI_LAB_VENV/bin/python" -m pip install --force-reinstall --no-deps /path/to/new.whl` to replace
same-version package bytes deliberately. Verify login and retained target state
after restart. Removing only the venv removes code while retaining state.
Removing the data root intentionally resets the database, master key, and
encrypted credentials; do this only for disposable installations or with an
approved recovery plan.
