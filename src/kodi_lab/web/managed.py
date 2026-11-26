"""Administrator routes to the same managed broker used by MCP."""

from __future__ import annotations

import base64
import binascii
from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.templating import Jinja2Templates

from kodi_lab.auth.service import AuthenticatedWebSession
from kodi_lab.managed import RELEASES
from kodi_lab.managed.broker import BrokerClient
from kodi_lab.managed.protocol import VERSION_SCOPED, Refusal

from .managed_view import stream_view

WEB_OPERATIONS = frozenset(
    {
        "managed_prepare",
        "managed_start",
        "managed_network_access",
        "managed_stop",
        "managed_reset",
        "managed_take_control",
        "managed_release",
        "managed_recover",
        "managed_navigate",
        "snapshot_save",
        "snapshot_load",
        "snapshot_delete",
    }
)


class ManagedAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: str = Field(max_length=32)
    arguments: dict[str, str] = Field(default_factory=dict, max_length=2)


def managed_router(
    broker: BrokerClient,
    templates: Jinja2Templates,
    current_session: Callable[..., AuthenticatedWebSession],
    mutation_session: Callable[..., AuthenticatedWebSession],
    *,
    resolve_session: Callable[[str | None], AuthenticatedWebSession | None],
    recheck_passive_session: Callable[[str | None], AuthenticatedWebSession | None],
    cookie_name: str,
    same_origin: Callable[[WebSocket], bool],
    require_https: bool,
) -> APIRouter:
    router = APIRouter()
    session = Depends(current_session)
    mutation = Depends(mutation_session)

    @router.get("/managed-kodi", response_class=HTMLResponse, name="managed_kodi_page")
    def page(
        request: Request,
        generation: str = "omega",
        authenticated: AuthenticatedWebSession = session,
    ) -> Response:
        require_version(generation)
        versions = {
            key: f"{key.title()} {major}.{minor}"
            + (" RC2 (Preview)" if key == "piers" else "")
            for key, (major, minor) in RELEASES.items()
        }
        return templates.TemplateResponse(
            request,
            "managed-kodi.html",
            {
                "csrf_token": authenticated.csrf_token,
                "generation": generation,
                "versions": versions,
            },
            headers={"Cache-Control": "no-store"},
        )

    @router.get("/api/managed-kodi/{version}")
    def status(
        version: str,
        _admin: AuthenticatedWebSession = session,
    ) -> Response:
        require_version(version)
        try:
            result = broker.request("admin", "managed_status", scope(version))["result"]
            if isinstance(result, dict) and result.get("state") in {
                "starting",
                "stopping",
            }:
                return JSONResponse(
                    {"profile": result}, headers={"Cache-Control": "no-store"}
                )
            saved = broker.request("admin", "snapshot_list", {})["result"]
        except Refusal as exc:
            return refused(exc)
        return JSONResponse(
            {"profile": result, "snapshots": saved},
            headers={"Cache-Control": "no-store"},
        )

    @router.post("/api/managed-kodi/{version}/actions")
    def action(
        version: str,
        action: ManagedAction,
        _admin: AuthenticatedWebSession = mutation,
    ) -> Response:
        require_version(version)
        if "generation" in action.arguments:
            raise HTTPException(400, "Use the managed runtime in the route")
        if action.operation not in WEB_OPERATIONS:
            raise HTTPException(404, "Unknown managed action")
        try:
            result = broker.request(
                "admin",
                action.operation,
                {**action.arguments, **scope(version)}
                if action.operation in VERSION_SCOPED
                else action.arguments,
            )
        except Refusal as exc:
            return refused(exc)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @router.get("/api/managed-kodi/{version}/screen")
    def screen(
        version: str,
        _admin: AuthenticatedWebSession = session,
    ) -> Response:
        require_version(version)
        try:
            result = broker.request("admin", "managed_screen", scope(version))["result"]
            if not isinstance(result, dict) or not isinstance(result.get("png"), str):
                raise Refusal("managed_screenshot_unavailable")
            image = base64.b64decode(result["png"], validate=True)
        except (Refusal, binascii.Error) as exc:
            return refused(exc)
        if not image.startswith(b"\x89PNG\r\n\x1a\n"):
            raise HTTPException(502, "Invalid managed screenshot")
        return Response(
            image, media_type="image/png", headers={"Cache-Control": "no-store"}
        )

    @router.websocket("/api/managed-kodi/{version}/view")
    async def viewer(websocket: WebSocket, version: str) -> None:
        if version not in RELEASES:
            await websocket.close(code=4404)
            return
        await stream_view(
            websocket,
            broker,
            resolve_session,
            cookie_name,
            same_origin,
            require_https,
            recheck_passive_session=recheck_passive_session,
            generation=version,
        )

    return router


def scope(version: str) -> dict[str, str]:
    return {"generation": version} if version != "omega" else {}


def require_version(version: str) -> None:
    if version not in RELEASES:
        raise HTTPException(404, "Managed runtime is unavailable")


def refused(error: Exception) -> JSONResponse:
    code = (
        str(error) if isinstance(error, Refusal) else "managed_screenshot_unavailable"
    )
    status = 503 if code == "operation_outcome_unconfirmed" else 409
    return JSONResponse(
        {"code": code}, status_code=status, headers={"Cache-Control": "no-store"}
    )
