from pathlib import Path

import pytest
from pydantic import ValidationError

import kodi_lab
from kodi_lab.config import Settings, safe_settings_error
from kodi_lab.storage import metadata


def _isolate_settings_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KODI_LAB_DATA_ROOT", raising=False)
    monkeypatch.delenv("KODI_LAB_RUNTIME_ROOT", raising=False)
    monkeypatch.delenv("KODI_LAB_DEVELOPMENT_ROOT", raising=False)
    monkeypatch.delenv("KODI_LAB_WEB_HOST", raising=False)
    monkeypatch.delenv("KODI_LAB_WEB_BIND_HOST", raising=False)
    monkeypatch.delenv("KODI_LAB_WEB_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("KODI_LAB_NATIVE_ZIP_BASE_URL", raising=False)
    monkeypatch.delenv("KODI_LAB_WEB_PORT", raising=False)
    monkeypatch.delenv("KODI_LAB_WEB_COOKIE_SECURE", raising=False)
    monkeypatch.delenv("KODI_LAB_TRUSTED_CUSTOM_BUILDS_ENABLED", raising=False)
    monkeypatch.delenv("KODI_LAB_EXTERNAL_TARGET_ALLOWED_PORTS", raising=False)
    monkeypatch.delenv("KODI_LAB_EXTERNAL_BRIDGE_ALLOWED_PORTS", raising=False)


def test_package_exposes_version() -> None:
    assert kodi_lab.__version__ == "1.0.0rc1"


def test_configuration_uses_documented_roots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_settings_environment(monkeypatch, tmp_path)
    settings = Settings()

    assert settings.data_root == Path("/var/lib/kodi-lab")
    assert settings.runtime_root == Path("/run/kodi-lab")
    assert settings.development_root is None
    assert settings.database_url == "sqlite:////var/lib/kodi-lab/database/kodi-lab.db"
    assert settings.web_host == "127.0.0.1"
    assert settings.effective_web_bind_host == "127.0.0.1"
    assert settings.web_allowed_hosts == ()
    assert settings.native_zip_base_url is None
    assert settings.web_port == 8100
    assert settings.web_cookie_secure is False
    assert settings.trusted_custom_builds_enabled is False
    assert settings.external_target_allowed_ports == (8080,)
    assert settings.external_bridge_allowed_ports == (8765,)


def test_external_bridge_ports_are_separate_normalized_server_configuration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_settings_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("KODI_LAB_EXTERNAL_BRIDGE_ALLOWED_PORTS", "[8766,8765,8766]")
    settings = Settings()
    assert settings.external_bridge_allowed_ports == (8765, 8766)
    assert settings.external_target_allowed_ports == (8080,)

    monkeypatch.setenv("KODI_LAB_EXTERNAL_BRIDGE_ALLOWED_PORTS", "[]")
    with pytest.raises(ValidationError):
        Settings()


def test_trusted_custom_builds_require_explicit_deployment_opt_in(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_settings_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("KODI_LAB_TRUSTED_CUSTOM_BUILDS_ENABLED", "true")
    assert Settings().trusted_custom_builds_enabled is True


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("KODI_LAB_WEB_HOST", "0.0.0.0"),
        ("KODI_LAB_WEB_HOST", "::"),
        ("KODI_LAB_WEB_PORT", "8000"),
        ("KODI_LAB_WEB_COOKIE_SECURE", "true"),
    ],
)
def test_phase4_web_profile_rejects_unapproved_exposure_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str, value: str
) -> None:
    _isolate_settings_environment(monkeypatch, tmp_path)
    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError):
        Settings()


def test_phase4_validation_port_is_explicitly_allowed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_settings_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("KODI_LAB_WEB_PORT", "8180")
    assert Settings().web_port == 8180


def test_remote_container_binding_requires_explicit_concrete_trusted_hosts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_settings_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("KODI_LAB_WEB_BIND_HOST", "0.0.0.0")
    with pytest.raises(ValidationError):
        Settings()
    monkeypatch.setenv("KODI_LAB_WEB_ALLOWED_HOSTS", '["lab.example","127.0.0.1"]')
    settings = Settings()
    assert settings.effective_web_bind_host == "0.0.0.0"
    assert settings.web_allowed_hosts == ("lab.example", "127.0.0.1")
    for rejected in (
        '["*"]',
        '["0.0.0.0"]',
        '["https://lab.example"]',
        '["lab.example:8100"]',
        '["lab.example","lab.example"]',
    ):
        monkeypatch.setenv("KODI_LAB_WEB_ALLOWED_HOSTS", rejected)
        with pytest.raises(ValidationError):
            Settings()


def test_configuration_errors_name_fields_without_echoing_input(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_settings_environment(monkeypatch, tmp_path)
    marker = "private-input-must-not-appear"
    monkeypatch.setenv("KODI_LAB_WEB_ALLOWED_HOSTS", f'["{marker.upper()}"]')
    with pytest.raises(ValidationError) as caught:
        Settings()
    message = safe_settings_error(caught.value)
    assert message == "invalid Kodi Lab configuration: KODI_LAB_WEB_ALLOWED_HOSTS"
    assert marker not in message
    assert "input_value" not in message

    monkeypatch.setenv("KODI_LAB_WEB_ALLOWED_HOSTS", "[]")
    monkeypatch.setenv("KODI_LAB_WEB_BIND_HOST", "0.0.0.0")
    with pytest.raises(ValidationError) as caught:
        Settings()
    assert safe_settings_error(caught.value) == (
        "wildcard Web bind requires KODI_LAB_WEB_ALLOWED_HOSTS"
    )


def test_data_root_environment_override_is_independent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_settings_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("KODI_LAB_DATA_ROOT", "/tmp/kodi-lab-data")

    settings = Settings()

    assert settings.data_root == Path("/tmp/kodi-lab-data")
    assert settings.runtime_root == Path("/run/kodi-lab")
    assert settings.database_url == "sqlite:////tmp/kodi-lab-data/database/kodi-lab.db"


def test_runtime_root_environment_override_is_independent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_settings_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("KODI_LAB_RUNTIME_ROOT", "/tmp/kodi-lab-run")

    settings = Settings()

    assert settings.data_root == Path("/var/lib/kodi-lab")
    assert settings.runtime_root == Path("/tmp/kodi-lab-run")
    assert settings.database_url == "sqlite:////var/lib/kodi-lab/database/kodi-lab.db"


def test_development_root_requires_explicit_safe_configuration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_settings_environment(monkeypatch, tmp_path)
    configured = tmp_path / "development"
    monkeypatch.setenv("KODI_LAB_DEVELOPMENT_ROOT", str(configured))

    assert Settings().development_root == configured


@pytest.mark.parametrize("value", ("relative/path", "/", "/var/tmp/../escape"))
def test_development_root_rejects_unsafe_configuration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, value: str
) -> None:
    _isolate_settings_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("KODI_LAB_DEVELOPMENT_ROOT", value)
    with pytest.raises(ValidationError):
        Settings()


def test_migration_metadata_contains_core_storage_tables() -> None:
    assert set(metadata.tables) == {
        "workspace_import_operations",
        "mcp_remote_observations",
        "external_reconnect_operations",
        "mcp_workspace_launches",
        "managed_resource_policy",
        "managed_idle_states",
        "managed_instance_uses",
        "managed_start_reservations",
        "managed_provision_reservations",
        "development_sessions",
        "development_session_policy",
        "managed_diagnostic_captures",
        "managed_agent_lifecycle_requests",
        "external_screenshot_preferences",
        "managed_bridge_trust",
        "household_onboarding_operations",
        "external_secret_retirements",
        "external_development_capability_amendments",
        "external_development_repository_readiness",
        "repository_workspace_deployments",
        "repository_workspace_validation_snapshots",
        "development_publications",
        "development_publication_holds",
        "development_repository_state",
        "addon_repository_candidates",
        "addon_repository_attachment_operations",
        "addon_repository_fetch_operations",
        "addon_repository_metadata_snapshots",
        "addon_repository_refresh_operations",
        "addon_repository_sources",
        "addon_zip_artifacts",
        "addon_zip_enablement_operations",
        "addon_zip_import_operations",
        "addon_zip_installation_evidence",
        "addon_zip_installation_operations",
        "addon_zip_smoke_test_operations",
        "native_zip_installations",
        "agent_workspace_creation_operations",
        "agent_workspace_capture_operations",
        "agent_workspace_cleanup_operations",
        "agent_workspace_lifecycle_events",
        "agent_workspace_mutation_events",
        "agent_workspace_promotion_handoffs",
        "agent_workspace_promotion_journal",
        "agent_workspace_promotion_operations",
        "agent_workspace_promotion_plan_paths",
        "agent_workspace_promotion_plans",
        "agent_workspaces",
        "promotion_verification_log_evidence",
        "promotion_verification_operations",
        "production_mutation_control",
        "development_source_attachment_operations",
        "development_source_deployment_operations",
        "development_source_deployments",
        "development_source_promotion_leases",
        "development_source_enablement_operations",
        "development_source_smoke_test_operations",
        "development_sources",
        "fixture_definitions",
        "external_kodi_target_events",
        "external_kodi_target_observations",
        "external_kodi_targets",
        "external_bridge_events",
        "external_bridge_deployment_receipts",
        "external_bridge_pairings",
        "external_bridge_request_receipts",
        "reviewed_bridge_builds",
        "external_development_source_baselines",
        "external_production_policies",
        "external_production_policy_revision_guards",
        "production_capability_amendments",
        "production_bridge_http_acknowledgements",
        "production_native_control_http_acknowledgements",
        "global_heavy_operation_lease",
        "managed_instances",
        "managed_screenshots",
        "managed_instance_delete_operations",
        "managed_instance_addon_repository_sources",
        "managed_instance_development_sources",
        "managed_instance_lifecycle_operations",
        "managed_instance_mutation_leases",
        "managed_runtime_reconstruction_operations",
        "mcp_clients",
        "mcp_client_target_grants",
        "mcp_client_target_grant_identities",
        "mcp_client_development_source_grants",
        "mcp_agent_workspace_bindings",
        "mcp_credentials",
        "mcp_development_requests",
        "mcp_workspace_validations",
        "mcp_workspace_deployment_operations",
        "mcp_workspace_execution_operations",
        "workspace_execution_recovery_operations",
        "historical_workspace_execution_containment_resolutions",
        "mcp_presets",
        "mcp_production_screenshot_receipts",
        "mcp_production_navigation_audits",
        "mcp_production_remote_receipt_bindings",
        "mcp_production_target_scopes",
        "production_operation_approvals",
        "production_approval_transition_events",
        "production_transient_dispatch_audits",
        "production_approval_creation_rates",
        "production_transient_dispatch_rates",
        "mcp_preset_permissions",
        "mcp_remote_action_receipts",
        "mcp_security_events",
        "mcp_target_preset_assignments",
        "reset_operations",
        "repository_replacement_operations",
        "repository_replacement_plans",
        "runtime_container_artifacts",
        "runtime_definitions",
        "runtime_execution_profiles",
        "runtime_revisions",
        "runtime_recipe_installations",
        "runtime_validation_evidence",
        "snapshot_create_operations",
        "snapshot_delete_operations",
        "snapshot_restore_operations",
        "snapshots",
        "targets",
        "web_admin_credentials",
        "web_sessions",
    }
