"""Strict JSON models and serialization for broker protocol version 1."""

from __future__ import annotations

import json
import re
from typing import Any, Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from kodi_lab.broker.errors import RequestError

PROTOCOL_VERSION: Final[Literal[1]] = 1
MAX_OPERATION_LENGTH = 64
_OPERATION_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


class StrictModel(BaseModel):
    """Protocol model with coercion and unknown fields disabled."""

    model_config = ConfigDict(extra="forbid", strict=True)


def is_canonical_uuid(value: object) -> bool:
    """Return whether a value is the lowercase canonical UUID spelling."""

    if not isinstance(value, str) or len(value) != 36:
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


class Request(StrictModel):
    """Exact protocol-v1 request envelope."""

    version: Literal[1]
    request_id: str
    operation: str = Field(min_length=1, max_length=MAX_OPERATION_LENGTH)
    params: dict[str, Any]

    @field_validator("version", mode="before")
    @classmethod
    def _strict_version(cls, value: object) -> object:
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError("version must be an integer")
        return value

    @field_validator("request_id")
    @classmethod
    def _canonical_request_id(cls, value: str) -> str:
        if not is_canonical_uuid(value):
            raise ValueError("request_id must be a canonical UUID string")
        return value

    @field_validator("operation")
    @classmethod
    def _valid_operation_name(cls, value: str) -> str:
        if _OPERATION_PATTERN.fullmatch(value) is None:
            raise ValueError("operation has an invalid name")
        return value


class ErrorBody(StrictModel):
    """Stable safe error data returned across the boundary."""

    code: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    message: str = Field(min_length=1, max_length=256)


class SuccessResponse(StrictModel):
    """Protocol-v1 success envelope."""

    version: Literal[1]
    request_id: str
    ok: Literal[True]
    result: dict[str, Any]

    @field_validator("version", mode="before")
    @classmethod
    def _strict_version(cls, value: object) -> object:
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError("version must be an integer")
        return value

    @field_validator("request_id")
    @classmethod
    def _canonical_request_id(cls, value: str) -> str:
        if not is_canonical_uuid(value):
            raise ValueError("request_id must be a canonical UUID string")
        return value


class ErrorResponse(StrictModel):
    """Protocol-v1 error envelope."""

    version: Literal[1]
    request_id: str | None
    ok: Literal[False]
    error: ErrorBody

    @field_validator("version", mode="before")
    @classmethod
    def _strict_version(cls, value: object) -> object:
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError("version must be an integer")
        return value

    @field_validator("request_id")
    @classmethod
    def _canonical_optional_request_id(cls, value: str | None) -> str | None:
        if value is not None and not is_canonical_uuid(value):
            raise ValueError("request_id must be a canonical UUID string")
        return value


type Response = SuccessResponse | ErrorResponse


class _DuplicateKeyError(ValueError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKeyError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant {value}")


def decode_json_object(payload: bytes) -> dict[str, object]:
    """Decode strict UTF-8 JSON while rejecting duplicate keys and constants."""

    try:
        text = payload.decode("utf-8", errors="strict")
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        RecursionError,
    ) as error:
        raise RequestError(
            "malformed_request", "Request body is not valid JSON."
        ) from error
    if not isinstance(value, dict):
        raise RequestError("malformed_request", "Request body must be a JSON object.")
    return value


def _safe_request_metadata(
    document: dict[str, object],
) -> tuple[str | None, str | None, int | None]:
    request_id_value = document.get("request_id")
    request_id: str | None = None
    if isinstance(request_id_value, str) and is_canonical_uuid(request_id_value):
        request_id = request_id_value

    operation_value = document.get("operation")
    operation = None
    if (
        isinstance(operation_value, str)
        and len(operation_value) <= MAX_OPERATION_LENGTH
        and _OPERATION_PATTERN.fullmatch(operation_value) is not None
    ):
        operation = operation_value

    version_value = document.get("version")
    version = (
        version_value
        if isinstance(version_value, int) and not isinstance(version_value, bool)
        else None
    )
    return request_id, operation, version


def decode_request(payload: bytes) -> Request:
    """Decode and strictly validate a protocol-v1 request."""

    document = decode_json_object(payload)
    request_id, operation, version = _safe_request_metadata(document)
    if version is not None and version != PROTOCOL_VERSION:
        raise RequestError(
            "unsupported_version",
            "The requested protocol version is not supported.",
            request_id=request_id,
            operation=operation,
            protocol_version=version,
        )
    try:
        return Request.model_validate(document)
    except ValidationError as error:
        raise RequestError(
            "malformed_request",
            "Request does not match the protocol schema.",
            request_id=request_id,
            operation=operation,
            protocol_version=version,
        ) from error


def encode_model(model: BaseModel) -> bytes:
    """Serialize a validated model as compact deterministic UTF-8 JSON."""

    return json.dumps(
        model.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")


def success_response(request_id: str, result: BaseModel) -> SuccessResponse:
    """Build a validated success response from a typed handler result."""

    return SuccessResponse(
        version=PROTOCOL_VERSION,
        request_id=request_id,
        ok=True,
        result=result.model_dump(mode="json"),
    )


def error_response(error: RequestError) -> ErrorResponse:
    """Build a safe structured error response."""

    return ErrorResponse(
        version=PROTOCOL_VERSION,
        request_id=error.request_id,
        ok=False,
        error=ErrorBody(code=error.code, message=error.safe_message),
    )
