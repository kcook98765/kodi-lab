"""Role consent composes clean replacement and separate reviewed Bridge receipts."""

from uuid import UUID, uuid5

from sqlalchemy import select

from kodi_lab.application.managed_bridge_preparation import (
    BridgePreparationError,
    ManagedBridgePreparationService,
)
from kodi_lab.application.managed_clean_reset import (
    ManagedCleanResetService,
    ManagedResetBridgeResult,
    ManagedResetInput,
    ManagedResetResult,
)
from kodi_lab.application.managed_versions import (
    AuthorizeVersion,
    ManagedVersionError,
    ManagedVersionLifecycleService,
)
from kodi_lab.storage.managed_lifecycle_owner import lifecycle_actor
from kodi_lab.storage.managed_start import ManagedStartBinding
from kodi_lab.storage.models import ManagedBridgePreparationRow, ManagedCleanResetRow


class ManagedAgentResetService:
    """No second Reset state machine; original parents own every native child."""

    def __init__(
        self,
        versions: ManagedVersionLifecycleService,
        bridge: ManagedBridgePreparationService | None = None,
    ) -> None:
        self.reset = ManagedCleanResetService(versions)
        self.bridge = bridge or ManagedBridgePreparationService(
            versions._sessions, versions._lifecycle, versions._resources
        )
        self.sessions = versions._sessions

    def invoke(
        self, client_id: UUID, operation: ManagedResetInput, authorize: AuthorizeVersion
    ) -> ManagedResetResult:
        if operation.expected_profile is None:
            raise ManagedVersionError("managed_reset_expected_profile_required")
        if operation.resume_bridge_version is not None:
            with self.sessions() as session:
                original = session.scalar(
                    select(ManagedCleanResetRow).where(
                        ManagedCleanResetRow.client_id == str(client_id),
                        ManagedCleanResetRow.request_id == str(operation.request_id),
                    )
                )
                if original is None or original.phase != "complete":
                    raise ManagedVersionError("managed_reset_original_request_required")
                authorize(session, UUID(original.target_id))
        result = self.reset.invoke(client_id, operation, authorize)
        with self.sessions() as session:
            parent = session.get(ManagedCleanResetRow, str(result.operation_id))
            if parent is None or parent.client_id != str(client_id):
                raise ManagedVersionError("managed_reset_not_authorized")
            authorize(session, UUID(parent.target_id))
            request_id = uuid5(UUID(parent.id), "trusted-managed-bridge:v1")
            saved = session.scalar(
                select(ManagedBridgePreparationRow).where(
                    ManagedBridgePreparationRow.request_id == str(request_id)
                )
            )
            saved_id = UUID(saved.id) if saved is not None else None
            expected = ManagedStartBinding(
                parent.target_id,
                parent.runtime_revision_id,
                parent.image_id,
                parent.new_container_id or "",
                "running" if parent.restart else "stopped",
            )
        if result.phase != "complete" or not result.replacement_current:
            return result.model_copy(
                update={
                    "bridge_preparation": ManagedResetBridgeResult(
                        operation_id=saved_id,
                        checkpoint="not_started"
                        if result.phase != "complete"
                        else "identity_changed",
                        reason="clean_replacement_not_current",
                    )
                }
            )
        try:
            row = self.bridge.accept(
                request_id=request_id,
                target_id=result.status.target_id or UUID(expected.target_id),
                keep_running=operation.restart,
                expected=expected,
                authorize=authorize,
            )
            if (
                operation.resume_bridge_version is not None
                and row.checkpoint != "complete"
                and operation.resume_bridge_version >= row.version
            ):
                row = self.bridge.resume(
                    UUID(row.id),
                    version=operation.resume_bridge_version,
                    authorize=authorize,
                )
            for _ in range(3):
                if row.checkpoint != "pending":
                    break
                row = self.bridge.continue_once(
                    UUID(row.id),
                    version=row.version,
                    authorize=authorize,
                    actor=lifecycle_actor(str(client_id)),
                )
            summary = ManagedResetBridgeResult(
                operation_id=UUID(row.id),
                checkpoint=row.checkpoint,
                version=row.version,
                reason=row.reason,
                stored_trust=self.bridge.stored_trust(row),
            )
        except BridgePreparationError as error:
            summary = ManagedResetBridgeResult(
                operation_id=saved_id,
                checkpoint="blocked",
                reason=str(error),
            )
        with self.sessions() as session:
            authorize(session, UUID(expected.target_id))
        # Re-observe the original replacement after maintenance; keep native
        # replacement and Bridge outcomes independently visible.
        current = self.reset._result(
            str(result.operation_id), authorize, result.replayed
        )
        recovery = current.recovery
        if current.phase == "complete" and current.replacement_current:
            recovery = (
                "Reviewed Bridge preparation is complete. Start this managed version "
                "when stopped, then deploy your owned workspace generation to this "
                "target. No Web project selection is required. Inspect live "
                "readiness separately."
                if summary.checkpoint == "complete" and summary.stored_trust == "active"
                else "Clean replacement is confirmed; reviewed Bridge preparation "
                "is incomplete. Inspect the saved Reset and Bridge evidence. "
                "Continue only the original Reset request; supply its saved Bridge "
                "version when explicit Bridge recovery is required. "
                "No Web project selection is required."
            )
        return current.model_copy(
            update={"bridge_preparation": summary, "recovery": recovery}
        )
