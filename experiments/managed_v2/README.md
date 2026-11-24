# Managed Kodi v2 on Omega

This fresh-install lane runs unmodified Kodi 21.3 with one independent writable profile volume, replaceable containers, and a private feed. It is an experimental entry point for the replacement tracked in [#405](https://github.com/kcook98765/kodi-lab/issues/405).

## Process boundary

The Docker-capable broker owns the private state directory and runs the single lifecycle controller. The loopback Streamable HTTP MCP process sends closed requests over a Unix socket. The socket lives in a broker-owned directory and admits the configured frontend UID. The broker independently checks the bearer scope, operation name, and argument shape. Run the frontend as a distinct account with no Docker group membership and no private broker state mounts. Provision the socket group so the broker can set it and the frontend can connect. The separate broker process lock prevents accidental replacement of a live socket. One nonblocking mutation lock protects each operation.

Kodi uses a Docker-reserved singleton name, the pinned image identity, an internal network, and an ownership-labelled profile volume. Native JSON-RPC uses a fresh HTTP credential. The managed Bridge uses a separate per-boot key and the existing signed Bridge protocol. The private repository and reviewed Bridge are system assets. Profile capture excludes Bridge authority and the native HTTP password.

## Operations and recovery

- Start checks the volume and waits for the actual Kodi version and authenticated Bridge identity. Stop checks clean native exit and removes the container. The one working profile survives container removal. Reset and snapshot restore stage a new profile and switch the pointer only after validating the replacement.
- Save and Load require stopped Kodi. Save publishes an archive and digest metadata. Load checks the exact image, compressed digest, archive membership, profile settings, and database integrity. Named archives and agent source workspaces have independent lifetimes.
- Import a bounded ZIP, package the owned workspace, or have an operator admit pinned repository bytes. One install request selects the exact ID, version, origin and SHA-256 from the private feed. Kodi resolves dependencies and applies updates or same-version replacement. The broker checks enabled state, repository origin and installed bytes. Direct deployment operates on a stopped profile and preserves userdata.
- For a non-Estuary skin, the controller stops Kodi, records the saved skin settings, and atomically changes the selection. It restarts on Estuary, installs, and then restores and verifies the previous skin.
- `managed_status` shows observed runtime state and the last mutation record. If a response is lost, inspect that record before starting another mutation. A verified completed install retains its result until another mutation. When the record is pending, the owner can call `managed_recover` to reconcile the observed resources. Recovery can discard unpublished staging files or restore the saved skin. It reports an interrupted install as unconfirmed and never repeats it implicitly.

## Focused checks and native run

Use a disposable Linux host or isolated test lane with the exact reviewed Omega image already present, the locked development environment, UID 1000, private Docker bridge routing, and 8 GiB plus 128 MiB of disk headroom. The native harness uses noninteractive sudo to start the frontend as UID 65534 with no supplementary groups and to grant access to its temporary broker socket. It asserts that the frontend cannot access Docker or broker state. The operator programs raw native observations and crash injection outside MCP. Run one trial at a time.

```sh
uv run --locked pytest -q tests/experiments/test_managed_v2.py
uv run --locked ruff check experiments/managed_v2 tests/experiments/test_managed_v2.py

umask 077
install -d -m 700 /path/to/empty-integrated-trial
uv run --locked python -m experiments.managed_v2.native_check \
  /path/to/empty-integrated-trial /path/to/private-native-fixtures

install -d -m 700 /path/to/empty-interruption-trial
uv run --locked python -m experiments.managed_v2.native_check \
  /path/to/empty-interruption-trial /path/to/private-native-fixtures --interrupt-only
```

The fixture set contains the Program, two direct modules, one transitive module, a missing-dependency control and two decoy versions. The alternative skin is derived from the reviewed runtime's Estuary assets. The interruption lane accepts an optional operator-acquired Global Search 9.0.7 ZIP with the pinned digest used in qualification. Fixture bytes stay outside Git. Teardown retires the trial's owned Docker resources and live credentials. The harness retains its named snapshot, independent source workspace and private evidence.

## Stage 2 scope

The lane has one development writer and one observer. It qualifies broker process termination and observed recovery on Omega. Host power loss, per-agent application ownership, production Web integration, WSL2, other Kodi lines and the mixed-extension dependency types needed by Aura remain separate work. Code admitted into Kodi shares the Kodi process trust domain. The next stage should cut the existing managed entry points over to this controller and remove the old managed broker, lifecycle and selected-session paths as part of that cutover. Keep external Kodi authority and shared viewer, acquisition and diagnostic capabilities.
