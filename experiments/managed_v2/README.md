# Managed Kodi v2 Omega proof of concept

This fresh-state experiment runs a reviewed, unmodified Kodi 21.3 image with one named working profile volume and replaceable containers. It is an independent entry point on the managed-v2 development branch. The existing deployed Web, MCP, broker and external Kodi routes do not invoke it.

A private local controller owns a fixed Docker image, an isolated network, one Kodi container and a static feed. Its small MCP surface accepts an owner bearer or a read-only observer bearer on loopback. Native installation uses the current Narrow Bridge wire protocol and signature verification with a managed-only system add-on entry point. The baseline mounts the reviewed Bridge core, the closed resident operation and the managed repository as system assets. Per-run keys are outside the profile.

Use a trusted disposable Linux host with the reviewed Omega OCI image already present, the locked Kodi Lab development environment, UID 1000, a reachable private Docker bridge, and disk headroom on the Docker and workspace filesystems. Start with an empty, mode-0700 state directory. Keep this proof separate from existing installs and run one test environment at a time.

## Focused checks and native run

```sh
uv run --locked pytest -q tests/experiments/test_managed_v2.py
uv run --locked ruff check experiments/managed_v2 tests/experiments/test_managed_v2.py
install -d -m 700 /path/to/empty-trial
umask 077
uv run --locked python -m experiments.managed_v2.native_check \
  /path/to/empty-trial /path/to/prior-native-install-experiment/artifacts
```

The native check needs the synthetic Program, its three module ZIPs, the missing-dependency control and two synthetic decoy versions from the prior comparison. It uses the installed MCP SDK to make authenticated Streamable HTTP calls on an ephemeral loopback port. The skin lane uses a variant derived from the reviewed runtime's Estuary assets. Use the --skin-only option to run that lane by itself in a fresh state directory. Fixture and skin bytes stay outside Git. The test driver retains a named compressed profile snapshot, independent workspaces and scoped output. It removes owned containers, volumes, the network and transient credentials.

## Operation model

- Prepare creates the one clean working profile for Omega. Start creates a new container of the reviewed image with that volume. Graceful Stop verifies exit and retires the container. Reset stages a clean replacement volume.
- Snapshot calls list, save, load and delete names. Save and Load require confirmed stopped Kodi. Archive identity records the exact runtime and compressed SHA-256. Load verifies the bytes, tar members and Kodi database checks before switching the profile pointer. Kodi temp data and Bridge authority are omitted.
- Import a bounded ZIP or package an owned workspace. A trusted operator can pre-admit bytes captured from an approved repository. Installation selects an exact ID, version and digest and stages it in the private managed feed. Kodi resolves dependencies. Direct deployment of the same installed add-on operates on a stopped volume and preserves userdata.
- Calls are serialized through the controller. The Bridge accepts managed installation, add-on setting and identity operations, with fresh credentials for each boot. If the active skin differs from Estuary, the controller cleanly stops Kodi, writes the targeted skin setting and starts an authenticated replacement container on the same volume. After installation it repeats this cycle to restore the prior skin and its look-and-feel values. Native install refuses unexpected state. If the outcome is uncertain, the next mutation requires a confirmed Stop.

## Prototype limits

This lane is scoped to one trusted development writer and one observer on a disposable host. The broker is the local Docker-capable controller process. Its interface does not grant Docker to the agent. The control and feed network is isolated. Code running inside Kodi shares Kodi's process trust domain. Product isolation, external-target regression, catalog transport for live third-party feeds, broad add-on types, host power-loss recovery, the Web UI and WSL2 need separate qualification. There is no database migration, legacy MCP adapter or integration with a retained production profile.

The eventual replacement can retire selected managed development sessions, container-bound workspace/restore receipts, duplicate managed lifecycle coordinators and Bridge repair/preparation flows. Keep reviewed acquisition and runtime artifacts, the authenticated Bridge protocol, archive safety and dependency readback concepts, bounded viewer controls and independent external authorization. The umbrella issue tracks later removal and broader acceptance. [Managed Kodi replacement](https://github.com/kcook98765/kodi-lab/issues/405)
