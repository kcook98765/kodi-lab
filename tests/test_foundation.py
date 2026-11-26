import json
from pathlib import Path

import pytest
from pydantic import ValidationError

import kodi_lab
from kodi_lab.config import Settings, safe_settings_error
from kodi_lab.storage.models import TargetRow


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


def test_migration_metadata_matches_fresh_schema() -> None:
    fixtures = Path(__file__).parent / "fixtures"
    foundation = json.loads((fixtures / "initial-schema.json").read_text())["tables"]
    replay = json.loads((fixtures / "worker-replay-schema.json").read_text())["tables"]
    assert set(TargetRow.metadata.tables) == (foundation.keys() | replay.keys()) - {
        "alembic_version"
    }
