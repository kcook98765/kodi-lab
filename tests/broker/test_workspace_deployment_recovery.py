"""The fixed stopped verifier reads complete bytes and never applies an archive."""

from dataclasses import replace
from pathlib import Path

import pytest

from kodi_lab.broker.docker_inventory import DockerManagedContainerAdapter
from kodi_lab.broker.errors import (
    DockerDevelopmentSourceDeploymentError,
    ManagedRuntimeNotRunningError,
)
from tests.broker import test_development_source_deployment as fixture


@pytest.mark.parametrize("prior", [False, True])
def test_stopped_inspection_checks_whole_tree_without_writes(
    tmp_path: Path, prior: bool
) -> None:
    docker = fixture.FakeDeploymentDockerClient()
    deployed = fixture._apply(docker, fixture._spec(tmp_path))
    docker.inspection["State"] = {"Status": "exited", "Running": False, "Pid": 0}
    before = list(docker.put_members)
    spec = replace(
        fixture._verification_spec(deployed.verified_destination_digest),
        require_stopped=True,
    )
    if prior:
        spec = replace(
            spec,
            verified_destination_digest="sha256:" + "f" * 64,
            alternate_destination_digest=deployed.verified_destination_digest,
        )
    proof = DockerManagedContainerAdapter(
        lambda: docker
    ).verify_attached_development_source_destination(
        spec=spec, expected_container_id=fixture.CONTAINER_ID
    )
    assert proof.verified_destination_digest == deployed.verified_destination_digest
    assert docker.put_members == before


def test_unrecognized_or_partially_copied_tree_is_not_containment_proof(
    tmp_path: Path,
) -> None:
    docker = fixture.FakeDeploymentDockerClient()
    fixture._apply(docker, fixture._spec(tmp_path))
    docker.inspection["State"] = {"Status": "exited", "Running": False, "Pid": 0}
    spec = replace(
        fixture._verification_spec("sha256:" + "f" * 64), require_stopped=True
    )
    with pytest.raises(
        DockerDevelopmentSourceDeploymentError, match="do not match retained evidence"
    ):
        DockerManagedContainerAdapter(
            lambda: docker
        ).verify_attached_development_source_destination(
            spec=spec, expected_container_id=fixture.CONTAINER_ID
        )


def test_running_process_is_not_stopped_containment(tmp_path: Path) -> None:
    docker = fixture.FakeDeploymentDockerClient()
    deployed = fixture._apply(docker, fixture._spec(tmp_path))
    docker.inspection["State"] = {"Status": "exited", "Running": True, "Pid": 12}
    spec = replace(
        fixture._verification_spec(deployed.verified_destination_digest),
        require_stopped=True,
    )
    with pytest.raises(ManagedRuntimeNotRunningError):
        DockerManagedContainerAdapter(
            lambda: docker
        ).verify_attached_development_source_destination(
            spec=spec, expected_container_id=fixture.CONTAINER_ID
        )


def test_requested_destination_digest_includes_fixed_runtime_ownership(
    tmp_path: Path,
) -> None:
    from kodi_lab.development.deployment import (
        capture_development_tree,
        expected_destination_digest,
    )

    docker = fixture.FakeDeploymentDockerClient()
    spec = fixture._spec(tmp_path)
    source = capture_development_tree(spec.development_root, spec.relative_locator)
    deployed = fixture._apply(docker, spec)
    expected = expected_destination_digest(
        source, runtime_uid=spec.runtime_uid, runtime_gid=spec.runtime_gid
    )
    assert expected == deployed.verified_destination_digest
    assert expected != source.digest
    assert (
        expected_destination_digest(
            source, runtime_uid=spec.runtime_uid + 1, runtime_gid=spec.runtime_gid
        )
        != expected
    )
