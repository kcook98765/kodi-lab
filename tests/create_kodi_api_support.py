"""Real Web authentication, composition and Create stages over fake transports."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import UUID

from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.create_kodi_readiness import CreateKodiReadinessService
from kodi_lab.auth.service import WebAuthenticationService
from kodi_lab.broker.docker_inventory import (
    ContainerHealthState,
    ContainerRuntimeState,
    ManagedContainerRecord,
)
from kodi_lab.broker.operations import EstablishManagedBridgeTrustResult
from kodi_lab.config import Settings
from kodi_lab.domain import RuntimeExecutionProfile
from kodi_lab.kodi.readiness import (
    KodiJsonRpcReadinessAdapter,
    KodiReadinessCoordinator,
)
from kodi_lab.storage.create_kodi import CreateKodiRepository
from kodi_lab.web.app import create_app
from tests.application.test_managed_bridge_trust import Client
from tests.create_kodi_readiness_support import Transport
from tests.create_kodi_runner_support import Broker


class WebEnvironment:
    def __init__(
        self,
        sessions: sessionmaker[Session],
        data_root: Path,
        *,
        operation_id: UUID | None = None,
    ) -> None:
        self.repo = CreateKodiRepository(sessions)
        self.broker = Broker(sessions, self.repo)
        self.transport = Transport()
        if operation_id:
            op = self.repo.get(operation_id)
            assert op
            self.broker.parent = op.id
            if op.container_id:
                self.broker.records = [
                    ManagedContainerRecord(
                        str(op.target_id),
                        op.container_id,
                        str(op.image_id),
                        ContainerRuntimeState.STOPPED,
                        ContainerHealthState.HEALTHY,
                        str(op.runtime_revision_id),
                    )
                ]

        async def no_network(*_args: Any, **_kwargs: Any) -> str:
            return "not_checked"

        # Only the native transport/clock are substituted. The real coordinator,
        # adapter verification and Web service composition still execute.
        def coordinator(
            broker: Any, profile: RuntimeExecutionProfile
        ) -> KodiReadinessCoordinator:
            return KodiReadinessCoordinator(
                broker,
                profile,
                clock=self.transport.clock,
                sleep=self.transport.clock.sleep,
                adapter_factory=lambda observed, profile, timeout: (
                    KodiJsonRpcReadinessAdapter(
                        observed, profile, timeout=timeout, transport=self.transport
                    )
                ),
            )

        def native(*args: Any, **kwargs: Any) -> CreateKodiReadinessService:
            return CreateKodiReadinessService(
                *args, **kwargs, coordinator_factory=coordinator
            )

        with (
            patch("kodi_lab.web.app.CreateKodiReadinessService", side_effect=native),
            patch("kodi_lab.repository.address.probe_advertised_origin", no_network),
        ):
            self.app = create_app(
                settings=Settings(
                    data_root=data_root,
                    development_root=data_root.parent / "development-root",
                    external_target_master_key=SecretStr("d3" * 21 + "c"),
                ),
                session_factory=sessions,
                broker=self.broker,
                auth=WebAuthenticationService(sessions),
                coordinator_factory=coordinator,
            )
        # Every outbound Bridge/native transport is isolated as well. Create
        # now exercises real trust admission after its native preparation.
        from dataclasses import replace

        from tests.application.test_managed_bridge_preparation import Native

        self.bridge_native = Native(self, "1.1.31")  # type: ignore[arg-type]
        self.bridge_client = Client()
        self.app.state.kodi_lab.managed_bridge_trust.close()
        self.app.state.kodi_lab.managed_bridge_trust.client = self.bridge_client
        self.app.state.kodi_lab.managed_bridge_trust.transport = self.bridge_native
        original_dispatch = self.broker.dispatch

        def dispatch(target: str) -> Any:
            result = original_dispatch(target)
            self.broker.records = [
                replace(record, health_state=ContainerHealthState.HEALTHY)
                for record in self.broker.records
            ]
            return result

        def establish(**kwargs: Any) -> EstablishManagedBridgeTrustResult:
            if kwargs.get("install_reviewed"):
                self.bridge_native.installed = True
            return EstablishManagedBridgeTrustResult(
                managed_instance_id=kwargs["managed_instance_id"],
                container_id=kwargs["expected_container_id"],
                generation=kwargs["generation"],
                bridge_id=str(self.bridge_client.value.bridge_id),
            )

        self.broker.dispatch = dispatch  # type: ignore[method-assign]
        self.broker.establish_managed_bridge_trust = establish  # type: ignore[attr-defined]

    @contextmanager
    def client(self) -> Iterator[TestClient]:
        with TestClient(self.app, base_url="https://127.0.0.1") as client:
            yield client
