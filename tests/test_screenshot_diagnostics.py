from __future__ import annotations

import json
import logging
from typing import Any, cast
from uuid import uuid4

import pytest

from kodi_lab.screenshot_diagnostics import (
    LOGGER,
    MAX_DIAGNOSTIC_COUNT,
    MAX_SCREENSHOT_DIAGNOSTIC_LOG_CHARACTERS,
    ScreenshotDiagnosticTrace,
    bind_screenshot_bridge_revision,
    emit_screenshot_observation,
    emit_screenshot_stage,
    observe_screenshot_diagnostic,
    screenshot_diagnostic_trace,
)


def _protocol_request() -> dict[str, str | int]:
    return {"type": "string", "byte_length": 36, "sha256": "a" * 64}


def test_metadata_values_are_typed_bounded_and_hostile_values_are_safe() -> None:
    secret = "secret-payload-" + ("x" * 100_000)
    with screenshot_diagnostic_trace(
        protocol_request=_protocol_request(),
        client_id=uuid4(),
        target_id=uuid4(),
        request_id=uuid4(),
    ) as trace:
        assert trace is not None
        emit_screenshot_stage(
            "bridge_response_received",
            "passed",
            "http_response_received",
            http_status=True,
            content_type=secret + "\r\ncontrol",
            response_byte_length=2**63,
            payload_present="yes",
            image_sha256=secret,
            detected_format=secret,
            width=100_000,
            height=-1,
            color_mode=object(),
            unexpected_payload=secret,
        )

    assert len(trace.events) == 1
    event = trace.events[0]
    assert event["content_type"] == "unrecognized"
    assert event["detected_format"] == "unrecognized"
    assert event["color_mode"] == "unrecognized"
    for key in (
        "http_status",
        "response_byte_length",
        "payload_present",
        "image_sha256",
        "width",
        "height",
        "unexpected_payload",
    ):
        assert key not in event
    serialized = json.dumps(event, ensure_ascii=True)
    assert secret not in serialized
    assert len(serialized) < MAX_SCREENSHOT_DIAGNOSTIC_LOG_CHARACTERS


def test_unknown_schema_inputs_degrade_without_throwing() -> None:
    with screenshot_diagnostic_trace(
        protocol_request={"raw": "not allowed"},
        client_id=uuid4(),
        target_id=uuid4(),
        request_id=uuid4(),
    ) as trace:
        assert trace is not None
        bind_screenshot_bridge_revision(cast(Any, "invalid"))
        emit_screenshot_stage("not_a_stage", "passed", "ignored")
        emit_screenshot_stage("image_payload", "not_an_outcome", "ignored")
        emit_screenshot_stage("image_payload", "passed", "BAD\nSECRET")

    assert len(trace.events) == 1
    assert trace.events[0]["code"] == "unclassified"
    assert trace.events[0]["protocol_request"] == {"type": "unrecognized"}
    assert trace.events[0]["bridge_revision"] is None


class _RaisingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.called = False

    def emit(self, record: logging.LogRecord) -> None:
        self.called = True
        raise RuntimeError("handler failed")


class _RaisingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        raise RuntimeError("formatter failed")


class _FormattingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.called = False

    def emit(self, record: logging.LogRecord) -> None:
        self.called = True
        self.format(record)


class _BrokenSink:
    def write(self, value: str) -> None:
        raise OSError("sink failed")


class _SinkHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.sink = _BrokenSink()
        self.called = False

    def emit(self, record: logging.LogRecord) -> None:
        self.called = True
        self.sink.write(record.getMessage())


class _CapturingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def test_oversized_or_hostile_metadata_never_reaches_the_log_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = _CapturingHandler()
    monkeypatch.setattr(LOGGER, "handlers", [handler])
    monkeypatch.setattr(LOGGER, "propagate", False)
    monkeypatch.setattr(LOGGER, "level", logging.INFO)
    monkeypatch.setattr(LOGGER, "disabled", False)
    monkeypatch.setattr(LOGGER.manager, "disable", logging.NOTSET)
    LOGGER.setLevel(logging.INFO)
    hostile = "credential\r\n" + ("z" * 100_000)

    with screenshot_diagnostic_trace(
        protocol_request=_protocol_request(),
        client_id=uuid4(),
        target_id=uuid4(),
        request_id=uuid4(),
    ):
        emit_screenshot_stage(
            "bridge_response_received",
            "passed",
            "http_response_received",
            content_type=hostile,
            unexpected_payload=hostile,
        )

    assert len(handler.messages) == 1
    assert hostile not in handler.messages[0]
    assert "credential" not in handler.messages[0]
    assert len(handler.messages[0]) < MAX_SCREENSHOT_DIAGNOSTIC_LOG_CHARACTERS


@pytest.mark.parametrize(
    "handler",
    [
        _RaisingHandler(),
        _FormattingHandler(),
        _SinkHandler(),
    ],
)
def test_logger_formatter_handler_and_sink_failures_are_swallowed(
    monkeypatch: pytest.MonkeyPatch, handler: logging.Handler
) -> None:
    if isinstance(handler, _FormattingHandler):
        handler.setFormatter(_RaisingFormatter())
    monkeypatch.setattr(LOGGER, "handlers", [handler])
    monkeypatch.setattr(LOGGER, "propagate", False)
    monkeypatch.setattr(LOGGER, "level", logging.INFO)
    monkeypatch.setattr(LOGGER, "disabled", False)
    monkeypatch.setattr(LOGGER.manager, "disable", logging.NOTSET)
    LOGGER.setLevel(logging.INFO)

    with screenshot_diagnostic_trace(
        protocol_request=_protocol_request(),
        client_id=uuid4(),
        target_id=uuid4(),
        request_id=uuid4(),
    ) as trace:
        assert trace is not None
        emit_screenshot_stage("image_payload", "passed", "raw_png_payload_received")

    assert [event["code"] for event in trace.events] == ["raw_png_payload_received"]
    assert cast(Any, handler).called is True


def test_serializer_and_internal_emitter_failures_are_swallowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with screenshot_diagnostic_trace(
        protocol_request=_protocol_request(),
        client_id=uuid4(),
        target_id=uuid4(),
        request_id=uuid4(),
    ) as trace:
        assert trace is not None

        def fail_serializer(*_args: object, **_kwargs: object) -> str:
            raise RuntimeError("serializer failed")

        monkeypatch.setattr(json, "dumps", fail_serializer)
        emit_screenshot_stage("image_payload", "passed", "raw_png_payload_received")
        assert len(trace.events) == 1

        def fail_emitter(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("emitter failed")

        monkeypatch.setattr(ScreenshotDiagnosticTrace, "_emit", fail_emitter)
        emit_screenshot_stage("mcp_result", "passed", "success")

    assert len(trace.events) == 1


@pytest.mark.parametrize(
    ("byte_length", "expected"),
    [
        (0, 0),
        (36, 36),
        (MAX_DIAGNOSTIC_COUNT, MAX_DIAGNOSTIC_COUNT),
        (MAX_DIAGNOSTIC_COUNT + 1, None),
        (-1, None),
    ],
)
def test_protocol_request_byte_length_uses_count_bound(
    byte_length: int, expected: int | None
) -> None:
    with screenshot_diagnostic_trace(
        protocol_request={
            "type": "string",
            "byte_length": byte_length,
            "sha256": "a" * 64,
        },
        client_id=uuid4(),
        target_id=uuid4(),
        request_id=uuid4(),
    ) as trace:
        assert trace is not None
        emit_screenshot_stage("mcp_authenticated", "passed", "bounded_request")

    protocol_request = trace.events[0]["protocol_request"]
    if expected is None:
        assert protocol_request == {"type": "unrecognized"}
    else:
        assert protocol_request == {
            "type": "string",
            "byte_length": expected,
            "sha256": "a" * 64,
        }


def test_diagnostic_observation_and_metadata_derivation_are_no_throw() -> None:
    def fail_observation() -> dict[str, object]:
        raise RuntimeError("forced diagnostic observation failure")

    assert observe_screenshot_diagnostic(fail_observation) is None
    with screenshot_diagnostic_trace(
        protocol_request=_protocol_request(),
        client_id=uuid4(),
        target_id=uuid4(),
        request_id=uuid4(),
    ) as trace:
        assert trace is not None
        emit_screenshot_observation(
            "image_payload", "passed", "mcp_base64_encoded", fail_observation
        )

    assert len(trace.events) == 1
    assert "image_sha256" not in trace.events[0]
