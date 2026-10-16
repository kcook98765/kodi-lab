"""An external deployment and its reconciliation stay tied to one target."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from kodi_lab.application.mcp_workspace_development import McpWorkspaceDevelopmentError
from kodi_lab.storage.models import (
    ExternalBridgeDeploymentReceiptRow,
    ExternalBridgePairingRow,
    ExternalDevelopmentSourceBaselineRow,
    ExternalKodiTargetRow,
    McpWorkspaceDeploymentOperationRow,
    TargetRow,
)
from tests.application.test_mcp_workspace_development import (
    EXTERNAL_TARGET_ID,
    SOURCE_ID,
    _configure_external_environment,
    _environment,
)


def test_other_target_success_cannot_establish_this_targets_baseline(
    tmp_path: Path,
) -> None:
    env = _environment(tmp_path)
    bridge, _targets = _configure_external_environment(env)
    result = env.development.deploy(
        client_id=env.client_id,
        request_id=uuid4(),
        workspace_id=env.workspace_id,
        expected_generation=0,
        target_id=EXTERNAL_TARGET_ID,
    )
    other_id = uuid4()
    with env.sessions.begin() as session:
        original = session.get(TargetRow, str(EXTERNAL_TARGET_ID))
        assert original is not None
        for model, key in (
            (TargetRow, "id"),
            (ExternalKodiTargetRow, "target_id"),
            (ExternalBridgePairingRow, "target_id"),
        ):
            row = session.get(model, str(EXTERNAL_TARGET_ID))
            assert row is not None
            data = {
                column.name: getattr(row, column.name)
                for column in model.__table__.columns
            }
            data[key] = str(other_id)
            if model is TargetRow:
                data["name"] = "Different paired Windows development target"
            if model is ExternalBridgePairingRow:
                data["pinned_bridge_id"] = str(uuid4())
            session.add(model(**data))
            session.flush()
        with pytest.raises(
            McpWorkspaceDevelopmentError, match="target_provenance_unknown"
        ):
            env.development._deployment_preflight_rows(
                session, env.workspace_id, 0, other_id
            )
        baseline = session.get(
            ExternalDevelopmentSourceBaselineRow,
            {
                "target_id": str(EXTERNAL_TARGET_ID),
                "development_source_id": str(SOURCE_ID),
            },
        )
        assert baseline is not None and baseline.state == "current"
        receipt = session.get(
            ExternalBridgeDeploymentReceiptRow, str(result.deployment_id)
        )
        assert receipt is not None and receipt.target_id == str(EXTERNAL_TARGET_ID)
    assert bridge.replacement_count == 1


@pytest.mark.parametrize(
    "field", ["addon_id", "addon_version", "generation", "bridge_id", "request_id"]
)
def test_reconciliation_rejects_mismatched_receipt_identity(
    tmp_path: Path, field: str
) -> None:
    env = _environment(tmp_path)
    bridge, _targets = _configure_external_environment(env)
    result = env.development.deploy(
        client_id=env.client_id,
        request_id=uuid4(),
        workspace_id=env.workspace_id,
        expected_generation=0,
        target_id=EXTERNAL_TARGET_ID,
    )
    assert bridge.receipt is not None
    values: dict[str, Any] = {
        "addon_id": "plugin.video.other",
        "addon_version": "9.9.9",
        "generation": 99,
        "bridge_id": uuid4(),
        "request_id": uuid4(),
    }
    bridge.receipt = replace(bridge.receipt, **{field: values[field]})
    with env.sessions.begin() as session:
        operation = session.get(
            McpWorkspaceDeploymentOperationRow, str(result.deployment_id)
        )
        receipt = session.get(
            ExternalBridgeDeploymentReceiptRow, str(result.deployment_id)
        )
        assert operation is not None and receipt is not None
        operation.state = "reconciliation_required"
        receipt.state = "ambiguous"
    with pytest.raises(McpWorkspaceDevelopmentError, match="deployment_ambiguous"):
        env.development.reconcile_external_deployment(result.deployment_id)
    assert bridge.replacement_count == 1
