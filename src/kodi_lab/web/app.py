"""Explicit FastAPI application factory for the Phase 4 Web slice."""

# FastAPI intentionally represents dependencies and body metadata as defaults.
# ruff: noqa: B008

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import Body, Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, StrictBool
from sqlalchemy.orm import Session, sessionmaker
from starlette.middleware.base import RequestResponseEndpoint
from starlette.middleware.trustedhost import TrustedHostMiddleware

from kodi_lab.application.errors import (
    ManagedInstanceNotFoundError,
    TargetNotFoundError,
)
from kodi_lab.application.instance_delete_operations import (
    ManagedInstanceDeleteConflictError,
    ManagedInstanceDeleteNotFoundError,
    ManagedInstanceDeleteOperationCoordinator,
    ManagedInstanceDeleteStatus,
    ManagedInstanceDeleteUnsupportedError,
)
from kodi_lab.application.instance_deletions import (
    DisposableManagedInstanceDeletionService,
    ManagedInstanceDeleteExecutor,
    ManagedInstanceDeleteServiceFactory,
)
from kodi_lab.application.mutations import ManagedInstanceMutationConflictError
from kodi_lab.application.readiness_tasks import ReadinessTaskRegistry
from kodi_lab.application.reset import DisposableInstanceResetService
from kodi_lab.application.reset_activity import ResetActivityRegistry
from kodi_lab.application.reset_operations import (
    QueuedResetOperation,
    ResetExecutor,
    ResetOperationConflictError,
    ResetOperationCoordinator,
    ResetServiceFactory,
)
from kodi_lab.application.reset_status import (
    ManagedInstanceResetStatus,
    ResetStatusService,
)
from kodi_lab.application.runtime_lifecycle import (
    LifecycleConflictError,
    ManagedRuntimeLifecycleService,
    ReadinessCoordinatorFactory,
)
from kodi_lab.application.runtime_matrix import (
    RuntimeMatrixResponse,
    RuntimeMatrixService,
    TargetRuntimeSelection,
)
from kodi_lab.application.runtime_status import (
    ManagedRuntimeStatus,
    ManagedRuntimeStatusService,
)
from kodi_lab.application.snapshot_delete_operations import (
    SnapshotDeleteConflictError,
    SnapshotDeleteNotFoundError,
    SnapshotDeleteOperationCoordinator,
    SnapshotDeleteStatus,
    SnapshotDeleteUnsupportedError,
)
from kodi_lab.application.snapshot_deletions import (
    DisposableSnapshotDeletionService,
    SnapshotDeleteExecutor,
    SnapshotDeleteServiceFactory,
)
from kodi_lab.application.snapshot_operations import (
    SnapshotCreateConflictError,
    SnapshotCreateOperationCoordinator,
    SnapshotCreateStatus,
    SnapshotCreateUnsupportedError,
)
from kodi_lab.application.snapshot_restore_operations import (
    SnapshotRestoreConflictError,
    SnapshotRestoreOperationCoordinator,
    SnapshotRestoreStatus,
    SnapshotRestoreUnsupportedError,
    SnapshotSourceInstanceDeletedError,
)
from kodi_lab.application.snapshot_restores import (
    DisposableSnapshotRestoreService,
    SnapshotRestoreExecutor,
    SnapshotRestoreServiceFactory,
)
from kodi_lab.application.snapshot_status import (
    SnapshotHistoryItem,
    SnapshotStatusService,
)
from kodi_lab.application.snapshots import (
    DisposableSnapshotCreationService,
    SnapshotCreateExecutor,
    SnapshotServiceFactory,
)
from kodi_lab.auth import (
    AuthenticatedWebSession,
    LoginThrottler,
    WebAuthenticationService,
)
from kodi_lab.broker.docker_inventory import ContainerRuntimeState
from kodi_lab.broker.errors import (
    BrokerUnavailableError,
    ClientError,
    ClientTransportError,
    RemoteBrokerError,
)
from kodi_lab.config import Settings
from kodi_lab.domain import (
    ManagedInstanceDeleteOperationState,
    SnapshotSourceBinding,
    SnapshotState,
)
from kodi_lab.kodi.readiness import KodiReadinessCoordinator
from kodi_lab.storage.repositories import ManagedInstanceRepository, SnapshotRepository

_WEB_ROOT = Path(__file__).parent


class EmptyActionRequest(BaseModel):
    """Lifecycle requests deliberately contain no browser authority."""

    model_config = ConfigDict(extra="forbid")


class ResetConfirmationRequest(BaseModel):
    """One explicit confirmation bit, with no runtime or Docker authority."""

    model_config = ConfigDict(extra="forbid")
    confirmed: Literal[True]


class SnapshotCreateRequest(BaseModel):
    """Optional display metadata with no storage or runtime authority."""

    model_config = ConfigDict(extra="forbid")
    label: str | None = None


class SnapshotCreateAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: UUID
    operation: SnapshotCreateStatus


class SnapshotRestoreRequest(BaseModel):
    """One destructive confirmation bit and no artifact or runtime authority."""

    model_config = ConfigDict(extra="forbid")
    confirmed: Literal[True]


class SnapshotRestoreAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: UUID
    operation: SnapshotRestoreStatus


class SnapshotDeleteRequest(BaseModel):
    """One permanent-deletion confirmation bit and no storage authority."""

    model_config = ConfigDict(extra="forbid")
    confirmed: Literal[True]


class SnapshotDeleteAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: UUID
    operation: SnapshotDeleteStatus


class ManagedInstanceDeleteRequest(BaseModel):
    """Explicit destructive choice with no Snapshot-list or Docker authority."""

    model_config = ConfigDict(extra="forbid")
    confirmed: Literal[True]
    preserve_snapshots: StrictBool


@dataclass(frozen=True, slots=True)
class WebApplicationContext:
    settings: Settings
    auth: WebAuthenticationService
    throttler: LoginThrottler
    status: ManagedRuntimeStatusService
    lifecycle: ManagedRuntimeLifecycleService
    matrix: RuntimeMatrixService
    readiness: ReadinessTaskRegistry
    reset_activity: ResetActivityRegistry
    reset_status: ResetStatusService
    reset_operations: ResetOperationCoordinator
    reset_executor: ResetExecutor
    snapshot_status: SnapshotStatusService
    snapshot_operations: SnapshotCreateOperationCoordinator
    snapshot_executor: SnapshotCreateExecutor
    snapshot_restore_operations: SnapshotRestoreOperationCoordinator
    snapshot_restore_executor: SnapshotRestoreExecutor
    snapshot_delete_operations: SnapshotDeleteOperationCoordinator
    snapshot_delete_executor: SnapshotDeleteExecutor
    instance_delete_operations: ManagedInstanceDeleteOperationCoordinator
    instance_delete_executor: ManagedInstanceDeleteExecutor


def create_app(
    *,
    settings: Settings,
    session_factory: sessionmaker[Session],
    broker: Any,
    readiness: ReadinessTaskRegistry | None = None,
    reset_activity: ResetActivityRegistry | None = None,
    auth: WebAuthenticationService | None = None,
    throttler: LoginThrottler | None = None,
    coordinator_factory: ReadinessCoordinatorFactory | None = None,
    reset_service_factory: ResetServiceFactory | None = None,
    snapshot_service_factory: SnapshotServiceFactory | None = None,
    snapshot_restore_service_factory: SnapshotRestoreServiceFactory | None = None,
    snapshot_delete_service_factory: SnapshotDeleteServiceFactory | None = None,
    instance_delete_service_factory: ManagedInstanceDeleteServiceFactory | None = None,
) -> FastAPI:
    """Build an app entirely from injected storage and typed broker boundaries."""

    task_registry = readiness or ReadinessTaskRegistry()
    reset_registry = reset_activity or ResetActivityRegistry()
    reset_operations = ResetOperationCoordinator(session_factory)
    status_service = ManagedRuntimeStatusService(session_factory, broker, task_registry)
    reset_status_service = ResetStatusService(
        session_factory, broker, reset_registry, reset_operations
    )
    service_factory = reset_service_factory or (
        lambda observer: DisposableInstanceResetService(
            session_factory, broker, activity=observer
        )
    )
    reset_executor = ResetExecutor(reset_operations, reset_registry, service_factory)
    snapshot_operations = SnapshotCreateOperationCoordinator(session_factory)
    snapshot_factory = snapshot_service_factory or (
        lambda lease: DisposableSnapshotCreationService(
            session_factory,
            broker,
            snapshot_operations,
            lease,
            data_root=settings.data_root,
        )
    )
    snapshot_executor = SnapshotCreateExecutor(snapshot_operations, snapshot_factory)
    snapshot_restore_operations = SnapshotRestoreOperationCoordinator(session_factory)
    snapshot_restore_factory = snapshot_restore_service_factory or (
        lambda lease: DisposableSnapshotRestoreService(
            session_factory,
            broker,
            snapshot_restore_operations,
            lease,
            data_root=settings.data_root,
        )
    )
    snapshot_restore_executor = SnapshotRestoreExecutor(
        snapshot_restore_operations, snapshot_restore_factory
    )
    snapshot_delete_operations = SnapshotDeleteOperationCoordinator(session_factory)
    snapshot_delete_factory = snapshot_delete_service_factory or (
        lambda lease: DisposableSnapshotDeletionService(
            session_factory,
            broker,
            snapshot_delete_operations,
            lease,
            data_root=settings.data_root,
        )
    )
    snapshot_delete_executor = SnapshotDeleteExecutor(
        snapshot_delete_operations, snapshot_delete_factory
    )
    instance_delete_operations = ManagedInstanceDeleteOperationCoordinator(
        session_factory
    )
    instance_delete_factory = instance_delete_service_factory or (
        lambda lease: DisposableManagedInstanceDeletionService(
            session_factory,
            broker,
            task_registry,
            instance_delete_operations,
            lease,
            data_root=settings.data_root,
        )
    )
    instance_delete_executor = ManagedInstanceDeleteExecutor(
        instance_delete_operations, instance_delete_factory
    )
    context = WebApplicationContext(
        settings=settings,
        auth=auth or WebAuthenticationService(session_factory),
        throttler=throttler or LoginThrottler(),
        status=status_service,
        lifecycle=ManagedRuntimeLifecycleService(
            session_factory,
            broker,
            task_registry,
            status_service,
            coordinator_factory=coordinator_factory or KodiReadinessCoordinator,
            reset_operations=reset_operations,
        ),
        matrix=RuntimeMatrixService(
            session_factory,
            trusted_custom_builds_enabled=(settings.trusted_custom_builds_enabled),
        ),
        readiness=task_registry,
        reset_activity=reset_registry,
        reset_status=reset_status_service,
        reset_operations=reset_operations,
        reset_executor=reset_executor,
        snapshot_status=SnapshotStatusService(session_factory),
        snapshot_operations=snapshot_operations,
        snapshot_executor=snapshot_executor,
        snapshot_restore_operations=snapshot_restore_operations,
        snapshot_restore_executor=snapshot_restore_executor,
        snapshot_delete_operations=snapshot_delete_operations,
        snapshot_delete_executor=snapshot_delete_executor,
        instance_delete_operations=instance_delete_operations,
        instance_delete_executor=instance_delete_executor,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        context.reset_executor.start()
        context.snapshot_executor.start()
        context.snapshot_restore_executor.start()
        context.snapshot_delete_executor.start()
        context.instance_delete_executor.start()
        try:
            yield
        finally:
            context.reset_executor.close()
            context.snapshot_executor.close()
            context.snapshot_restore_executor.close()
            context.snapshot_delete_executor.close()
            context.instance_delete_executor.close()
            context.readiness.close()

    app = FastAPI(
        title="Kodi Lab",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.kodi_lab = context
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=[settings.web_host, "localhost"],
    )
    templates = Jinja2Templates(directory=_WEB_ROOT / "templates")
    app.mount("/static", StaticFiles(directory=_WEB_ROOT / "static"), name="static")

    @app.middleware("http")
    async def security_headers(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        )
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    def current_session(request: Request) -> AuthenticatedWebSession:
        token = request.cookies.get(settings.web_session_cookie_name)
        authenticated = context.auth.resolve(token)
        if authenticated is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
        return authenticated

    def mutation_session(
        request: Request,
        authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> AuthenticatedWebSession:
        _require_same_origin(request)
        supplied = request.headers.get("X-Kodi-Lab-CSRF")
        if not context.auth.csrf_matches(authenticated, supplied):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
        return authenticated

    @app.exception_handler(ManagedInstanceNotFoundError)
    async def instance_not_found(
        _request: Request, _error: ManagedInstanceNotFoundError
    ) -> JSONResponse:
        return _json_error(
            404, "managed_instance_not_found", "Managed instance not found."
        )

    @app.exception_handler(TargetNotFoundError)
    async def target_not_found(
        _request: Request, _error: TargetNotFoundError
    ) -> JSONResponse:
        return _json_error(404, "target_not_found", "Target not found.")

    @app.exception_handler(LifecycleConflictError)
    async def lifecycle_conflict(
        _request: Request, error: LifecycleConflictError
    ) -> JSONResponse:
        return _json_error(409, "lifecycle_conflict", str(error)[:256])

    @app.exception_handler(ResetOperationConflictError)
    async def reset_operation_conflict(
        _request: Request, error: ResetOperationConflictError
    ) -> JSONResponse:
        return _json_error(409, "reset_operation_conflict", str(error)[:256])

    @app.exception_handler(ManagedInstanceMutationConflictError)
    async def mutation_conflict(
        _request: Request, error: ManagedInstanceMutationConflictError
    ) -> JSONResponse:
        return _json_error(409, "managed_instance_mutation_conflict", str(error)[:256])

    @app.exception_handler(SnapshotCreateConflictError)
    async def snapshot_create_conflict(
        _request: Request, error: SnapshotCreateConflictError
    ) -> JSONResponse:
        return _json_error(409, "snapshot_create_conflict", str(error)[:256])

    @app.exception_handler(SnapshotCreateUnsupportedError)
    async def snapshot_create_unsupported(
        _request: Request, error: SnapshotCreateUnsupportedError
    ) -> JSONResponse:
        return _json_error(409, "snapshot_create_unsupported", str(error)[:256])

    @app.exception_handler(SnapshotRestoreConflictError)
    async def snapshot_restore_conflict(
        _request: Request, error: SnapshotRestoreConflictError
    ) -> JSONResponse:
        return _json_error(409, "snapshot_restore_conflict", str(error)[:256])

    @app.exception_handler(SnapshotRestoreUnsupportedError)
    async def snapshot_restore_unsupported(
        _request: Request, error: SnapshotRestoreUnsupportedError
    ) -> JSONResponse:
        return _json_error(409, "snapshot_restore_unsupported", str(error)[:256])

    @app.exception_handler(SnapshotSourceInstanceDeletedError)
    async def snapshot_source_deleted(
        _request: Request, _error: SnapshotSourceInstanceDeletedError
    ) -> JSONResponse:
        return _json_error(
            409,
            "source_instance_deleted",
            "The preserved Snapshot source ManagedInstance has been deleted.",
        )

    @app.exception_handler(SnapshotDeleteConflictError)
    async def snapshot_delete_conflict(
        _request: Request, error: SnapshotDeleteConflictError
    ) -> JSONResponse:
        return _json_error(409, "snapshot_delete_conflict", str(error)[:256])

    @app.exception_handler(SnapshotDeleteUnsupportedError)
    async def snapshot_delete_unsupported(
        _request: Request, error: SnapshotDeleteUnsupportedError
    ) -> JSONResponse:
        return _json_error(409, "snapshot_delete_unsupported", str(error)[:256])

    @app.exception_handler(SnapshotDeleteNotFoundError)
    async def snapshot_delete_not_found(
        _request: Request, _error: SnapshotDeleteNotFoundError
    ) -> JSONResponse:
        return _json_error(404, "snapshot_not_found", "Snapshot not found.")

    @app.exception_handler(ManagedInstanceDeleteConflictError)
    async def instance_delete_conflict(
        _request: Request, error: ManagedInstanceDeleteConflictError
    ) -> JSONResponse:
        return _json_error(409, "instance_delete_conflict", str(error)[:256])

    @app.exception_handler(ManagedInstanceDeleteUnsupportedError)
    async def instance_delete_unsupported(
        _request: Request, error: ManagedInstanceDeleteUnsupportedError
    ) -> JSONResponse:
        return _json_error(409, "instance_delete_unsupported", str(error)[:256])

    @app.exception_handler(ManagedInstanceDeleteNotFoundError)
    async def instance_delete_not_found(
        _request: Request, _error: ManagedInstanceDeleteNotFoundError
    ) -> JSONResponse:
        return _json_error(
            404, "managed_instance_not_found", "Managed instance not found."
        )

    @app.exception_handler(ClientError)
    async def broker_failure(_request: Request, error: ClientError) -> JSONResponse:
        if isinstance(error, (BrokerUnavailableError, ClientTransportError)):
            return _json_error(
                503, "runtime_service_unavailable", "Runtime service unavailable."
            )
        if isinstance(error, RemoteBrokerError):
            conflict_codes = {
                "managed_container_not_found",
                "ambiguous_managed_container",
                "container_identity_precondition_failed",
                "runtime_state_precondition_failed",
                "container_state_not_reconcilable",
                "container_state_not_stoppable",
                "runtime_revision_precondition_failed",
                "container_must_be_stopped",
                "provisioning_conflict",
            }
            code = 409 if error.code in conflict_codes else 503
            return _json_error(code, error.code, error.safe_message[:256])
        return _json_error(
            503, "runtime_service_unavailable", "Runtime service unavailable."
        )

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request) -> Response:
        token = request.cookies.get(settings.web_session_cookie_name)
        if context.auth.resolve(token) is not None:
            return RedirectResponse("/", status_code=303)
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "error": None,
                "credentials_configured": context.auth.credentials_configured(),
            },
        )

    @app.post("/login", response_class=HTMLResponse)
    async def login(request: Request) -> Response:
        _require_same_origin(request)
        form = await request.form()
        password_value = form.get("password")
        password = password_value if isinstance(password_value, str) else ""
        source = "unknown" if request.client is None else request.client.host
        if not context.throttler.allowed(source):
            return _login_failure(templates, request, context, status_code=401)
        created = context.auth.login(password)
        if created is None:
            context.throttler.record_failure(source)
            return _login_failure(templates, request, context, status_code=401)
        context.throttler.record_success(source)
        context.auth.logout(request.cookies.get(settings.web_session_cookie_name))
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            settings.web_session_cookie_name,
            created.token,
            max_age=settings.web_session_absolute_seconds,
            httponly=True,
            secure=settings.web_cookie_secure,
            samesite="strict",
            path="/",
        )
        return response

    @app.post("/logout")
    async def logout(request: Request) -> Response:
        _require_same_origin(request)
        authenticated = current_session(request)
        form = await request.form()
        csrf_value = form.get("csrf_token")
        csrf_token = csrf_value if isinstance(csrf_value, str) else None
        if not context.auth.csrf_matches(authenticated, csrf_token):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
        context.auth.logout(request.cookies.get(settings.web_session_cookie_name))
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(
            settings.web_session_cookie_name,
            path="/",
            secure=settings.web_cookie_secure,
            httponly=True,
            samesite="strict",
        )
        return response

    @app.get("/", response_class=HTMLResponse)
    async def main_page(request: Request) -> Response:
        try:
            authenticated = current_session(request)
        except HTTPException:
            return RedirectResponse("/login", status_code=303)
        reset_statuses = {
            item.managed_instance_id: item
            for item in context.reset_status.list_statuses()
        }
        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "instances": context.status.list_statuses(),
                "reset_statuses": reset_statuses,
                "csrf_token": authenticated.csrf_token,
                "poll_interval_ms": 2000,
            },
        )

    @app.get("/runtimes", response_class=HTMLResponse)
    async def runtime_matrix_page(
        request: Request,
        include_legacy: Literal["false", "true"] = "false",
        include_prerelease: Literal["false", "true"] = "false",
    ) -> Response:
        try:
            current_session(request)
        except HTTPException:
            return RedirectResponse("/login", status_code=303)
        matrix = context.matrix.list_matrix(
            include_legacy=_selection_bool(include_legacy),
            include_prerelease=_selection_bool(include_prerelease),
        )
        return templates.TemplateResponse(
            request,
            "runtimes.html",
            {"matrix": matrix},
        )

    @app.get("/instances/{instance_id}/reset", response_class=HTMLResponse)
    async def reset_confirmation_page(request: Request, instance_id: UUID) -> Response:
        try:
            authenticated = current_session(request)
        except HTTPException:
            return RedirectResponse("/login", status_code=303)
        reset_status = context.reset_status.get(instance_id)
        instance = context.status.get(instance_id)
        return templates.TemplateResponse(
            request,
            "reset-confirmation.html",
            {
                "instance": instance,
                "reset": reset_status,
                "csrf_token": authenticated.csrf_token,
            },
        )

    @app.get("/instances/{instance_id}/delete", response_class=HTMLResponse)
    async def instance_delete_page(request: Request, instance_id: UUID) -> Response:
        try:
            authenticated = current_session(request)
        except HTTPException:
            return RedirectResponse("/login", status_code=303)
        instance = context.status.get(instance_id)
        snapshots = context.snapshot_status.list_for_instance(instance_id)
        with session_factory() as session:
            persisted = ManagedInstanceRepository(session).get(instance_id)
        if persisted is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        counts = {
            state_value: sum(item.state.value == state_value for item in snapshots)
            for state_value in ("available", "failed", "quarantined")
        }
        return templates.TemplateResponse(
            request,
            "instance-delete.html",
            {
                "instance": instance,
                "snapshot_count": len(snapshots),
                "aggregate_snapshot_bytes": sum(
                    item.archive_size or 0 for item in snapshots
                ),
                "snapshot_counts": counts,
                "notes_present": persisted.notes is not None,
                "csrf_token": authenticated.csrf_token,
            },
        )

    @app.get("/snapshots/preserved", response_class=HTMLResponse)
    async def preserved_snapshot_page(request: Request) -> Response:
        try:
            authenticated = current_session(request)
        except HTTPException:
            return RedirectResponse("/login", status_code=303)
        return templates.TemplateResponse(
            request,
            "preserved-snapshots.html",
            {
                "snapshots": context.snapshot_status.list_preserved(),
                "csrf_token": authenticated.csrf_token,
            },
        )

    @app.get("/snapshots/preserved/{snapshot_id}/delete", response_class=HTMLResponse)
    async def preserved_snapshot_delete_page(
        request: Request, snapshot_id: UUID
    ) -> Response:
        try:
            authenticated = current_session(request)
        except HTTPException:
            return RedirectResponse("/login", status_code=303)
        snapshot = next(
            (
                item
                for item in context.snapshot_status.list_preserved()
                if item.snapshot_id == snapshot_id
            ),
            None,
        )
        if snapshot is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        if snapshot.state not in {
            SnapshotState.AVAILABLE,
            SnapshotState.FAILED,
            SnapshotState.QUARANTINED,
        }:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT)
        return templates.TemplateResponse(
            request,
            "preserved-snapshot-delete.html",
            {
                "snapshot": snapshot,
                "csrf_token": authenticated.csrf_token,
            },
        )

    @app.get("/instances/{instance_id}/snapshots", response_class=HTMLResponse)
    async def snapshot_history_page(request: Request, instance_id: UUID) -> Response:
        try:
            current_session(request)
        except HTTPException:
            return RedirectResponse("/login", status_code=303)
        instance = context.status.get(instance_id)
        return templates.TemplateResponse(
            request,
            "snapshots.html",
            {
                "instance": instance,
                "snapshots": context.snapshot_status.list_for_instance(instance_id),
            },
        )

    @app.get("/instances/{instance_id}/snapshots/new", response_class=HTMLResponse)
    async def snapshot_create_page(request: Request, instance_id: UUID) -> Response:
        try:
            authenticated = current_session(request)
        except HTTPException:
            return RedirectResponse("/login", status_code=303)
        return templates.TemplateResponse(
            request,
            "snapshot-create.html",
            {
                "instance": context.status.get(instance_id),
                "csrf_token": authenticated.csrf_token,
            },
        )

    @app.get(
        "/instances/{instance_id}/snapshots/{snapshot_id}/restore",
        response_class=HTMLResponse,
    )
    async def snapshot_restore_page(
        request: Request, instance_id: UUID, snapshot_id: UUID
    ) -> Response:
        try:
            authenticated = current_session(request)
        except HTTPException:
            return RedirectResponse("/login", status_code=303)
        instance = context.status.get(instance_id)
        snapshot = next(
            (
                item
                for item in context.snapshot_status.list_for_instance(instance_id)
                if item.snapshot_id == snapshot_id
            ),
            None,
        )

        if snapshot is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        with session_factory() as session:
            persisted_instance = ManagedInstanceRepository(session).get(instance_id)
        fixture_id = (
            None
            if persisted_instance is None
            else persisted_instance.fixture_definition_id
        )
        return templates.TemplateResponse(
            request,
            "snapshot-restore.html",
            {
                "instance": instance,
                "snapshot": snapshot,
                "runtime_match": (
                    instance.runtime_revision_id == snapshot.runtime_revision_id
                ),
                "fixture_match": fixture_id == snapshot.fixture_definition_id,
                "csrf_token": authenticated.csrf_token,
            },
        )

    @app.get(
        "/instances/{instance_id}/snapshots/{snapshot_id}/delete",
        response_class=HTMLResponse,
    )
    async def snapshot_delete_page(
        request: Request, instance_id: UUID, snapshot_id: UUID
    ) -> Response:
        try:
            authenticated = current_session(request)
        except HTTPException:
            return RedirectResponse("/login", status_code=303)
        instance = context.status.get(instance_id)
        snapshot = next(
            (
                item
                for item in context.snapshot_status.list_for_instance(instance_id)
                if item.snapshot_id == snapshot_id
            ),
            None,
        )
        if snapshot is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        if snapshot.state not in {
            SnapshotState.AVAILABLE,
            SnapshotState.FAILED,
            SnapshotState.QUARANTINED,
        }:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT)
        return templates.TemplateResponse(
            request,
            "snapshot-delete.html",
            {
                "instance": instance,
                "snapshot": snapshot,
                "csrf_token": authenticated.csrf_token,
            },
        )

    @app.get("/api/runtime-matrix", response_model=RuntimeMatrixResponse)
    def runtime_matrix(
        include_legacy: Literal["false", "true"] = "false",
        include_prerelease: Literal["false", "true"] = "false",
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> RuntimeMatrixResponse:
        return context.matrix.list_matrix(
            include_legacy=_selection_bool(include_legacy),
            include_prerelease=_selection_bool(include_prerelease),
        )

    @app.get(
        "/api/targets/{target_id}/runtime-selection",
        response_model=TargetRuntimeSelection,
    )
    def target_runtime_selection(
        target_id: UUID,
        include_legacy: Literal["false", "true"] = "false",
        include_prerelease: Literal["false", "true"] = "false",
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> TargetRuntimeSelection:
        return context.matrix.resolve_target(
            target_id,
            include_legacy=_selection_bool(include_legacy),
            include_prerelease=_selection_bool(include_prerelease),
        )

    @app.get("/api/instances", response_model=list[ManagedRuntimeStatus])
    def list_instances(
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> list[ManagedRuntimeStatus]:
        return context.status.list_statuses()

    @app.get("/api/instances/{instance_id}", response_model=ManagedRuntimeStatus)
    def get_instance(
        instance_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> ManagedRuntimeStatus:
        return context.status.get(instance_id)

    @app.get(
        "/api/instances/{instance_id}/reset-status",
        response_model=ManagedInstanceResetStatus,
    )
    def get_instance_reset_status(
        instance_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> ManagedInstanceResetStatus:
        return context.reset_status.get(instance_id)

    @app.get(
        "/api/instances/{instance_id}/snapshots",
        response_model=list[SnapshotHistoryItem],
    )
    def list_snapshots(
        instance_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> list[SnapshotHistoryItem]:
        return context.snapshot_status.list_for_instance(instance_id)

    @app.get("/api/snapshots/preserved", response_model=list[SnapshotHistoryItem])
    def list_preserved_snapshots(
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> list[SnapshotHistoryItem]:
        return context.snapshot_status.list_preserved()

    @app.get(
        "/api/instance-delete-operations/{operation_id}",
        response_model=ManagedInstanceDeleteStatus,
    )
    def instance_delete_operation(
        operation_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> ManagedInstanceDeleteStatus:
        operation = context.instance_delete_operations.get(operation_id)
        if operation is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        result = context.instance_delete_operations.to_status(operation)
        if operation.reconciliation_required:
            result = result.model_copy(
                update={
                    "reconciliation": (
                        context.instance_delete_operations.inspect_reconciliation(
                            operation, broker=broker, data_root=settings.data_root
                        )
                    )
                }
            )
        return result

    @app.get(
        "/api/instances/{instance_id}/snapshot-create-operations/{operation_id}",
        response_model=SnapshotCreateStatus,
    )
    def snapshot_create_operation(
        instance_id: UUID,
        operation_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> SnapshotCreateStatus:
        operation = context.snapshot_operations.get(operation_id)
        if operation is None or operation.managed_instance_id != instance_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        return context.snapshot_operations.to_status(operation)

    @app.get(
        "/api/instances/{instance_id}/snapshot-restore-operations/{operation_id}",
        response_model=SnapshotRestoreStatus,
    )
    def snapshot_restore_operation(
        instance_id: UUID,
        operation_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> SnapshotRestoreStatus:
        operation = context.snapshot_restore_operations.get(operation_id)
        if operation is None or operation.managed_instance_id != instance_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        result = context.snapshot_restore_operations.to_status(operation)
        if operation.reconciliation_required:
            observation = context.snapshot_restore_operations.inspect_reconciliation(
                operation, broker
            )
            result = result.model_copy(update={"reconciliation_state": observation})
        return result

    @app.get(
        "/api/instances/{instance_id}/snapshot-delete-operations/{operation_id}",
        response_model=SnapshotDeleteStatus,
    )
    def snapshot_delete_operation(
        instance_id: UUID,
        operation_id: UUID,
        _authenticated: AuthenticatedWebSession = Depends(current_session),
    ) -> SnapshotDeleteStatus:
        operation = context.snapshot_delete_operations.get(operation_id)
        if operation is None or operation.managed_instance_id != instance_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        result = context.snapshot_delete_operations.to_status(operation)
        if operation.reconciliation_required:
            result = result.model_copy(
                update={
                    "reconciliation_state": (
                        context.snapshot_delete_operations.inspect_reconciliation(
                            operation, data_root=settings.data_root
                        )
                    )
                }
            )
        return result

    @app.post(
        "/api/instances/{instance_id}/snapshots",
        response_model=SnapshotCreateAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def queue_snapshot_create(
        instance_id: UUID,
        body: SnapshotCreateRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> SnapshotCreateAccepted:
        context.status.get(instance_id)
        queued = context.snapshot_operations.queue(instance_id, label=body.label)
        context.snapshot_executor.submit(queued.operation.operation_id)
        return SnapshotCreateAccepted(
            snapshot_id=queued.snapshot.id,
            operation=queued.operation,
        )

    @app.post(
        "/api/instances/{instance_id}/snapshots/{snapshot_id}/restore",
        response_model=SnapshotRestoreAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def queue_snapshot_restore(
        instance_id: UUID,
        snapshot_id: UUID,
        _body: SnapshotRestoreRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> SnapshotRestoreAccepted:
        with session_factory() as session:
            routed_snapshot = SnapshotRepository(session).get(snapshot_id)
        if (
            routed_snapshot is not None
            and routed_snapshot.source_binding is SnapshotSourceBinding.DELETED_SOURCE
        ):
            raise SnapshotSourceInstanceDeletedError("source_instance_deleted")
        current = context.status.get(instance_id)
        if (
            current.provision_state.value != "provisioned"
            or current.container_state
            not in {ContainerRuntimeState.RUNNING, ContainerRuntimeState.STOPPED}
        ):
            raise SnapshotRestoreUnsupportedError(
                "Restore requires one exactly running or stopped managed container."
            )
        operation = context.snapshot_restore_operations.queue(instance_id, snapshot_id)
        if operation.state.value == "queued":
            context.snapshot_restore_executor.submit(operation.operation_id)
        return SnapshotRestoreAccepted(snapshot_id=snapshot_id, operation=operation)

    @app.post(
        "/api/instances/{instance_id}/snapshots/{snapshot_id}/delete",
        response_model=SnapshotDeleteAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def queue_snapshot_delete(
        instance_id: UUID,
        snapshot_id: UUID,
        _body: SnapshotDeleteRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> SnapshotDeleteAccepted:
        context.status.get(instance_id)
        operation = context.snapshot_delete_operations.queue(instance_id, snapshot_id)
        if operation.state.value == "queued":
            context.snapshot_delete_executor.submit(operation.operation_id)
        return SnapshotDeleteAccepted(snapshot_id=snapshot_id, operation=operation)

    @app.post(
        "/api/snapshots/preserved/{snapshot_id}/delete",
        response_model=SnapshotDeleteAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def queue_preserved_snapshot_delete(
        snapshot_id: UUID,
        _body: SnapshotDeleteRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> SnapshotDeleteAccepted:
        operation = context.snapshot_delete_operations.queue_preserved(snapshot_id)
        if operation.state.value == "queued":
            context.snapshot_delete_executor.submit(operation.operation_id)
        return SnapshotDeleteAccepted(snapshot_id=snapshot_id, operation=operation)

    @app.post(
        "/api/instances/{instance_id}/delete",
        response_model=ManagedInstanceDeleteStatus,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def queue_instance_delete(
        instance_id: UUID,
        body: ManagedInstanceDeleteRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> ManagedInstanceDeleteStatus:
        operation = context.instance_delete_operations.queue(
            instance_id, preserve_snapshots=body.preserve_snapshots
        )
        if operation.state is ManagedInstanceDeleteOperationState.QUEUED:
            context.instance_delete_executor.submit(operation.operation_id)
        return operation

    @app.post(
        "/api/instances/{instance_id}/reset",
        response_model=QueuedResetOperation,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def queue_instance_reset(
        instance_id: UUID,
        _body: ResetConfirmationRequest,
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> QueuedResetOperation:
        existing = context.reset_operations.active_for_instance(instance_id)
        if existing is not None:
            return QueuedResetOperation(
                context.reset_operations.to_status(existing), False
            )
        current = context.reset_status.get(instance_id)
        if current.eligibility.value != "supported":
            raise ResetOperationConflictError(current.eligibility_detail)
        queued = context.reset_operations.queue(instance_id)
        if queued.created:
            context.reset_executor.submit(queued.operation.operation_id)
        return queued

    @app.post(
        "/api/instances/{instance_id}/provision",
        response_model=ManagedRuntimeStatus,
    )
    def provision(
        instance_id: UUID,
        _body: EmptyActionRequest = Body(),
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> ManagedRuntimeStatus:
        return context.lifecycle.provision(instance_id)

    @app.post("/api/instances/{instance_id}/start", response_model=ManagedRuntimeStatus)
    def start(
        instance_id: UUID,
        _body: EmptyActionRequest = Body(),
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> ManagedRuntimeStatus:
        return context.lifecycle.start(instance_id)

    @app.post("/api/instances/{instance_id}/stop", response_model=ManagedRuntimeStatus)
    def stop(
        instance_id: UUID,
        _body: EmptyActionRequest = Body(),
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> ManagedRuntimeStatus:
        return context.lifecycle.stop(instance_id)

    @app.post(
        "/api/instances/{instance_id}/deprovision",
        response_model=ManagedRuntimeStatus,
    )
    def deprovision(
        instance_id: UUID,
        _body: EmptyActionRequest = Body(),
        _authenticated: AuthenticatedWebSession = Depends(mutation_session),
    ) -> ManagedRuntimeStatus:
        return context.lifecycle.deprovision(instance_id)

    return app


def _require_same_origin(request: Request) -> None:
    expected = (request.url.scheme.lower(), request.url.netloc.lower())
    origin = request.headers.get("origin")
    if origin is not None:
        parsed = urlsplit(origin)
        if (
            parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or (parsed.scheme.lower(), parsed.netloc.lower()) != expected
        ):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
        return
    referer = request.headers.get("referer")
    if referer is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    parsed = urlsplit(referer)
    if (parsed.scheme.lower(), parsed.netloc.lower()) != expected:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)


def _selection_bool(value: Literal["false", "true"]) -> bool:
    return value == "true"


def _login_failure(
    templates: Jinja2Templates,
    request: Request,
    context: WebApplicationContext,
    *,
    status_code: int,
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "error": "Authentication failed.",
            "credentials_configured": context.auth.credentials_configured(),
        },
        status_code=status_code,
    )


def _json_error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
    )
