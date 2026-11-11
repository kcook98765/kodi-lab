# Trusted Managed Lab

In Agents, an administrator may deliberately enable **Trusted Managed Lab — Full
Development** for a new client or grant it to an existing client. This is the
version 1 managed role. Existing clients and their presets are unchanged.

The grant covers all installed, approved disposable Managed Kodi instances,
including versions prepared later. It includes inspection, screenshots, logs,
supported semantic remote actions (including Select), Start/Stop, autonomous
Reset, add-on settings and existing owned-workspace development operations.
Technical prerequisites, resource limits, exact runtime/container identity,
operation leases and durable replay protections still apply. The role does not
install missing Kodi versions or override active-use guards.

The role supplies no external Kodi permissions. External Production and External
Development continue to require the client's original preset, each target's
policy and exact grants, plus the existing external consent and identity checks.
External operations never use the managed permission set. Granting or revoking
the managed role preserves separately approved external access.

The role grants no host filesystem, shell, Docker or service administration.
Workspace ownership/source authority still applies, including isolation between
agents. Agent-owned intake, selection-independent development and Reset/Bridge
composition are separate slices under #305, #306 and #308; the role alone does
not establish that these workflows have passed native acceptance.

The client API reports `managed_lab_role_version` (configured role) and
`managed_lab_permissions` (empty when disabled). Revoke the role in Agents to
remove managed convenience authority while retaining projects, base permissions
and credentials. Credential revocation, expiry and client disablement continue
to apply at admission and mutation rechecks. Grants and revocations have durable
audit events and advance the client's authority revision.
