"""A migrated repository requires fresh target evidence for its exact bootstrap."""

from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from kodi_lab.application.mcp_workspace_development import McpWorkspaceDevelopmentError
from kodi_lab.repository import development
from kodi_lab.repository.policy import DEVELOPMENT_REPOSITORY_ID
from kodi_lab.repository.transition import _development_records
from kodi_lab.repository.workspace_package import bridge_manifest_for_zip
from kodi_lab.storage.models import DevelopmentRepositoryStateRow
from tests.application.test_mcp_workspace_development import EXTERNAL_TARGET_ID
from tests.application.test_repository_workspace_deployment import _setup


class UpdatedRepositoryTarget:
    def __init__(self, previous: Any, content: bytes) -> None:
        self.previous = previous
        self.digest, self.files, self.size = bridge_manifest_for_zip(
            content, DEVELOPMENT_REPOSITORY_ID
        )

    def addon_inventory(self, target_id: UUID) -> Any:
        result = self.previous.addon_inventory(target_id)
        return result.model_copy(
            update={
                "items": [
                    item.model_copy(update={"version": "1.0.1"})
                    if item.addon_id == DEVELOPMENT_REPOSITORY_ID
                    else item
                    for item in result.items
                ]
            }
        )

    def inspect_addon(self, target_id: UUID, addon_id: str) -> Any:
        result = self.previous.inspect_addon(target_id, addon_id)
        if addon_id != DEVELOPMENT_REPOSITORY_ID:
            return result
        return replace(
            result,
            addon_version="1.0.1",
            manifest_digest=self.digest,
            file_count=self.files,
            total_bytes=self.size,
        )


def test_new_repository_version_is_not_confirmed_by_old_client_evidence(
    tmp_path: Path,
) -> None:
    env, repository, original_target = _setup(tmp_path)
    _, old = development.load_current(repository.root)
    following = development._generation(
        repository.root,
        repository.base_url,
        _development_records(repository.root, old),
        version="1.0.1",
    )
    development._pointer(repository.root / "CURRENT", following["tree_sha256"])
    development._pointer(repository.root / "ACK", following["tree_sha256"])
    with env.sessions.begin() as session:
        state = session.get(DevelopmentRepositoryStateRow, 1)
        assert state is not None
        state.bootstrap_sha256 = following["bootstrap_zip_sha256"]
        state.bootstrap_tree_sha256 = following["bootstrap_tree_sha256"]
        state.current_generation = following["tree_sha256"]
        state.current_index_sha256 = following["addons_xml_sha256"]
    arguments = dict(
        target_id=EXTERNAL_TARGET_ID,
        base_url=repository.base_url,
        reachable_in_kodi=True,
        manual_updates_confirmed=True,
    )
    with pytest.raises(McpWorkspaceDevelopmentError, match="prerequisite_unconfirmed"):
        env.development.confirm_development_repository(**arguments)
    updated = UpdatedRepositoryTarget(
        original_target, development.bootstrap_zip(repository.base_url, "1.0.1")
    )
    env.development.configure_external(bridges=updated, targets=updated)
    confirmed = env.development.confirm_development_repository(**arguments)
    assert confirmed.repository_version == "1.0.1"
    assert confirmed.bootstrap_sha256 == following["bootstrap_zip_sha256"]
    assert confirmed.observed_manifest_digest == updated.digest
