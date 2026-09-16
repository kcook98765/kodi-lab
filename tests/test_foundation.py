from pathlib import Path

import pytest
from pydantic import ValidationError

import kodi_lab
from kodi_lab.config import Settings
from kodi_lab.storage import metadata


def _isolate_settings_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KODI_LAB_DATA_ROOT", raising=False)
    monkeypatch.delenv("KODI_LAB_RUNTIME_ROOT", raising=False)
    monkeypatch.delenv("KODI_LAB_WEB_HOST", raising=False)
    monkeypatch.delenv("KODI_LAB_WEB_PORT", raising=False)
    monkeypatch.delenv("KODI_LAB_WEB_COOKIE_SECURE", raising=False)
    monkeypatch.delenv("KODI_LAB_TRUSTED_CUSTOM_BUILDS_ENABLED", raising=False)


def test_package_exposes_version() -> None:
    assert kodi_lab.__version__ == "0.0.0"


def test_configuration_uses_documented_roots(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _isolate_settings_environment(monkeypatch, tmp_path)
    settings = Settings()

    assert settings.data_root == Path("/var/lib/kodi-lab")
    assert settings.runtime_root == Path("/run/kodi-lab")
    assert settings.database_url == "sqlite:////var/lib/kodi-lab/database/kodi-lab.db"
    assert settings.web_host == "127.0.0.1"
    assert settings.web_port == 8100
    assert settings.web_cookie_secure is False
    assert settings.trusted_custom_builds_enabled is False


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


def test_migration_metadata_contains_core_storage_tables() -> None:
    assert set(metadata.tables) == {
        "fixture_definitions",
        "global_heavy_operation_lease",
        "managed_instances",
        "managed_instance_mutation_leases",
        "reset_operations",
        "runtime_container_artifacts",
        "runtime_definitions",
        "runtime_execution_profiles",
        "runtime_revisions",
        "runtime_validation_evidence",
        "snapshot_create_operations",
        "snapshot_delete_operations",
        "snapshot_restore_operations",
        "snapshots",
        "targets",
        "web_admin_credentials",
        "web_sessions",
    }
