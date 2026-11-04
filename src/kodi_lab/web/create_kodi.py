"""Protected Create intent, passive status and one explicit bounded continuation."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Literal, Protocol
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from starlette.concurrency import run_in_threadpool

from kodi_lab.application.create_kodi import CreateKodiService
from kodi_lab.application.create_kodi_runner import CreateKodiRunner
from kodi_lab.application.managed_kodi_prerequisites import (
    ManagedKodiPrerequisiteService,
    StableGeneration,
)
from kodi_lab.application.reconciliation import ManagedInstanceReconciliationService
from kodi_lab.auth.service import AuthenticatedWebSession
from kodi_lab.broker.operations import ManagedContainerInventoryResult
from kodi_lab.domain import RuntimeRecipeInstallationState, TargetType
from kodi_lab.domain.create_kodi import (
    CreateAcceptance,
    CreateCheckpoint,
    CreateConflict,
    CreateOperation,
    CreateOutcome,
    CreateStage,
    CreateTransitionError,
)
from kodi_lab.runtime.recipes import (
    load_installation_recipe,
    load_recorded_reviewed_recipe,
    recipe_target_id,
)
from kodi_lab.storage.models import (
    GlobalHeavyOperationLeaseRow,
    ManagedInstanceMutationLeaseRow,
    ManagedInstanceRow,
    ManagedProvisionReservationRow,
    ManagedStartReservationRow,
    RuntimeRecipeInstallationRow,
    TargetRow,
)

BASE = "/api/create-kodi/operations"
type NextAction = Literal[
    "continue", "wait", "check_again", "needs_attention", "complete"
]
type AvailabilityState = Literal[
    "not_created",
    "unverified",
    "running",
    "stopped",
    "unavailable",
    "changed",
    "unknown",
    "needs_attention",
]


class CreateApproval(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    request_id: UUID
    generation: StableGeneration
    recipe_id: str = Field(min_length=1, max_length=128)
    recipe_revision: int = Field(ge=1)
    recipe_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    owner_approved: bool


class CreateAvailability(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    target_id: UUID
    state: AvailabilityState
    message: str
    details_url: str | None = None


class CreateContinuation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    version: int = Field(ge=0)


class CreateStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    operation_id: UUID
    generation: str
    kodi_version: str | None
    version: int
    stage: CreateStage
    checkpoint: CreateCheckpoint
    phase: str
    status: Literal["waiting", "preparing", "needs_attention", "complete"]
    reason_code: str | None
    message: str
    complete: bool
    status_url: str
    next_action: NextAction
    instance: CreateAvailability
    # Advisory receipt observation only; continuation still verifies ownership.
    installation_result_available: bool | None = None


class CreateAccepted(CreateStatus):
    replayed: bool


class _Inventory(Protocol):
    def managed_container_inventory(self) -> ManagedContainerInventoryResult: ...


class CreateApiError(Exception):
    def __init__(
        self, status: int, code: str, message: str, operation_id: UUID | None = None
    ) -> None:
        self.status, self.code, self.message = status, code, message
        self.operation_id = operation_id


class CreateKodiAPI:
    """Keep passive presentation separate from explicit one-step execution."""

    def __init__(
        self,
        sessions: sessionmaker[Session],
        service: CreateKodiService,
        prerequisites: ManagedKodiPrerequisiteService,
        broker: _Inventory,
        runner: CreateKodiRunner,
    ) -> None:
        self._sessions, self.service = sessions, service
        self._prerequisites, self._broker = prerequisites, broker
        self._runner = runner
        # No broker, readiness registry or recovery executor is supplied.
        self._reconciliation = ManagedInstanceReconciliationService(sessions)

    def _new_intent_checks(self, generation: StableGeneration, target_id: UUID) -> None:
        try:
            snapshot = self._prerequisites.snapshot(generation)
        except Exception as error:
            raise CreateApiError(
                503,
                "create_prerequisites_unavailable",
                "Setup requirements could not be checked. Check again.",
            ) from error
        checks = {check.key: check for check in snapshot.checks}
        for key in ("installer", "activity", "runtime", "running_policy", "capacity"):
            item = checks.get(key)
            if item is None or item.status == "unknown":
                raise CreateApiError(
                    503,
                    "create_prerequisites_unavailable",
                    "Setup requirements could not be checked. Check again.",
                )
            if key == "installer" and item.status != "passed":
                raise CreateApiError(
                    503,
                    "create_prerequisites_unavailable",
                    "The installation service is unavailable. "
                    "Review setup requirements and Check again.",
                )
            if item.status == "attention" or (
                key == "activity" and item.status == "existing"
            ):
                raise CreateApiError(
                    409,
                    "create_prerequisites_need_attention",
                    "Setup requirements need attention. Review them and Check again.",
                )
        # Unknown Docker/Windows disk or VM memory observations stay advisory.
        storage = checks.get("data_storage")
        if storage is not None and storage.status == "attention":
            raise CreateApiError(
                409,
                "create_storage_unavailable",
                "Kodi Lab storage needs attention before setup can be approved.",
            )
        with self._sessions() as session:
            if any(
                session.scalar(select(model).limit(1)) is not None
                for model in (
                    ManagedInstanceMutationLeaseRow,
                    GlobalHeavyOperationLeaseRow,
                    ManagedProvisionReservationRow,
                    ManagedStartReservationRow,
                )
            ):
                raise CreateApiError(
                    409,
                    "create_operation_pending",
                    "Another Kodi operation needs to finish or be reviewed first.",
                )
            for instance in session.scalars(select(ManagedInstanceRow)):
                if self._reconciliation.status(
                    UUID(instance.target_id), read_session=session
                ).required:
                    raise CreateApiError(
                        409,
                        "create_recovery_required",
                        "An unfinished Kodi operation needs review before new setup.",
                    )
        try:
            inventory = self._broker.managed_container_inventory()
        except Exception as error:
            raise CreateApiError(
                503,
                "create_prerequisites_unavailable",
                "Kodi availability could not be checked. Check again.",
            ) from error
        if any(r.managed_instance_id == str(target_id) for r in inventory.containers):
            raise CreateApiError(
                409,
                "create_existing_kodi",
                "A Kodi installation already exists for this version. "
                "Review the existing setup.",
            )

    @staticmethod
    def _match_request(payload: CreateApproval, op: CreateOperation) -> None:
        if (
            payload.generation,
            payload.recipe_id,
            payload.recipe_revision,
            payload.recipe_digest,
        ) != (op.generation, op.recipe_id, op.recipe_revision, op.recipe_digest):
            raise CreateApiError(
                409,
                "create_request_conflict",
                "This request was already approved with different Kodi settings.",
                op.id,
            )

    def accept(self, payload: CreateApproval) -> CreateAcceptance:
        if payload.owner_approved is not True:
            raise CreateApiError(
                422, "create_approval_required", "Explicit Create approval is required."
            )
        existing = self.service.for_request(payload.request_id)
        if existing is not None:
            self._match_request(payload, existing)
        else:
            reviewed = load_installation_recipe(payload.generation)
            if (payload.recipe_id, payload.recipe_revision, payload.recipe_digest) != (
                reviewed.recipe.recipe_id,
                reviewed.recipe.recipe_revision,
                reviewed.digest,
            ):
                raise CreateApiError(
                    409,
                    "create_recipe_changed",
                    "Kodi setup details have changed. "
                    "Check requirements and review the selection again.",
                )
            target = recipe_target_id(reviewed)
            existing_target = self.service.for_target(target)
            if existing_target is not None:
                if existing_target.request_id == payload.request_id:
                    # A concurrent identical acceptance became visible between
                    # the request lookup and canonical lookup. It is a replay.
                    self._match_request(payload, existing_target)
                    return self.service.accept(
                        request_id=payload.request_id,
                        generation=payload.generation,
                        recipe_digest=payload.recipe_digest,
                        owner_approved=True,
                    )
                raise CreateApiError(
                    409,
                    "create_target_conflict",
                    "A saved Create request already exists for this Kodi.",
                    existing_target.id,
                )
            try:
                self._new_intent_checks(payload.generation, target)
            except CreateApiError:
                # Another process may have accepted this exact request while
                # observations were in progress. Never lose its durable identity.
                existing = self.service.for_request(payload.request_id)
                if existing is None:
                    raise
                self._match_request(payload, existing)
        try:
            return self.service.accept(
                request_id=payload.request_id,
                generation=payload.generation,
                recipe_digest=payload.recipe_digest,
                owner_approved=True,
            )
        except CreateConflict as error:
            code = (
                "create_request_conflict"
                if str(error) == "request_inputs_conflict"
                else "create_target_conflict"
            )
            raise CreateApiError(
                409,
                code,
                "A saved Create request conflicts with this approval.",
                error.operation_id,
            ) from error
        except ValueError as error:
            raise CreateApiError(
                409,
                "create_recipe_changed",
                "Kodi setup details have changed. "
                "Check requirements and review the selection again.",
            ) from error

    def continue_operation(self, operation_id: UUID, version: int) -> CreateStatus:
        try:
            op = self.service.get(operation_id)
            if op is None:
                raise CreateApiError(
                    404,
                    "create_operation_not_found",
                    "No saved Create request was found.",
                )
            if op.version != version:
                raise CreateTransitionError("stale_version")
            return self.present(
                self._runner.continue_once(operation_id, version=version)
            )
        except CreateApiError:
            raise
        except CreateTransitionError as error:
            stale = str(error) == "stale_version"
            raise CreateApiError(
                409,
                "create_stale_version" if stale else "create_continuation_blocked",
                "Setup changed. Read its saved status before continuing."
                if stale
                else "Setup cannot safely continue. Read its saved status and review "
                "the existing Kodi operation before taking further action.",
                operation_id,
            ) from error
        except Exception as error:
            # A response failure does not prove dispatch failed. Never retry here
            # or expose underlying broker/lease/transport exception text.
            raise CreateApiError(
                503,
                "create_continuation_unavailable",
                "The setup response is unavailable. Read its saved status before "
                "continuing; do not repeat earlier work.",
                operation_id,
            ) from error

    def _availability(self, op: CreateOperation) -> CreateAvailability:
        def value(
            state: AvailabilityState, message: str, details: bool = False
        ) -> CreateAvailability:
            return CreateAvailability(
                target_id=op.target_id,
                state=state,
                message=message,
                details_url=f"/instances/{op.target_id}" if details else None,
            )

        with self._sessions() as session:
            target = session.get(TargetRow, str(op.target_id))
            instance = session.get(ManagedInstanceRow, str(op.target_id))
            if (
                target is None
                or target.target_type is not TargetType.MANAGED
                or instance is None
            ):
                return value(
                    "unavailable" if op.container_id else "not_created",
                    "This Kodi is no longer registered."
                    if op.container_id
                    else "Kodi has not been created yet.",
                )
            if self._reconciliation.status(op.target_id, read_session=session).required:
                return value(
                    "needs_attention",
                    "An unfinished Kodi operation needs review.",
                    True,
                )
            if op.runtime_revision_id is None or op.container_id is None:
                return value(
                    "unverified", "Setup has not verified this Kodi yet.", True
                )
            if instance.runtime_revision_id != str(op.runtime_revision_id):
                return value(
                    "changed",
                    "This Kodi no longer matches the saved installation.",
                    True,
                )
        try:
            records = [
                r
                for r in self._broker.managed_container_inventory().containers
                if r.managed_instance_id == str(op.target_id)
            ]
        except Exception:
            return value("unknown", "Kodi availability could not be checked.", True)
        if not records:
            return value("unavailable", "This Kodi is unavailable.", True)
        if len(records) != 1 or (
            records[0].container_id,
            records[0].runtime_revision_id,
            records[0].image_id,
        ) != (op.container_id, str(op.runtime_revision_id), op.image_id):
            return value(
                "changed", "This Kodi no longer matches the saved installation.", True
            )
        state = records[0].runtime_state
        availability_state: AvailabilityState = (
            "running"
            if state == "running"
            else "stopped"
            if state == "stopped"
            else "unknown"
        )
        return value(
            availability_state,
            "Kodi is running."
            if state == "running"
            else "Kodi is stopped."
            if state == "stopped"
            else "Kodi availability is not confirmed.",
            True,
        )

    def present(self, op: CreateOperation) -> CreateStatus:
        installation_result_available = None
        if (
            op.stage is CreateStage.INSTALLATION
            and op.checkpoint is CreateCheckpoint.DISPATCHED
            and op.installation_operation_id is not None
        ):
            with self._sessions() as session:
                child = session.get(
                    RuntimeRecipeInstallationRow, str(op.installation_operation_id)
                )
                installation_result_available = bool(
                    child is not None
                    and child.state
                    in {
                        RuntimeRecipeInstallationState.READY,
                        RuntimeRecipeInstallationState.FAILED,
                    }
                    and (
                        child.generation,
                        child.recipe_id,
                        child.recipe_revision,
                        child.recipe_digest,
                    )
                    == (
                        op.generation,
                        op.recipe_id,
                        op.recipe_revision,
                        op.recipe_digest,
                    )
                )
        try:
            kodi_version = load_recorded_reviewed_recipe(
                op.generation, op.recipe_id, op.recipe_revision, op.recipe_digest
            ).recipe.kodi_version
        except (ValueError, RuntimeError):
            kodi_version = None
        instance = self._availability(op)
        complete = op.checkpoint is CreateCheckpoint.COMPLETE
        state: Literal["waiting", "preparing", "needs_attention", "complete"] = (
            "preparing"
        )
        phase = {
            CreateStage.INSTALLATION: "Preparing Kodi",
            CreateStage.REGISTRATION: "Setting up Kodi",
            CreateStage.PROVISIONING: "Setting up Kodi",
            CreateStage.STARTUP: "Starting Kodi",
            CreateStage.READINESS: "Checking Kodi",
        }[op.stage]
        reason, message = None, f"{phase}."
        if complete:
            state, phase, message = (
                "complete",
                "Ready",
                "Kodi setup completed. " + instance.message,
            )
        elif op.checkpoint is CreateCheckpoint.NEEDS_ATTENTION:
            state, phase = "needs_attention", "Needs attention"
            reason = (
                "create_outcome_unconfirmed"
                if op.outcome is CreateOutcome.UNCERTAIN
                else "create_failed"
                if op.outcome is CreateOutcome.FAILED
                else "create_action_blocked"
            )
            message = (
                "Setup needs review. "
                "Its saved outcome does not permit an automatic retry."
            )
        elif instance.state == "needs_attention":
            state, phase, reason = (
                "needs_attention",
                "Needs attention",
                "create_recovery_required",
            )
            message = instance.message
        elif op.stage is CreateStage.INSTALLATION and op.checkpoint in {
            CreateCheckpoint.ACCEPTED,
            CreateCheckpoint.PLANNED,
        }:
            state, reason, message = (
                "waiting",
                "create_awaiting_setup",
                "Create approved. Setup has not started.",
            )
        elif op.checkpoint is CreateCheckpoint.DISPATCHED:
            state, reason = "waiting", "create_confirmation_pending"
            message = "Setup has been requested; its result has not been confirmed yet."
            if op.stage is CreateStage.READINESS:
                reason = "create_native_readiness_pending"
                message = "Kodi is still getting ready. Check again to verify it."
        # Presentation guidance is derived only from this snapshot. It neither
        # inspects child ownership nor grants permission to skip admission.
        next_action: NextAction = "continue"
        if complete:
            next_action = "complete"
        elif state == "needs_attention":
            next_action = "needs_attention"
        elif op.checkpoint is CreateCheckpoint.DISPATCHED:
            next_action = (
                "check_again"
                if op.stage in {CreateStage.READINESS, CreateStage.REGISTRATION}
                else "wait"
            )
        return CreateStatus(
            operation_id=op.id,
            generation=op.generation,
            kodi_version=kodi_version,
            version=op.version,
            stage=op.stage,
            checkpoint=op.checkpoint,
            phase=phase,
            status=state,
            reason_code=reason,
            message=message,
            complete=complete,
            status_url=f"{BASE}/{op.id}",
            next_action=next_action,
            instance=instance,
            installation_result_available=installation_result_available,
        )


def create_kodi_router(
    api: CreateKodiAPI,
    authenticated: Callable[..., AuthenticatedWebSession],
    mutation: Callable[..., AuthenticatedWebSession],
) -> APIRouter:
    router = APIRouter()
    read_admin = Depends(authenticated)
    write_admin = Depends(mutation)

    def reply(
        content: BaseModel, status: int = 200, *, location: str | None = None
    ) -> JSONResponse:
        headers = {"Cache-Control": "no-store"}
        if location:
            headers["Location"] = location
        return JSONResponse(
            content.model_dump(mode="json"), status_code=status, headers=headers
        )

    def failure(error: CreateApiError) -> JSONResponse:
        body: dict[str, object] = {
            "error": {"code": error.code, "message": error.message}
        }
        if error.operation_id:
            body.update(
                operation_id=str(error.operation_id),
                status_url=f"{BASE}/{error.operation_id}",
            )
        return JSONResponse(
            body, status_code=error.status, headers={"Cache-Control": "no-store"}
        )

    def accept_approval(payload: CreateApproval) -> JSONResponse:
        try:
            accepted = api.accept(payload)
            result = CreateAccepted(
                **api.present(accepted.operation).model_dump(),
                replayed=accepted.replayed,
            )
            return reply(
                result, 200 if accepted.replayed else 202, location=result.status_url
            )
        except CreateApiError as error:
            return failure(error)

    async def read_json(request: Request) -> bytearray | JSONResponse:
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            return failure(
                CreateApiError(
                    415, "create_json_required", "Send a JSON Create request."
                )
            )
        body = bytearray()
        try:
            async with asyncio.timeout(5):
                async for chunk in request.stream():
                    if len(body) + len(chunk) > 4096:
                        return failure(
                            CreateApiError(
                                413,
                                "create_request_too_large",
                                "Create request is too large.",
                            )
                        )
                    body.extend(chunk)
        except TimeoutError:
            return failure(
                CreateApiError(
                    408, "create_request_timeout", "Create request timed out."
                )
            )
        return body

    @router.post(BASE, response_model=CreateAccepted)
    async def accept(
        request: Request, _admin: AuthenticatedWebSession = write_admin
    ) -> JSONResponse:
        # Authenticate/verify origin and CSRF before buffering/parsing approval.
        body = await read_json(request)
        if isinstance(body, JSONResponse):
            return body
        try:
            payload = CreateApproval.model_validate_json(body)
        except ValidationError:
            return failure(
                CreateApiError(
                    422,
                    "create_invalid_request",
                    "Provide a request UUID, reviewed stable Kodi recipe "
                    "and explicit approval. Unknown fields are not allowed.",
                )
            )
        return await run_in_threadpool(accept_approval, payload)

    def continue_once(operation_id: UUID, payload: CreateContinuation) -> JSONResponse:
        try:
            result = api.continue_operation(operation_id, payload.version)
            return reply(result, location=result.status_url)
        except CreateApiError as error:
            return failure(error)

    @router.post(BASE + "/{operation_id}/continue", response_model=CreateStatus)
    async def continue_operation(
        operation_id: UUID,
        request: Request,
        _admin: AuthenticatedWebSession = write_admin,
    ) -> JSONResponse:
        body = await read_json(request)
        if isinstance(body, JSONResponse):
            return body
        try:
            payload = CreateContinuation.model_validate_json(body)
            if request.query_params:
                raise ValueError("unexpected_query")
        except (ValidationError, ValueError):
            return failure(
                CreateApiError(
                    422,
                    "create_invalid_request",
                    "Provide only the saved nonnegative integer version. "
                    "Unknown fields and query parameters are not allowed.",
                )
            )
        # Broker dispatch and bounded native probes are synchronous. Keep the
        # entire one-shot action/presentation off the application event loop.
        return await run_in_threadpool(continue_once, operation_id, payload)

    @router.get(BASE + "/by-target/{target_id}", response_model=CreateStatus)
    def reconnect(
        target_id: UUID, _admin: AuthenticatedWebSession = read_admin
    ) -> JSONResponse:
        op = api.service.for_target(target_id)
        return (
            reply(api.present(op))
            if op
            else failure(
                CreateApiError(
                    404,
                    "create_operation_not_found",
                    "No saved Create request was found.",
                )
            )
        )

    @router.get(BASE + "/{operation_id}", response_model=CreateStatus)
    def status(
        operation_id: UUID, _admin: AuthenticatedWebSession = read_admin
    ) -> JSONResponse:
        op = api.service.get(operation_id)
        return (
            reply(api.present(op))
            if op
            else failure(
                CreateApiError(
                    404,
                    "create_operation_not_found",
                    "No saved Create request was found.",
                )
            )
        )

    return router
