import json
from collections.abc import Callable
from typing import Any
from uuid import UUID

import pytest

from kodi_lab.broker.docker_inventory import ContainerRuntimeState, KodiProcessState
from kodi_lab.broker.operations import ObserveManagedKodiRuntimeResult
from kodi_lab.domain import (
    ProcessObservationKind,
    PythonBytecodePolicy,
    ReadinessProbeKind,
    RuntimeExecutionProfile,
    RuntimeKind,
)
from kodi_lab.kodi.addons import (
    KODI_ERROR_MESSAGE_MAX_LENGTH,
    KodiAddonSmokeAdapter,
    KodiAddonSmokeAmbiguousError,
    KodiAddonSmokeJsonRpcError,
    KodiAddonSmokeTimeoutError,
    sanitize_kodi_error_message,
)
from kodi_lab.kodi.readiness import JsonRpcUnavailableError

REVISION_ID = UUID(int=2)


def profile() -> RuntimeExecutionProfile:
    return RuntimeExecutionProfile(
        runtime_revision_id=REVISION_ID,
        runtime_kind=RuntimeKind.KODI,
        runtime_uid=1000,
        runtime_gid=1000,
        python_bytecode_policy=PythonBytecodePolicy.SUPPRESS,
        expected_application_name="Kodi",
        expected_kodi_version="21.3",
        expected_version_tag="stable",
        expected_prerelease_ordinal=None,
        jsonrpc_port=8080,
        jsonrpc_path="/jsonrpc",
        process_observation=ProcessObservationKind.DOCKER_HEALTHCHECK,
        readiness_probe=ReadinessProbeKind.KODI_JSONRPC,
        readiness_timeout_seconds=90,
        poll_interval_seconds=1.0,
        http_timeout_seconds=3.0,
    )


def observation() -> ObserveManagedKodiRuntimeResult:
    return ObserveManagedKodiRuntimeResult(
        managed_instance_id=str(UUID(int=1)),
        runtime_revision_id=str(REVISION_ID),
        container_id="a" * 64,
        runtime_state=ContainerRuntimeState.RUNNING,
        process_state=KodiProcessState.RUNNING,
        ipv4_address="10.250.0.9",
        jsonrpc_port=8080,
    )


def response(*, request_id: str = "kodi-lab-addon-smoke", result: object) -> bytes:
    return json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}).encode()


class FakeTransport:
    def __init__(
        self,
        value: bytes | BaseException,
        *,
        after_post: Callable[[], None] | None = None,
    ) -> None:
        self.value = value
        self.after_post = after_post
        self.requests: list[dict[str, Any]] = []
        self.bodies: list[bytes] = []
        self.timeouts: list[float] = []

    def post(
        self, *, host: str, port: int, path: str, body: bytes, timeout: float
    ) -> bytes:
        assert (host, port, path) == ("10.250.0.9", 8080, "/jsonrpc")
        self.bodies.append(body)
        self.requests.append(json.loads(body))
        self.timeouts.append(timeout)
        if self.after_post is not None:
            self.after_post()
        if isinstance(self.value, BaseException):
            raise self.value
        return self.value


@pytest.mark.parametrize(
    ("files", "count"),
    [
        ([], 0),
        (
            [
                {
                    "file": "plugin://discarded/",
                    "filetype": "directory",
                    "label": "x",
                    "type": "unknown",
                }
            ],
            1,
        ),
    ],
)
def test_invokes_only_fixed_bounded_directory_request(
    files: list[object], count: int
) -> None:
    transport = FakeTransport(
        response(
            result={
                "limits": {"start": 0, "end": count, "total": count},
                "files": files,
            }
        )
    )
    result = KodiAddonSmokeAdapter(
        observation(), profile(), clock=lambda: 1.0, transport=transport
    ).invoke("plugin.video.example")
    assert result.item_count == count
    assert result.duration_ms == 0
    assert transport.timeouts == [10.0]
    assert transport.requests == [
        {
            "jsonrpc": "2.0",
            "id": "kodi-lab-addon-smoke",
            "method": "Files.GetDirectory",
            "params": {
                "directory": "plugin://plugin.video.example/",
                "media": "video",
                "properties": [],
                "limits": {"start": 0, "end": 1},
            },
        }
    ]
    assert transport.bodies == [
        b'{"jsonrpc":"2.0","id":"kodi-lab-addon-smoke","method":"Files.GetDirectory",'
        b'"params":{"directory":"plugin://plugin.video.example/","media":"video",'
        b'"properties":[],"limits":{"start":0,"end":1}}}'
    ]


def test_negative_diagnostic_dispatches_only_fixed_method_and_correlates_error() -> (
    None
):
    transport = FakeTransport(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": "kodi-lab-rpc-negative-diagnostic",
                "error": {
                    "code": -32601,
                    "message": "Method not found.",
                    "data": {"secret": "unretained"},
                },
            }
        ).encode()
    )
    adapter = KodiAddonSmokeAdapter(observation(), profile(), transport=transport)
    with pytest.raises(KodiAddonSmokeJsonRpcError) as raised:
        adapter.invoke_negative_diagnostic()
    assert raised.value.diagnostic is not None
    assert (raised.value.diagnostic.code, raised.value.diagnostic.message) == (
        -32601,
        "Method not found.",
    )
    assert transport.requests == [
        {
            "jsonrpc": "2.0",
            "id": "kodi-lab-rpc-negative-diagnostic",
            "method": "KodiLab.NonexistentDiagnosticMethod",
        }
    ]
    assert transport.timeouts == [10.0]


@pytest.mark.parametrize(
    "payload",
    [
        response(request_id="kodi-lab-rpc-negative-diagnostic", result="OK"),
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": "wrong",
                "error": {"code": -32601, "message": "Method not found."},
            }
        ).encode(),
    ],
)
def test_negative_diagnostic_rejects_uncorrelated_or_success_response(
    payload: bytes,
) -> None:
    with pytest.raises(KodiAddonSmokeAmbiguousError):
        KodiAddonSmokeAdapter(
            observation(), profile(), transport=FakeTransport(payload)
        ).invoke_negative_diagnostic()


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json",
        b"{}",
        response(request_id="wrong", result={"limits": {}}),
        response(result={"files": [], "limits": {"start": 0, "end": 0}}),
        response(
            result={
                "files": [
                    {
                        "file": "a",
                        "filetype": "file",
                        "label": "a",
                        "type": "unknown",
                    },
                    {
                        "file": "b",
                        "filetype": "file",
                        "label": "b",
                        "type": "unknown",
                    },
                ],
                "limits": {"start": 0, "end": 2, "total": 2},
            }
        ),
        response(
            result={
                "files": [{"file": "a", "filetype": "file", "type": "unknown"}],
                "limits": {"start": 0, "end": 1, "total": 1},
            }
        ),
        response(
            result={
                "files": [
                    {
                        "file": "a",
                        "filetype": "file",
                        "label": "a",
                        "type": "bad\nvalue",
                    }
                ],
                "limits": {"start": 0, "end": 1, "total": 1},
            }
        ),
    ],
)
def test_rejects_malformed_wrong_correlation_and_oversized_results(
    payload: bytes,
) -> None:
    with pytest.raises(KodiAddonSmokeAmbiguousError):
        KodiAddonSmokeAdapter(
            observation(), profile(), transport=FakeTransport(payload)
        ).invoke("plugin.video.example")


def test_classifies_only_a_correlated_kodi_error() -> None:
    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": "kodi-lab-addon-smoke",
            "error": {
                "code": -32602,
                "message": "Invalid params.",
                "data": {"path": "/secret"},
            },
        }
    ).encode()
    with pytest.raises(KodiAddonSmokeJsonRpcError) as raised:
        KodiAddonSmokeAdapter(
            observation(),
            profile(),
            clock=iter([1.0, 1.125]).__next__,
            transport=FakeTransport(payload),
        ).invoke("plugin.video.example")
    assert raised.value.duration_ms == 125
    assert raised.value.diagnostic is not None
    assert raised.value.diagnostic.code == -32602
    assert raised.value.diagnostic.message == "Invalid params."
    assert "secret" not in repr(raised.value.diagnostic)


@pytest.mark.parametrize(
    ("code", "message"),
    [(-32601, "Method not found."), (-32602, "Invalid params.")],
)
def test_smoke_error_keeps_only_safe_code_and_message(code: int, message: str) -> None:
    payload = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": "kodi-lab-addon-smoke",
            "error": {"code": code, "message": message, "data": {"secret": "raw"}},
        }
    ).encode()
    with pytest.raises(KodiAddonSmokeJsonRpcError) as raised:
        KodiAddonSmokeAdapter(
            observation(), profile(), transport=FakeTransport(payload)
        ).invoke("plugin.video.example")
    assert raised.value.diagnostic is not None
    assert (raised.value.diagnostic.code, raised.value.diagnostic.message) == (
        code,
        message,
    )
    assert "raw" not in repr(raised.value)


def test_smoke_error_message_is_single_line_bounded_and_redacted() -> None:
    unsafe = (
        "Invalid\nparams.\t/var/lib/kodi/private C:\\Users\\kyle\\secret "
        "\\\\server\\share\\secret token=abc password:xyz "
        'api_key="quoted secret" Bearer qwerty '
        "Authorization: Basic encodedCredential "
        "https://user:pass@example.invalid/private " + "x" * 400
    )
    safe = sanitize_kodi_error_message(unsafe)
    assert safe is not None
    assert len(safe) == KODI_ERROR_MESSAGE_MAX_LENGTH
    assert "\n" not in safe and "\t" not in safe
    for secret in (
        "/var/lib",
        "C:\\Users",
        "server\\share",
        "abc",
        "xyz",
        "qwerty",
        "encodedCredential",
        "quoted secret",
        "user:pass",
    ):
        assert secret not in safe
    assert safe.count("[redacted]") >= 5
    assert sanitize_kodi_error_message("\r\n\t") is None


def test_timeout_after_dispatch_is_distinct_and_bounded() -> None:
    error = JsonRpcUnavailableError("timed out")
    error.__cause__ = TimeoutError()
    with pytest.raises(KodiAddonSmokeTimeoutError) as raised:
        KodiAddonSmokeAdapter(
            observation(),
            profile(),
            clock=iter([1.0, 11.0]).__next__,
            transport=FakeTransport(error),
        ).invoke("plugin.video.example")
    assert raised.value.duration_ms == 10_000


def test_request_api_has_no_caller_invocation_parameters() -> None:
    with pytest.raises(TypeError):
        KodiAddonSmokeAdapter(observation(), profile()).invoke(  # type: ignore[call-arg]
            "plugin.video.example", route="caller-controlled"
        )
