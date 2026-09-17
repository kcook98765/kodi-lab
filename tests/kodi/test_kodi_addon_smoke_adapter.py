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
    KodiAddonSmokeAdapter,
    KodiAddonSmokeAmbiguousError,
    KodiAddonSmokeJsonRpcError,
    KodiAddonSmokeTimeoutError,
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
        self.timeouts: list[float] = []

    def post(
        self, *, host: str, port: int, path: str, body: bytes, timeout: float
    ) -> bytes:
        assert (host, port, path) == ("10.250.0.9", 8080, "/jsonrpc")
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
            "error": {"code": -32602, "message": "discarded"},
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
