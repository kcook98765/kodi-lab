from collections.abc import Mapping
from uuid import UUID

import pytest

from kodi_lab.broker.docker_inventory import (
    INSTANCE_ID_LABEL_KEY,
    MANAGED_LABEL_KEY,
    MANAGED_LABEL_VALUE,
    DockerManagedContainerAdapter,
    KodiProcessState,
)
from kodi_lab.broker.errors import (
    ContainerIdentityPreconditionError,
    InvalidManagedContainerMetadataError,
    ManagedRuntimeEndpointError,
    ManagedRuntimeNotRunningError,
    RuntimeRevisionPreconditionError,
)
from kodi_lab.broker.provisioning import (
    MANAGED_NETWORK_NAME,
    RUNTIME_REVISION_LABEL_KEY,
    ManagedRuntimeObservationSpec,
)
from kodi_lab.domain import (
    ProcessObservationKind,
    ReadinessProbeKind,
    RuntimeExecutionProfile,
    RuntimeKind,
)

INSTANCE_ID = str(UUID(int=1))
REVISION_ID = UUID(int=2)
CONTAINER_ID = f"{3:064x}"


def profile() -> RuntimeExecutionProfile:
    return RuntimeExecutionProfile(
        runtime_revision_id=REVISION_ID,
        runtime_kind=RuntimeKind.KODI,
        runtime_uid=1000,
        runtime_gid=1000,
        expected_application_name="Kodi",
        expected_kodi_version="21.3",
        expected_version_tag="stable",
        jsonrpc_port=8080,
        jsonrpc_path="/jsonrpc",
        process_observation=ProcessObservationKind.DOCKER_HEALTHCHECK,
        readiness_probe=ReadinessProbeKind.KODI_JSONRPC,
        readiness_timeout_seconds=90,
        poll_interval_seconds=1.0,
        http_timeout_seconds=3.0,
    )


def inspect_data(
    *,
    state: str = "running",
    health: object = "healthy",
    networks: object | None = None,
    labels: object | None = None,
) -> dict[str, object]:
    state_data: dict[str, object] = {"Status": state}
    if health is not None:
        state_data["Health"] = {"Status": health}
    return {
        "Id": CONTAINER_ID,
        "Image": "sha256:" + ("a" * 64),
        "Config": {
            "Labels": labels
            if labels is not None
            else {
                MANAGED_LABEL_KEY: MANAGED_LABEL_VALUE,
                INSTANCE_ID_LABEL_KEY: INSTANCE_ID,
                RUNTIME_REVISION_LABEL_KEY: str(REVISION_ID),
            }
        },
        "State": state_data,
        "NetworkSettings": {
            "Networks": networks
            if networks is not None
            else {MANAGED_NETWORK_NAME: {"IPAddress": "10.250.0.17"}}
        },
    }


class FakeDockerClient:
    def __init__(self, inspection: object) -> None:
        self.inspection = inspection
        self.filters: Mapping[str, list[str]] | None = None
        self.closed = False

    def containers(
        self, *, all: bool, filters: Mapping[str, list[str]]
    ) -> list[object]:
        assert all is True
        self.filters = filters
        return [{"Id": CONTAINER_ID}]

    def inspect_container(self, container: str) -> object:
        assert container == CONTAINER_ID
        return self.inspection

    def start(self, container: str) -> None:
        raise AssertionError("observation attempted start")

    def stop(self, container: str, *, timeout: int) -> None:
        raise AssertionError("observation attempted stop")

    def close(self) -> None:
        self.closed = True


def observe(inspection: object, *, expected_id: str = CONTAINER_ID):  # type: ignore[no-untyped-def]
    client = FakeDockerClient(inspection)
    result = DockerManagedContainerAdapter(lambda: client).observe_managed_kodi_runtime(
        spec=ManagedRuntimeObservationSpec(
            managed_instance_id=INSTANCE_ID,
            runtime_revision_id=str(REVISION_ID),
            profile=profile(),
        ),
        expected_container_id=expected_id,
    )
    return result, client


@pytest.mark.parametrize(
    ("health", "expected"),
    [
        ("healthy", KodiProcessState.RUNNING),
        ("unhealthy", KodiProcessState.NOT_RUNNING),
        ("starting", KodiProcessState.STARTING),
        (None, KodiProcessState.UNKNOWN_ERROR),
        ("future", KodiProcessState.UNKNOWN_ERROR),
    ],
)
def test_observation_maps_only_docker_health_to_process_state(
    health: object, expected: KodiProcessState
) -> None:
    result, client = observe(inspect_data(health=health))
    assert result.process_state is expected
    assert result.ipv4_address == "10.250.0.17"
    assert result.jsonrpc_port == 8080
    assert client.filters == {
        "label": [
            f"{MANAGED_LABEL_KEY}={MANAGED_LABEL_VALUE}",
            f"{INSTANCE_ID_LABEL_KEY}={INSTANCE_ID}",
        ]
    }
    assert client.closed


@pytest.mark.parametrize("malformed_health", [{}, {"Status": 3}, "healthy"])
def test_malformed_required_health_is_unknown_error(
    malformed_health: object,
) -> None:
    inspection = inspect_data()
    state = inspection["State"]
    assert isinstance(state, dict)
    state["Health"] = malformed_health
    result, _client = observe(inspection)
    assert result.process_state is KodiProcessState.UNKNOWN_ERROR


def test_stopped_container_has_no_endpoint_observation() -> None:
    with pytest.raises(ManagedRuntimeNotRunningError):
        observe(inspect_data(state="exited"))


@pytest.mark.parametrize(
    "networks",
    [
        {},
        {MANAGED_NETWORK_NAME: {"IPAddress": ""}},
        {MANAGED_NETWORK_NAME: {"IPAddress": "172.27.0.2"}},
        {
            MANAGED_NETWORK_NAME: {"IPAddress": "10.250.0.17"},
            "legacy": {"IPAddress": "172.27.0.2"},
        },
    ],
)
def test_wrong_absent_or_ambiguous_network_endpoint_is_rejected(
    networks: object,
) -> None:
    with pytest.raises(ManagedRuntimeEndpointError):
        observe(inspect_data(networks=networks))


def test_stale_container_identity_is_rejected() -> None:
    with pytest.raises(ContainerIdentityPreconditionError):
        observe(inspect_data(), expected_id="f" * 64)


def test_unowned_and_wrong_revision_containers_are_rejected() -> None:
    unowned = {
        MANAGED_LABEL_KEY: "false",
        INSTANCE_ID_LABEL_KEY: INSTANCE_ID,
        RUNTIME_REVISION_LABEL_KEY: str(REVISION_ID),
    }
    with pytest.raises(InvalidManagedContainerMetadataError):
        observe(inspect_data(labels=unowned))
    wrong_revision = {
        MANAGED_LABEL_KEY: MANAGED_LABEL_VALUE,
        INSTANCE_ID_LABEL_KEY: INSTANCE_ID,
        RUNTIME_REVISION_LABEL_KEY: str(UUID(int=99)),
    }
    with pytest.raises(RuntimeRevisionPreconditionError):
        observe(inspect_data(labels=wrong_revision))
