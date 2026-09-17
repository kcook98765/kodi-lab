from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.readiness_tasks import ReadinessTaskRegistry
from kodi_lab.auth.service import WebAuthenticationService
from kodi_lab.config import Settings
from kodi_lab.domain import (
    KODI_RUNTIME_VALIDATION_CONTRACT,
    ConnectionState,
    HealthStatus,
    ProcessObservationKind,
    ReadinessProbeKind,
    RevisionApprovalState,
    RuntimeContainerArtifact,
    RuntimeDefinition,
    RuntimeExecutionProfile,
    RuntimeKind,
    RuntimeRevision,
    RuntimeSupportTier,
    RuntimeValidationEvidence,
    RuntimeValidationResult,
    Target,
    TargetAvailability,
    TargetType,
)
from kodi_lab.storage.repositories import (
    RuntimeContainerArtifactRepository,
    RuntimeDefinitionRepository,
    RuntimeExecutionProfileRepository,
    RuntimeRevisionRepository,
    RuntimeValidationEvidenceRepository,
    TargetRepository,
)
from kodi_lab.web.app import create_app

from .conftest import (
    DEFINITION_ID,
    IMAGE_ID,
    INSTANCE_ID,
    PASSWORD,
    REVISION_ID,
    FakeBroker,
    coordinator_factory,
    login,
)

NOW = datetime(2026, 9, 15, 21, 0, tzinfo=UTC)
MATRIX_IDS = {
    "19.5": (
        UUID("00000000-0000-4000-8000-000000000191"),
        UUID("00000000-0000-4000-8000-000000000192"),
        UUID("00000000-0000-4000-8000-000000000193"),
    ),
    "20.5": (
        UUID("00000000-0000-4000-8000-000000000201"),
        UUID("00000000-0000-4000-8000-000000000202"),
        UUID("00000000-0000-4000-8000-000000000203"),
    ),
    "22.0-beta.2": (
        UUID("00000000-0000-4000-8000-000000000221"),
        UUID("00000000-0000-4000-8000-000000000222"),
        UUID("00000000-0000-4000-8000-000000000223"),
    ),
}


def seed_web_matrix(factory: sessionmaker[Session]) -> None:
    with factory.begin() as session:
        existing_revision = RuntimeRevisionRepository(session).get(REVISION_ID)
        assert existing_revision is not None
        RuntimeContainerArtifactRepository(session).create(
            RuntimeContainerArtifact(
                runtime_revision_id=REVISION_ID,
                image_id=IMAGE_ID,
            )
        )
        add_evidence(session, existing_revision)
        add_runtime(session, "19.5", RuntimeSupportTier.LEGACY)
        add_runtime(session, "20.5", RuntimeSupportTier.CURRENT)
        add_runtime(
            session,
            "22.0-beta.2",
            RuntimeSupportTier.PREVIEW,
            requires_trust=True,
        )


def add_runtime(
    session: Session,
    version: str,
    tier: RuntimeSupportTier,
    *,
    requires_trust: bool = False,
) -> None:
    definition_id, revision_id, target_id = MATRIX_IDS[version]
    definition = RuntimeDefinition(
        id=definition_id,
        slug=f"web-kodi-{version.replace('.', '-')}",
        kodi_version=version,
        support_tier=tier,
    )
    revision = RuntimeRevision(
        id=revision_id,
        runtime_definition_id=definition_id,
        revision_number=1,
        source_kind="test-source",
        source_reference=f"test://{version}",
        content_digest=revision_id.hex * 2,
        manifest_reference=f"runtime/{version}/manifest.json",
        manifest_digest="d" * 64,
        built_at=NOW,
        approval_state=RevisionApprovalState.APPROVED,
        approved_at=NOW,
        requires_trusted_build_confirmation=requires_trust,
        trusted_build_confirmed_at=NOW if requires_trust else None,
    )
    RuntimeDefinitionRepository(session).create(definition)
    RuntimeRevisionRepository(session).create(revision)
    RuntimeContainerArtifactRepository(session).create(
        RuntimeContainerArtifact(
            runtime_revision_id=revision_id,
            image_id="sha256:" + revision_id.hex * 2,
        )
    )
    release = definition.kodi_release
    RuntimeExecutionProfileRepository(session).create(
        RuntimeExecutionProfile(
            runtime_revision_id=revision_id,
            runtime_kind=RuntimeKind.KODI,
            runtime_uid=1000,
            runtime_gid=1000,
            expected_application_name="Kodi",
            expected_kodi_version=f"{release.major}.{release.minor}",
            expected_version_tag=release.channel.value,
            expected_prerelease_ordinal=release.prerelease_ordinal,
            jsonrpc_port=8080,
            jsonrpc_path="/jsonrpc",
            process_observation=ProcessObservationKind.DOCKER_HEALTHCHECK,
            readiness_probe=ReadinessProbeKind.KODI_JSONRPC,
            readiness_timeout_seconds=90,
            poll_interval_seconds=1,
            http_timeout_seconds=3,
        )
    )
    add_evidence(session, revision)
    TargetRepository(session).create(
        Target(
            id=target_id,
            name=f"Kodi {version} target",
            target_type=TargetType.MANAGED,
            kodi_version=version,
            availability=TargetAvailability.UNAVAILABLE,
            health=HealthStatus.WARNING,
            connection_state=ConnectionState.DISCONNECTED,
        )
    )


def add_evidence(session: Session, revision: RuntimeRevision) -> None:
    RuntimeValidationEvidenceRepository(session).create(
        RuntimeValidationEvidence(
            id=UUID(int=revision.id.int + 1),
            runtime_revision_id=revision.id,
            contract_version=KODI_RUNTIME_VALIDATION_CONTRACT,
            result=RuntimeValidationResult.PASSED,
            started_at=NOW,
            completed_at=NOW,
            evidence_reference=f"test://evidence/{revision.id}",
            recorded_at=NOW,
        )
    )


@contextmanager
def matrix_client(
    factory: sessionmaker[Session], *, trusted_custom_builds_enabled: bool = False
) -> Iterator[TestClient]:
    auth = WebAuthenticationService(factory)
    auth.set_password(PASSWORD)
    app = create_app(
        settings=Settings(trusted_custom_builds_enabled=trusted_custom_builds_enabled),
        session_factory=factory,
        broker=FakeBroker(),
        readiness=ReadinessTaskRegistry(max_workers=1),
        auth=auth,
        coordinator_factory=coordinator_factory,
    )
    with TestClient(app, base_url="http://127.0.0.1") as client:
        yield client


def by_release(document: dict[str, object]) -> dict[str, dict[str, object]]:
    runtimes = document["runtimes"]
    assert isinstance(runtimes, list)
    return {item["canonical_release"]: item for item in runtimes}


def test_matrix_routes_require_authentication(
    database: sessionmaker[Session],
) -> None:
    seed_web_matrix(database)
    with matrix_client(database) as client:
        html = client.get("/runtimes", follow_redirects=False)
        assert html.status_code == 303
        assert html.headers["location"] == "/login"
        assert client.get("/api/runtime-matrix").status_code == 401
        assert (
            client.get(
                f"/api/targets/{MATRIX_IDS['20.5'][2]}/runtime-selection"
            ).status_code
            == 401
        )


def test_matrix_api_policy_combinations_and_safe_serialization(
    database: sessionmaker[Session],
) -> None:
    seed_web_matrix(database)
    with matrix_client(database) as client:
        login(client)
        normal = by_release(client.get("/api/runtime-matrix").json())
        assert {key: item["selectable"] for key, item in normal.items()} == {
            "19.5": False,
            "20.5": True,
            "21.3": True,
            "22.0-beta.2": False,
        }
        legacy = by_release(
            client.get("/api/runtime-matrix?include_legacy=true").json()
        )
        assert legacy["19.5"]["selectable"] is True
        prerelease = by_release(
            client.get("/api/runtime-matrix?include_prerelease=true").json()
        )
        assert prerelease["22.0-beta.2"]["selectable"] is False
        assert prerelease["22.0-beta.2"]["ineligibility_reason"] == (
            "trusted_custom_builds_disabled"
        )
        serialized = str(normal)
        for forbidden in (
            "image_id",
            "artifact",
            "evidence_reference",
            "source_reference",
            "manifest",
        ):
            assert forbidden not in serialized


def test_trusted_build_setting_is_not_browser_authority(
    database: sessionmaker[Session],
) -> None:
    seed_web_matrix(database)
    injected_query = (
        "/api/runtime-matrix?include_prerelease=true"
        "&trusted_custom_builds_enabled=true"
        "&validation_result=passed&support_tier=current"
        "&runtime_revision_id=00000000-0000-4000-8000-000000000222"
        "&image_id=sha256:attacker"
    )
    with matrix_client(database) as client:
        login(client)
        beta = by_release(client.get(injected_query).json())["22.0-beta.2"]
        assert beta["selectable"] is False
        assert beta["ineligibility_reason"] == "trusted_custom_builds_disabled"

    with matrix_client(database, trusted_custom_builds_enabled=True) as client:
        login(client)
        both = by_release(
            client.get(
                "/api/runtime-matrix?include_legacy=true&include_prerelease=true"
            ).json()
        )
        assert all(item["selectable"] for item in both.values())


def test_matrix_query_booleans_are_strict(database: sessionmaker[Session]) -> None:
    seed_web_matrix(database)
    with matrix_client(database) as client:
        login(client)
        for query in (
            "include_legacy=1",
            "include_legacy=yes",
            "include_prerelease=True",
            "include_prerelease=on",
        ):
            assert client.get(f"/api/runtime-matrix?{query}").status_code == 422
            assert client.get(f"/runtimes?{query}").status_code == 422


def test_matrix_html_is_explicit_and_omits_backend_authority(
    database: sessionmaker[Session],
) -> None:
    seed_web_matrix(database)
    with matrix_client(database) as client:
        login(client)
        page = client.get("/runtimes")
        assert page.status_code == 200
        for text in (
            "Kodi 19.5",
            "Kodi 20.5",
            "Kodi 21.3",
            "Kodi 22.0 Beta 2",
            "Current",
            "Legacy",
            "Preview",
            "Validated",
            "Include Legacy",
            "Include Pre-release",
            "Trusted custom builds are disabled",
        ):
            assert text in page.text
        for forbidden in (IMAGE_ID, "evidence_reference", "source_reference"):
            assert forbidden not in page.text
        assert "Curated runtime matrix" in client.get("/").text


def test_target_selection_preview_is_exact_and_server_resolved(
    database: sessionmaker[Session],
) -> None:
    seed_web_matrix(database)
    with matrix_client(database, trusted_custom_builds_enabled=True) as client:
        login(client)
        target_19 = MATRIX_IDS["19.5"][2]
        normal_19 = client.get(f"/api/targets/{target_19}/runtime-selection").json()
        assert normal_19["selectable"] is False
        selected_19 = client.get(
            f"/api/targets/{target_19}/runtime-selection?include_legacy=true"
        ).json()
        assert selected_19["selectable"] is True
        assert selected_19["required_canonical_release"] == "19.5"
        assert selected_19["runtime_definition_id"] == str(MATRIX_IDS["19.5"][0])
        assert selected_19["selected_revision_number"] == 1

        for version in ("20.5", "22.0-beta.2"):
            target_id = MATRIX_IDS[version][2]
            suffix = "?include_prerelease=true" if "beta" in version else ""
            selected = client.get(
                f"/api/targets/{target_id}/runtime-selection{suffix}"
            ).json()
            assert selected["selectable"] is True
            assert selected["required_canonical_release"] == version
            assert selected["runtime_definition_id"] == str(MATRIX_IDS[version][0])

        selected_21 = client.get(f"/api/targets/{INSTANCE_ID}/runtime-selection").json()
        assert selected_21["required_canonical_release"] == "21.3"
        assert selected_21["runtime_definition_id"] == str(DEFINITION_ID)
        assert selected_21["selected_revision_number"] == 2
        assert "runtime_revision_id" not in selected_21
        assert "image_id" not in selected_21

        with database.begin() as session:
            beta1 = Target(
                id=UUID("00000000-0000-4000-8000-000000000211"),
                name="Kodi 22 Beta 1 target",
                target_type=TargetType.MANAGED,
                kodi_version="22.0-beta.1",
                availability=TargetAvailability.UNAVAILABLE,
                health=HealthStatus.WARNING,
                connection_state=ConnectionState.DISCONNECTED,
            )
            TargetRepository(session).create(beta1)
        unresolved = client.get(
            f"/api/targets/{beta1.id}/runtime-selection?include_prerelease=true"
        ).json()
        assert unresolved["selectable"] is False
        assert unresolved["runtime_definition_id"] is None
        assert unresolved["ineligibility_reason"] == ("runtime_definition_unavailable")


def test_unknown_target_selection_is_bounded(
    database: sessionmaker[Session],
) -> None:
    seed_web_matrix(database)
    with matrix_client(database) as client:
        login(client)
        response = client.get(
            "/api/targets/ffffffff-ffff-4fff-8fff-ffffffffffff/runtime-selection"
        )
        assert response.status_code == 404
        assert response.json() == {
            "error": {"code": "target_not_found", "message": "Target not found."}
        }


def test_matrix_web_surface_has_no_catalog_or_evidence_mutation(
    database: sessionmaker[Session],
) -> None:
    seed_web_matrix(database)
    with matrix_client(database) as client:
        login(client)
        assert client.post("/api/runtime-matrix", json={}).status_code == 405
        assert (
            client.post(
                f"/api/targets/{MATRIX_IDS['20.5'][2]}/runtime-selection",
                json={"runtime_revision_id": str(MATRIX_IDS["20.5"][1])},
            ).status_code
            == 405
        )
        for route in (
            "/api/runtime-validation-evidence",
            "/api/runtime-definitions",
            "/api/runtime-revisions",
            "/api/runtime-builds",
        ):
            assert client.post(route, json={}).status_code == 404
