"""Versioned managed-only role; external authority never uses these permissions."""

TRUSTED_MANAGED_LAB_ROLE_VERSION = 1

# Frozen v1 role: extending legacy presets must not silently extend this grant.
TRUSTED_MANAGED_LAB_PERMISSIONS = frozenset(
    {
        "addon.install",
        "development.execute",
        "development.launch",
        "development.request_promotion",
        "development.settings.write",
        "development_source.inspect",
        "managed.lifecycle",
        "managed.reset",
        "managed.versions",
        "target.control",
        "target.inspect",
        "target.visual",
        "workspace.capture",
        "workspace.deploy",
        "workspace.edit",
        "workspace.inspect",
    }
)
