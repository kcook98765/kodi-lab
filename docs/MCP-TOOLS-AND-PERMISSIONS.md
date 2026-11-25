# MCP tools and permissions

Connect through the configured MCP endpoint with a current bearer token. Call
`tools/list` with that token to discover the operations the agent may use.
Managed operations use a shared managed environment. External target operations
use exact target UUIDs and independent per-target grants.

## Managed Kodi

An administrator enables **Trusted Managed Lab** for the agent. The agent uses
`managed_acquire` before mutating the environment and `managed_release` when
finished. A conflicting human or agent owner blocks mutation. Before saving,
loading or resetting a profile, stop Kodi through the managed lifecycle.

Check `managed_status` for the available generations. Runtime-scoped tools
accept `generation` (`matrix`, `nexus`, `omega`, or `piers`) and default
to `omega`. Stop the current runtime before starting another. The working
profiles are independent. Ownership and the private workspace namespace span
versions. Load snapshots only into their original reviewed image identity.

| Task | MCP tools |
| --- | --- |
| Lifecycle and authority | `managed_status`, `managed_prepare`, `managed_start`, `managed_stop`, `managed_reset`, `managed_acquire`, `managed_release` |
| Private profile snapshots | `managed_snapshot_list`, `managed_snapshot_save`, `managed_snapshot_load`, `managed_snapshot_delete` |
| Private workspaces | `managed_workspace_open`, `managed_workspace_tree`, `managed_workspace_read`, `managed_workspace_edit`, `managed_workspace_package`, `managed_workspace_deploy` |
| Native package workflow | `managed_package_import`, `managed_addon_install`, `managed_addon_inventory`, `managed_addon_launch`, `managed_addon_setting` |
| Inspection and control | `managed_logs`, `managed_gui`, `managed_screen`, `managed_navigate`, `managed_skin` |

Admit a ZIP with its digest or package a workspace, then submit one install
intent for the add-on ID, version and approved digest. Kodi performs native
installation and dependency resolution. Use the same operation for updates
and same-version replacements. Direct source deployment updates the installed
tree while Kodi is stopped. Follow it with Start and execute the add-on.
An add-on may still need first-run configuration after native installation.

### Managed package transfer

The Toolkit's `managed-package-import` command sends the exact ZIP as one
authenticated binary POST to the configured MCP URL. It returns the add-on ID,
version and digest for `managed_addon_install` or `managed_workspace_open`.
The server validates SHA-256, manifest, archive paths and expanded content.
The binary upload accepts up to 32 MiB, with a 64 MiB expanded-tree limit,
3,000 archive entries and a 30-second receive deadline. The 64 KiB MCP JSON
limit remains in place. Transfer needs Trusted Managed Lab and is private to
the credential's agent. Upload does not install or deploy.

Open a first source package with `managed_workspace_open`. For an intentional
bulk refresh of your existing workspace, upload the complete updated ZIP, then
supply its admitted digest and `replace="true"`. This replaces the private
workspace tree without changing installed Kodi files. Keep unpublished edits
you want to retain. For ordinary text changes retain the digest-guarded
`managed_workspace_edit` and direct deploy workflow.

### Approved disposable public repository testing

The four `managed_public_*` tools are available to trusted managed clients.
They use the existing managed owner and require a separate operator-approved
disposable Linux site with an unprepared Omega profile. An agent selects an
existing exact approval and uses native install and catalog operations. The
host operator owns approval and revocation. Ordinary package admission
refuses repository add-ons. See the [supported workflow and shared-hosting
limits](MANAGED-PUBLIC-REPOSITORY.md).

## External Kodi

| Task | MCP tools |
| --- | --- |
| Discover and inspect granted targets | `target_list`, `target_status`, `addon_list`, `system_info`, `inspect_ui_state` |
| Read diagnostics | `log_read`, `log_scan`, `screenshot_capture` |
| Authorized input | `remote_action` |
| External development | `workspace_validate`, `workspace_deploy_preflight`, `workspace_deploy`, `workspace_execute` |

External registration and Narrow Bridge pairing establish the target's
capabilities. A separate client preset, target preset and exact grant authorize
access. Household production visual and navigation authority are separately
enabled and scoped. External development requires a bound workspace and source
grant, matching validation and eligible external target. Repository mode uses
human approval and exact installed-tree verification. See the [external setup](LINUX-V1-INSTALL.md).

Use the [fresh-install guide](MANAGED-V2-FRESH-INSTALL.md) for configuration,
managed profile recovery and backup boundaries.
