"""Actual native coordinator/JSON-RPC adapter with an in-memory HTTP transport."""

import json
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.create_kodi_readiness import CreateKodiReadinessService
from kodi_lab.application.runtime_installations import RuntimeInstallationService
from kodi_lab.broker.docker_inventory import KodiProcessState
from kodi_lab.broker.operations import ObserveManagedKodiRuntimeResult
from kodi_lab.domain import RuntimeExecutionProfile
from kodi_lab.kodi.readiness import (
    JsonRpcUnavailableError,
    KodiJsonRpcReadinessAdapter,
    KodiReadinessCoordinator,
)
from kodi_lab.storage.create_kodi import CreateKodiRepository
from tests.create_kodi_start_support import Broker as StartBroker
from tests.kodi.test_readiness import FakeClock


class Transport:
    def __init__(self) -> None:
        self.clock = FakeClock()
        self.methods: list[str] = []
        self.mode = "ready"
        self.native_version: dict[str, int | str] = {
            "major": 21,
            "minor": 3,
            "tag": "stable",
        }
        self.on_request: Callable[[], None] | None = None

    def post(
        self, *, host: str, port: int, path: str, body: bytes, timeout: float
    ) -> bytes:
        assert (host, port, path) == ("10.250.0.10", 8080, "/jsonrpc")
        assert 0 < timeout <= 3
        request = json.loads(body)
        method = request["method"]
        assert method in {"JSONRPC.Ping", "Application.GetProperties"}
        self.methods.append(method)
        if self.on_request:
            self.on_request()
        if self.mode == "unreachable":
            raise JsonRpcUnavailableError("fake unreachable")
        if self.mode == "invalid":
            return b"not JSON"
        result: Any = "pong"
        if method == "Application.GetProperties":
            assert request["params"] == {"properties": ["name", "version"]}
            result = {
                "name": "Other" if self.mode == "name" else "Kodi",
                "version": {
                    **self.native_version,
                    **({"major": 20} if self.mode == "version" else {}),
                },
            }
        return json.dumps(
            {"jsonrpc": "2.0", "id": request["id"], "result": result}
        ).encode()


class Broker(StartBroker):
    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        self.observations = 0
        self.process_state = KodiProcessState.RUNNING
        self.starting_observations = 0
        self.observation_patch: dict[str, Any] = {}

    def observe_managed_kodi_runtime(
        self, *, managed_instance_id: str, expected_container_id: str
    ) -> ObserveManagedKodiRuntimeResult:
        self.observations += 1
        observed = super().observe_managed_kodi_runtime(
            managed_instance_id=managed_instance_id,
            expected_container_id=expected_container_id,
        )
        return observed.model_copy(
            update={
                "process_state": KodiProcessState.STARTING
                if self.observations <= self.starting_observations
                else self.process_state,
                **self.observation_patch,
            }
        )

    def start_managed_container(self, **_kwargs: Any) -> Any:
        raise AssertionError("readiness must never Start")

    def stop_managed_container(self, **_kwargs: Any) -> Any:
        raise AssertionError("readiness must never Stop")

    def provision_managed_container(self, **_kwargs: Any) -> Any:
        raise AssertionError("readiness must never provision")

    def deprovision_managed_container(self, **_kwargs: Any) -> Any:
        raise AssertionError("readiness must never replace")

    def launch_reviewed_runtime_installation(self, **_kwargs: Any) -> Any:
        raise AssertionError("readiness must never install")


def service(
    sessions: sessionmaker[Session],
    repo: CreateKodiRepository,
    broker: Broker,
    transport: Transport,
) -> CreateKodiReadinessService:
    def coordinator(
        client: Any, profile: RuntimeExecutionProfile
    ) -> KodiReadinessCoordinator:
        return KodiReadinessCoordinator(
            client,
            profile,
            clock=transport.clock,
            sleep=transport.clock.sleep,
            adapter_factory=lambda observed, profile, timeout: (
                KodiJsonRpcReadinessAdapter(
                    observed, profile, timeout=timeout, transport=transport
                )
            ),
        )

    return CreateKodiReadinessService(
        sessions,
        repo,
        RuntimeInstallationService(sessions, broker),
        broker,
        coordinator_factory=coordinator,
    )
