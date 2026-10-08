from pathlib import Path

import pytest

from kodi_lab.application.mcp_administration import (
    McpAdministrationError,
    McpAdministrationService,
    McpProductionReadOnlyRequest,
)
from kodi_lab.application.production_inspection import PRODUCTION_VISUAL_PRESET_ID
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.models import ExternalProductionPolicyRow
from tests.application.test_production_bridges import _pair_production_bridge
from tests.kodi.fake_external_bridge import FakeExternalBridge
from tests.storage.test_migrations import (  # noqa: F401
    migration_setup,
    migration_templates,
)
from tests.storage.test_screenshot_preference_migration import _register_household


def test_grouped_read_only_authorization_on_migrated_head(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, url = migration_setup(monkeypatch, tmp_path, start_revision="head")
    engine = create_database_engine(url)
    sessions = create_session_factory(engine)
    target_id, _ = _register_household(sessions, tmp_path)
    try:
        with FakeExternalBridge(target_id=target_id, token=b"x" * 32) as fake:
            _pair_production_bridge(sessions, tmp_path, target_id, fake)
            admin = McpAdministrationService(sessions)
            client = admin.create_client(
                display_name="Migrated schema agent",
                preset_id=PRODUCTION_VISUAL_PRESET_ID,
            )
            admin.issue_credential(client.id)
            with sessions() as session:
                policy = session.get(ExternalProductionPolicyRow, str(target_id))
                assert policy is not None
                expected = McpProductionReadOnlyRequest(
                    expected_client_authority_revision=1,
                    expected_identity_generation=policy.identity_generation,
                    expected_target_policy_revision=policy.policy_revision,
                    expected_bridge_trust_revision=1,
                )
            result = admin.authorize_production_read_only(
                client.id, target_id, expected
            )
            assert result.visual_scope is not None
            assert result.inspection_scope.effective_state == "active"
            admin.revoke_production_inspection_scope(result.visual_scope.scope_id)
            expected = expected.model_copy(
                update={
                    "expected_target_policy_revision": result.target_policy_revision
                }
            )
            with pytest.raises(
                McpAdministrationError, match="read_only_scope_review_required"
            ):
                admin.authorize_production_read_only(client.id, target_id, expected)
            assert (
                len(
                    admin.list_production_inspection_scopes(
                        client_id=client.id, target_id=target_id
                    )
                )
                == 2
            )
    finally:
        engine.dispose()
