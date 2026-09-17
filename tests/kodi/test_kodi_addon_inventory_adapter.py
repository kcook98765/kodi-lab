import json
from collections.abc import Callable, Sequence
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
    MAX_ADDON_ID_LENGTH,
    MAX_ADDON_NAME_LENGTH,
    KodiAddonInventoryAdapter,
    KodiAddonInventoryLimitExceededError,
    KodiAddonInventoryMalformedDataError,
    KodiAddonInventoryUnsupportedResponseError,
)
from kodi_lab.kodi.readiness import JsonRpcInvalidResponseError, JsonRpcUnavailableError

INSTANCE_ID = str(UUID(int=1))
REVISION_ID = UUID(int=2)
CONTAINER_ID = "a" * 64


def profile(
    version: str = "21.3", tag: str = "stable", ordinal: int | None = None
) -> RuntimeExecutionProfile:
    return RuntimeExecutionProfile(
        runtime_revision_id=REVISION_ID,
        runtime_kind=RuntimeKind.KODI,
        runtime_uid=1000,
        runtime_gid=1000,
        python_bytecode_policy=PythonBytecodePolicy.INTERPRETER_DEFAULT,
        expected_application_name="Kodi",
        expected_kodi_version=version,
        expected_version_tag=tag,
        expected_prerelease_ordinal=ordinal,
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
        managed_instance_id=INSTANCE_ID,
        runtime_revision_id=str(REVISION_ID),
        container_id=CONTAINER_ID,
        runtime_state=ContainerRuntimeState.RUNNING,
        process_state=KodiProcessState.RUNNING,
        ipv4_address="10.250.0.9",
        jsonrpc_port=8080,
    )


def rpc_response(request_id: str, result: object) -> bytes:
    return json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}).encode()


def identity_response(
    major: int = 21,
    minor: int = 3,
    tag: str = "stable",
    ordinal: int | None = None,
) -> bytes:
    version: dict[str, object] = {"major": major, "minor": minor, "tag": tag}
    if ordinal is not None:
        version["tagversion"] = str(ordinal)
    return rpc_response(
        "kodi-lab-readiness-identity", {"name": "Kodi", "version": version}
    )


def addon(
    addon_id: str = "plugin.video.example",
    *,
    name: str = "Example",
    version: str = "01.002-beta+7",
    enabled: object = True,
    kodi_type: str = "xbmc.python.pluginsource",
) -> dict[str, object]:
    return {
        "addonid": addon_id,
        "type": kodi_type,
        "name": name,
        "version": version,
        "enabled": enabled,
    }


def page(items: Sequence[object], *, start: int = 0, total: int | None = None) -> bytes:
    actual_total = len(items) if total is None else total
    return rpc_response(
        f"kodi-lab-addon-inventory-{start}",
        {
            "limits": {
                "start": start,
                "end": start + len(items),
                "total": actual_total,
            },
            "addons": items,
        },
    )


class FakeTransport:
    def __init__(
        self,
        responses: list[bytes | BaseException],
        *,
        after_post: Callable[[], None] | None = None,
    ) -> None:
        self.responses = responses
        self.requests: list[dict[str, Any]] = []
        self.after_post = after_post

    def post(
        self, *, host: str, port: int, path: str, body: bytes, timeout: float
    ) -> bytes:
        assert (host, port, path) == ("10.250.0.9", 8080, "/jsonrpc")
        assert 0 < timeout <= 3
        self.requests.append(json.loads(body))
        value = self.responses.pop(0)
        if self.after_post is not None:
            self.after_post()
        if isinstance(value, BaseException):
            raise value
        return value


@pytest.mark.parametrize(
    ("version", "tag", "ordinal", "identity"),
    [
        ("19.5", "stable", None, identity_response(19, 5)),
        ("20.5", "stable", None, identity_response(20, 5)),
        ("21.3", "stable", None, identity_response()),
        ("22.0", "beta", 2, identity_response(22, 0, "beta", 2)),
    ],
)
def test_normalizes_curated_runtime_responses_without_version_branches(
    version: str, tag: str, ordinal: int | None, identity: bytes
) -> None:
    transport = FakeTransport(
        [
            identity,
            page(
                [
                    addon("z.disabled", enabled=False, kodi_type="xbmc.service"),
                    addon("a.enabled", enabled=True),
                ]
            ),
        ]
    )
    result = KodiAddonInventoryAdapter(
        observation(), profile(version, tag, ordinal), transport=transport
    ).get_inventory()
    assert [item.addon_id for item in result.items] == ["a.enabled", "z.disabled"]
    assert result.items[0].version == "01.002-beta+7"
    assert result.items[0].kodi_type == "xbmc.python.pluginsource"
    assert result.items[1].enabled is False
    assert transport.requests[1] == {
        "jsonrpc": "2.0",
        "id": "kodi-lab-addon-inventory-0",
        "method": "Addons.GetAddons",
        "params": {
            "type": "unknown",
            "installed": True,
            "enabled": "all",
            "properties": ["name", "version", "enabled"],
            "limits": {"start": 0, "end": 64},
        },
    }


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json",
        b"{}",
        rpc_response("wrong", {}),
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": "kodi-lab-addon-inventory-0",
                "error": {"code": -1},
            }
        ).encode(),
        rpc_response("kodi-lab-addon-inventory-0", {"addons": []}),
    ],
)
def test_rejects_unsupported_envelope_or_result(payload: bytes) -> None:
    adapter = KodiAddonInventoryAdapter(
        observation(),
        profile(),
        transport=FakeTransport([identity_response(), payload]),
    )
    with pytest.raises(KodiAddonInventoryUnsupportedResponseError):
        adapter.get_inventory()


@pytest.mark.parametrize(
    "bad_item",
    [
        {"addonid": "missing.fields"},
        {key: value for key, value in addon().items() if key != "enabled"},
        addon(enabled="true"),
        addon(addon_id="x" * (MAX_ADDON_ID_LENGTH + 1)),
        addon(name="x" * (MAX_ADDON_NAME_LENGTH + 1)),
        addon(name="bad\x00name"),
    ],
)
def test_rejects_any_malformed_item(bad_item: object) -> None:
    adapter = KodiAddonInventoryAdapter(
        observation(),
        profile(),
        transport=FakeTransport([identity_response(), page([bad_item])]),
    )
    with pytest.raises(KodiAddonInventoryMalformedDataError):
        adapter.get_inventory()


def test_rejects_duplicate_addon_id() -> None:
    adapter = KodiAddonInventoryAdapter(
        observation(),
        profile(),
        transport=FakeTransport([identity_response(), page([addon(), addon()])]),
    )
    with pytest.raises(KodiAddonInventoryMalformedDataError):
        adapter.get_inventory()


def test_rejects_total_beyond_bound_before_collecting_items() -> None:
    adapter = KodiAddonInventoryAdapter(
        observation(),
        profile(),
        transport=FakeTransport([identity_response(), page([], total=2_049)]),
    )
    with pytest.raises(KodiAddonInventoryLimitExceededError):
        adapter.get_inventory()


@pytest.mark.parametrize(
    "error",
    [JsonRpcUnavailableError("timeout"), JsonRpcInvalidResponseError("too large")],
)
def test_transport_timeout_and_response_byte_bound_are_typed(
    error: BaseException,
) -> None:
    adapter = KodiAddonInventoryAdapter(
        observation(), profile(), transport=FakeTransport([identity_response(), error])
    )
    expected = (
        JsonRpcUnavailableError
        if isinstance(error, JsonRpcUnavailableError)
        else KodiAddonInventoryUnsupportedResponseError
    )
    with pytest.raises(expected):
        adapter.get_inventory()


def test_total_deadline_covers_probe_and_inventory() -> None:
    times = iter([0.0, 0.0, 0.0, 6.0])
    adapter = KodiAddonInventoryAdapter(
        observation(),
        profile(),
        clock=lambda: next(times),
        transport=FakeTransport([identity_response(), page([])]),
    )
    with pytest.raises(JsonRpcUnavailableError):
        adapter.get_inventory()


class FakeSocket:
    def settimeout(self, _timeout: float) -> None:
        pass


class FakeHttpResponse:
    def __init__(
        self, status: int, payload: bytes, content_length: str | None = None
    ) -> None:
        self.status = status
        self._payload = payload
        self._content_length = content_length

    def getheader(self, name: str) -> str | None:
        assert name == "Content-Length"
        return self._content_length

    def read(self, amount: int) -> bytes:
        return self._payload[:amount]


class FakeHttpConnection:
    response: FakeHttpResponse

    def __init__(self, _host: str, _port: int, *, timeout: float) -> None:
        assert 0 < timeout <= 3
        self.sock = FakeSocket()

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes,
        headers: dict[str, str],
    ) -> None:
        assert method == "POST"
        assert path == "/jsonrpc"
        assert body
        assert headers == {"Content-Type": "application/json"}

    def getresponse(self) -> FakeHttpResponse:
        return self.response

    def close(self) -> None:
        pass


def test_default_transport_rejects_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeHttpConnection.response = FakeHttpResponse(302, b"")
    monkeypatch.setattr(
        "kodi_lab.kodi.readiness.http.client.HTTPConnection", FakeHttpConnection
    )
    with pytest.raises(JsonRpcUnavailableError):
        KodiAddonInventoryAdapter(observation(), profile()).get_inventory()


def test_default_transport_enforces_64k_response_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    FakeHttpConnection.response = FakeHttpResponse(
        200, b"x" * 65_537, content_length="65537"
    )
    monkeypatch.setattr(
        "kodi_lab.kodi.readiness.http.client.HTTPConnection", FakeHttpConnection
    )
    with pytest.raises(KodiAddonInventoryUnsupportedResponseError):
        KodiAddonInventoryAdapter(observation(), profile()).get_inventory()


def test_enumerates_multiple_pages_as_one_complete_result() -> None:
    first = [addon(f"plugin.example.{index:03d}") for index in range(64)]
    last = [addon("plugin.example.064")]
    adapter = KodiAddonInventoryAdapter(
        observation(),
        profile(),
        transport=FakeTransport(
            [
                identity_response(),
                page(first, start=0, total=65),
                page(last, start=64, total=65),
            ]
        ),
    )
    assert len(adapter.get_inventory().items) == 65
