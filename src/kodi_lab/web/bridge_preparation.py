"""Authenticated administrator maintenance; no MCP or external target endpoint."""

from collections.abc import Callable
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import JSONResponse

from kodi_lab.application.managed_bridge_preparation import (
    BridgePreparationError,
    ManagedBridgePreparationService,
)
from kodi_lab.auth.service import AuthenticatedWebSession
from kodi_lab.storage.models import ManagedBridgePreparationRow


class BridgePreparationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: UUID = Field(strict=False)
    keep_running: bool = False


class BridgePreparationContinuation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: int = Field(ge=0)


def bridge_preparation_router(
    service: ManagedBridgePreparationService,
    authenticated: Callable[..., AuthenticatedWebSession],
    mutation: Callable[..., AuthenticatedWebSession],
) -> APIRouter:
    router = APIRouter()
    read_admin, write_admin = Depends(authenticated), Depends(mutation)
    base = "/api/instances/{target_id}/bridge-preparation"

    def reply(row: ManagedBridgePreparationRow | None) -> JSONResponse:
        return JSONResponse(
            None
            if row is None
            else {
                "operation_id": row.id,
                "request_id": row.request_id,
                "target_id": row.target_id,
                "version": row.version,
                "phase": row.phase,
                "checkpoint": row.checkpoint,
                "reason": row.reason,
                "keep_running": row.keep_running,
                "attempt": row.attempt,
                "recovery_action": service.recovery_action(row),
                "stored_trust": service.stored_trust(row),
                "installation": "verified_during_setup"
                if row.phase in {"stop", "complete"}
                else "pending",
                "history_url": f"/instances/{row.target_id}/history",
            },
            headers={"Cache-Control": "no-store"},
        )

    def exact(target_id: UUID, operation_id: UUID) -> ManagedBridgePreparationRow:
        row = service.get(operation_id)
        if row is None or row.target_id != str(target_id):
            raise HTTPException(404, "managed_bridge_preparation_not_found")
        return row

    @router.get(base)
    def latest(
        target_id: UUID, _admin: AuthenticatedWebSession = read_admin
    ) -> JSONResponse:
        from sqlalchemy import select

        with service.sessions() as session:
            row = session.scalar(
                select(ManagedBridgePreparationRow)
                .where(ManagedBridgePreparationRow.target_id == str(target_id))
                .order_by(ManagedBridgePreparationRow.created_at.desc())
                .limit(1)
            )
            return reply(row)

    @router.post(base)
    def accept(
        target_id: UUID,
        body: BridgePreparationRequest,
        _admin: AuthenticatedWebSession = write_admin,
    ) -> JSONResponse:
        try:
            return reply(
                service.accept(
                    request_id=body.request_id,
                    target_id=target_id,
                    keep_running=body.keep_running,
                )
            )
        except Exception as error:
            raise HTTPException(
                409, "managed_bridge_preparation_requires_review"
            ) from error

    @router.get(base + "/{operation_id}")
    def status(
        target_id: UUID,
        operation_id: UUID,
        _admin: AuthenticatedWebSession = read_admin,
    ) -> JSONResponse:
        return reply(exact(target_id, operation_id))

    @router.post(base + "/{operation_id}/{action}")
    def continue_original(
        target_id: UUID,
        operation_id: UUID,
        action: str,
        body: BridgePreparationContinuation,
        _admin: AuthenticatedWebSession = write_admin,
    ) -> JSONResponse:
        exact(target_id, operation_id)
        if action not in {"continue", "resume"}:
            raise HTTPException(404)
        try:
            return reply(
                service.resume(operation_id, version=body.version)
                if action == "resume"
                else service.continue_once(operation_id, version=body.version)
            )
        except (BridgePreparationError, PermissionError) as error:
            raise HTTPException(
                409, "managed_bridge_original_preparation_requires_review"
            ) from error

    return router
