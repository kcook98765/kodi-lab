# Kodi Lab

Kodi Lab hosts isolated Kodi runtimes for add-on development and testing. Its Web interface manages environments and viewing. Its authenticated MCP server lets coding agents install, edit, run and inspect add-ons. Kodi Lab uses your chosen agent and model. Use reviewed add-ons and controlled media. Add-on compatibility varies by Kodi version.

## Install on a fresh site

Use Ubuntu 24.04 amd64 with Docker Engine, Compose V2 and Python 3.12, or Windows 11 with Ubuntu 24.04 WSL2 and Docker Desktop's Linux engine. Use the documented prerequisites and a reviewed source revision. The installer builds and verifies the packaged Web, restricted broker and repository services, then initializes a fresh database.

Use empty application and managed volumes for a fresh installation. Existing compatible managed-v2 sites can use the [verified in-place update](docs/MANAGED-V2-FRESH-INSTALL.md#update-a-managed-v2-site). Migration from the retired pre-alpha architecture is unsupported.

- [Ubuntu installation](docs/HOST-INSTALLER.md)
- [Windows WSL2 installation](docs/WINDOWS-WSL2-INSTALL.md)
- [Fresh managed operation and recovery](docs/MANAGED-V2-FRESH-INSTALL.md)

The default login is `http://127.0.0.1:18100/login`. Local agents use `http://127.0.0.1:18100/mcp` with a separate bearer credential. Read the [networking guide](docs/WEB-NETWORKING.md) before configuring LAN Web access or HTTPS. The add-on repository has a separate listener.

## Prepare Kodi and develop

1. In **Kodi Runtimes**, acquire a reviewed Kodi version. The [runtime matrix and limits](docs/MANAGED-V2-FRESH-INSTALL.md#reviewed-runtimes) cover legacy Matrix 19.5 on Linux amd64, Nexus 20.5, Omega 21.3 and preview Piers 22.0 RC2. Retained Windows native qualification covers Omega 21.3.
2. In **Managed Kodi**, choose the version, select **Prepare**, then **Start**. The broker prepares the independent working profile and authenticated Narrow Bridge. Use **View Screen** for view-only noVNC and **Take Control** for human input.
3. In **Agents**, create a credential with **Trusted Managed Lab** for a coding agent. An agent acquires the shared owner, then chooses a prepared generation. Each version retains one working profile. Only one managed Kodi can run at a time.
4. Use the [Toolkit and OpenCode setup](https://github.com/kcook98765/kodi-agent-toolkit) to validate and admit a ZIP with one bounded file transfer. Provision the [reviewed dependency catalog](docs/MANAGED-V2-FRESH-INSTALL.md#approve-dependencies-for-native-installs), then request native installation with the exact approved digest. Kodi resolves dependencies. Execute the add-on and verify fresh logs or observed behavior.
5. For ordinary edits, deploy directly from the private workspace. Stop Kodi before direct deployment, then Start and test. Save a named snapshot for a stopped environment you want to reuse. Stop and release the owner when done.

Ordinary development uses an internal network and private feed. The optional [approved public repository test](docs/MANAGED-PUBLIC-REPOSITORY.md) uses a separate disposable Linux site.

[Managed MCP contracts and package admission](docs/MCP-TOOLS-AND-PERMISSIONS.md) · [Security boundaries](SECURITY.md)

For private silent videos of managed Kodi, see [Managed recording](docs/MANAGED-RECORDING.md).

## External Kodi and recovery

Register external Kodi separately and grant its inspection, navigation or development permissions per target. Trusted Managed Lab authority applies to managed Kodi only. [External registration and Bridge pairing](docs/LINUX-V1-INSTALL.md#install-and-pair-a-remote-kodi-bridge)

Application backups and managed state are separate. Named snapshots capture stopped Kodi profiles. Private workspaces and approved packages live on the broker volume. Follow the [backup and recovery guide](docs/MANAGED-V2-FRESH-INSTALL.md#state-and-recovery).
