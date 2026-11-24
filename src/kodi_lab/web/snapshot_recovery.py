"""Operation-specific routes inside the existing authenticated Web application."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from kodi_lab.application.snapshot_recovery import SnapshotRecoveryService
from kodi_lab.application.snapshot_recovery_evidence import RecoveryRefused
from kodi_lab.auth.service import WebAuthenticationService


class EmptyReview(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RecoveryConsent(EmptyReview):
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_id: UUID
    confirmed: StrictBool


def recovery_router(
    service: SnapshotRecoveryService,
    epoch_id: str,
    current: Callable[..., Any],
    mutation: Callable[..., Any],
    auth: WebAuthenticationService,
    cookie: str,
) -> APIRouter:
    router = APIRouter()
    read_admin, write_admin = Depends(current), Depends(mutation)

    def actor(request: Request) -> str:
        token = request.cookies.get(cookie)
        if token is None:
            raise RecoveryRefused("recovery_administrator_required")
        return auth.token_digest(token)

    @router.get("/snapshot-recovery", response_class=HTMLResponse)
    def page(request: Request, authenticated: Any = read_admin) -> HTMLResponse:
        # Only fixed markup and existing opaque CSRF value; profile names are
        # never caller-provided HTML or hardcoded as verified success.
        from html import escape

        return HTMLResponse(
            "<!doctype html><html><head><title>Original snapshot recovery</title>"
            f'<meta name="kodi-lab-csrf" content="{escape(authenticated.csrf_token)}">'
            "</head><body><h1>Original snapshot recovery</h1>"
            "<p>Review the immutable failed restore and exact original snapshot. "
            "This creates one stopped successor. Interrupted dispatch remains fenced. "
            "Historical failure and archives are preserved; current source and Bridge "
            "confidence require ordinary revalidation.</p>"
            '<button id="recovery-preview">Review exact recovery</button>'
            '<p><a id="recovery-receipt" hidden>Authenticated durable receipt</a></p>'
            '<button id="recovery-observe" hidden>Check saved recovery status</button>'
            '<pre id="recovery-proof"></pre><button id="recovery-confirm" hidden>'
            "Confirm this exact one-use stopped recovery</button>"
            '<script src="/static/snapshot-recovery.js" defer></script></body></html>'
        )

    @router.post("/api/snapshot-recovery/reviews")
    def preview(
        request: Request, _body: EmptyReview, _authenticated: Any = write_admin
    ) -> dict[str, Any]:
        return service.preview(epoch_id, actor(request))

    @router.post("/api/snapshot-recovery/{recovery_id}/confirm", status_code=202)
    def confirm(
        recovery_id: UUID,
        request: Request,
        body: RecoveryConsent,
        tasks: BackgroundTasks,
        _authenticated: Any = write_admin,
    ) -> dict[str, Any]:
        if not body.confirmed:
            raise RecoveryRefused("recovery_confirmation_required")
        result, dispatch = service.confirm(
            str(recovery_id), actor(request), body.fingerprint, str(body.request_id)
        )
        if dispatch:
            tasks.add_task(service.execute, str(recovery_id))
        return result

    @router.post("/api/snapshot-recovery/{recovery_id}/revoke")
    def revoke(
        recovery_id: UUID, request: Request, _authenticated: Any = write_admin
    ) -> dict[str, str]:
        service.revoke(str(recovery_id), actor(request))
        return {"state": "revoked"}

    @router.get("/api/snapshot-recovery/{recovery_id}")
    def status(recovery_id: UUID, _authenticated: Any = read_admin) -> dict[str, Any]:
        return service.status(str(recovery_id))

    return router
