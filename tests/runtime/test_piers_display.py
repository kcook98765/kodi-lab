"""Piers display packaging preserves RC1 trust and explicit operation consent."""

import hashlib
from dataclasses import asdict, replace
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.runtime_installations import (
    RuntimeInstallationActionConflict,
    RuntimeInstallationService,
    preview_acknowledgement,
)
from kodi_lab.broker.novnc_service import HumanDisplayService
from kodi_lab.config import Settings
from kodi_lab.domain import RuntimeRecipeInstallationState
from kodi_lab.runtime.display_capabilities import (
    installed_display_capability,
    reviewed_display_capability,
)
from kodi_lab.runtime.install import _advance, _commit_ready, _create_operation
from kodi_lab.runtime.ostree_assembly import (
    assembly_assets_digest,
    load_assembly_assets,
)
from kodi_lab.runtime.ostree_installation import OstreeInstallationAssembly
from kodi_lab.runtime.preview import PreviewAcknowledgement, recorded_acknowledgement
from kodi_lab.runtime.recipes import (
    ReviewedRecipe,
    load_display_candidate_recipe,
    load_installation_recipe,
)
from kodi_lab.runtime.ubuntu_support import load_support_closure
from kodi_lab.storage.repositories import (
    ManagedInstanceRepository,
    RuntimeRecipeInstallationRepository,
)
from tests.application.test_piers_installation_admission import broker
from tests.runtime import test_ostree_installation as fixture
from tests.runtime.test_display_candidate import proof

factory = fixture.factory


def test_exact_r3_preserves_rc1_and_all_97_support_pins() -> None:
    old, new = load_installation_recipe("piers"), load_display_candidate_recipe("piers")
    assert old.recipe.recipe_revision == 2 and new.recipe.recipe_revision == 3
    assert old.recipe.provider == new.recipe.provider
    assert old.recipe.base_image_reference == new.recipe.base_image_reference
    before, after = load_support_closure(), load_support_closure(revision=3)
    by_name = {p.name: p for p in after.packages}
    assert all(asdict(by_name[p.name]) == asdict(p) for p in before.packages)
    assert len(before.packages) == 97 and len(after.packages) == 116
    assert sum(p.size for p in after.packages) == 78022102
    assert before.base_inventory == after.base_inventory
    assert by_name["perl-base"].version == "5.40.1-7ubuntu0.3"
    capability = reviewed_display_capability(new)
    assert capability is not None and capability.qualification == "candidate"
    assert capability.package == (
        "tigervnc-scraping-server",
        "1.15.0+dfsg-2build1",
        "amd64",
    )
    old_assets, assets = load_assembly_assets(), load_assembly_assets(revision=3)
    assert {n for n in old_assets if old_assets[n] != assets[n]} == {
        "supervisor.py",
        "Dockerfile",
    }
    assert b'"640x360x24"' in assets["supervisor.py"]
    assert b"LP_NUM_THREADS=1" in assets["Dockerfile"]
    assert b"chmod 0444 /usr/local/lib/kodi-lab-novnc-view.py" in assets["Dockerfile"]
    assert reviewed_display_capability(old) is None
    assert reviewed_display_capability(replace(new, recipe=old.recipe)) is None


@pytest.mark.parametrize("field", ["recipe_id", "recipe_revision", "recipe_digest"])
def test_mixed_preview_consent_never_selects_r3(field: str) -> None:
    old, new = (
        preview_acknowledgement(),
        preview_acknowledgement(display_candidate=True),
    )
    old[field] = new[field]
    with pytest.raises(ValidationError):
        PreviewAcknowledgement.model_validate(old)


def test_r3_admission_is_explicit_operation_bound_and_not_default(
    factory: sessionmaker[Session],
) -> None:
    service = RuntimeInstallationService(factory, broker())
    ack = PreviewAcknowledgement.model_validate(
        preview_acknowledgement(display_candidate=True)
    )
    with pytest.raises(RuntimeInstallationActionConflict):
        service.start_preview(development_mode=True, acknowledgement=ack)
    result = service.start_preview(
        development_mode=True, acknowledgement=ack, display_candidate=True
    )
    operation = UUID(result.operation_id)
    assert recorded_acknowledgement(factory, operation) == ack
    with factory.begin() as db:
        row = RuntimeRecipeInstallationRepository(db).get(operation)
        assert row is not None and row.recipe_revision == 3
        row.recipe_revision = 2
    with pytest.raises(ValueError, match="operation recipe differs"):
        recorded_acknowledgement(factory, operation)


def ready_display(
    factory: sessionmaker[Session],
) -> tuple[UUID, ReviewedRecipe, OstreeInstallationAssembly, UUID]:
    reviewed = load_display_candidate_recipe("piers")
    operation = uuid4()
    _create_operation(factory, reviewed, operation)
    original = fixture.assembly(operation)
    assets = load_assembly_assets(revision=3)
    built = replace(
        original,
        assembly=replace(
            original.assembly,
            recipe_revision=3,
            supporting_closure_digest=load_support_closure(revision=3).digest,
            assembly_assets_digest="sha256:" + assembly_assets_digest(revision=3),
            asset_hashes=tuple(
                sorted(
                    (name, hashlib.sha256(data).hexdigest())
                    for name, data in assets.items()
                )
            ),
        ),
        acknowledgement=PreviewAcknowledgement.model_validate(
            preview_acknowledgement(display_candidate=True)
        ),
    )
    result = fixture.validation(built.image_id)
    result = replace(
        result,
        report={
            **result.report,
            "recipe_digest": reviewed.digest,
            "display": {**proof(), "process_probe": "piers-loader"},
        },
    )
    for state in (
        RuntimeRecipeInstallationState.PENDING,
        RuntimeRecipeInstallationState.ACQUIRING,
        RuntimeRecipeInstallationState.ASSEMBLING,
    ):
        _advance(factory, operation, expected=state)
    _commit_ready(
        factory, reviewed, operation, built, result, data_root=Settings().data_root
    )
    with factory() as db:
        row = RuntimeRecipeInstallationRepository(db).get(operation)
        assert row is not None and row.runtime_revision_id is not None
        revision = UUID(row.runtime_revision_id)
    return operation, reviewed, built, revision


def test_r3_sealed_proof_and_preview_creation_remain_separate_from_viewer(
    factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operation, reviewed, _built, revision = ready_display(factory)
    with factory() as db:
        capability = installed_display_capability(db, revision)
        assert capability == reviewed_display_capability(reviewed)
        assert ManagedInstanceRepository(db).list() == []
    backend = broker()
    from types import SimpleNamespace

    backend.reviewed_runtime_image_present = lambda _: SimpleNamespace(present=True)
    service = RuntimeInstallationService(factory, backend)
    candidate_view = service.get(operation)
    assert candidate_view is not None
    assert candidate_view.preview_acknowledgement == preview_acknowledgement(
        display_candidate=True
    )
    assert (
        service._view(
            "piers", None, capable=True, display_candidate=True
        ).preview_acknowledgement
        == candidate_view.preview_acknowledgement
    )
    assert (
        service._view("piers", None, capable=True).preview_acknowledgement
        == preview_acknowledgement()
    )
    roundtrip_ack = PreviewAcknowledgement.model_validate(
        candidate_view.preview_acknowledgement
    )
    created = service.create_preview_instance(
        development_mode=True,
        acknowledgement=roundtrip_ack,
        display_candidate=True,
    )
    assert created.action == "open"
    with factory() as db:
        instance = ManagedInstanceRepository(db).list()[0]
        assert instance.runtime_revision_id == revision
    monkeypatch.setattr(
        "docker.APIClient",
        lambda **kw: pytest.fail("candidate must not start a viewer"),
    )
    with pytest.raises(ValueError, match="not qualified"):
        HumanDisplayService(
            Settings(experimental_novnc_instances=(instance.target_id,)), factory
        ).resolve(instance.target_id)


def test_old_assembly_cannot_be_relabelled_as_display_candidate() -> None:
    from kodi_lab.runtime.install import acquisition_attestation

    reviewed = load_display_candidate_recipe("piers")
    borrowed = replace(
        fixture.assembly(uuid4()),
        acknowledgement=PreviewAcknowledgement.model_validate(
            preview_acknowledgement(display_candidate=True)
        ),
    )
    with pytest.raises(ValueError, match="provenance differs"):
        acquisition_attestation(reviewed, borrowed)


def test_cli_candidate_cannot_borrow_ordinary_preview_consent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    from kodi_lab.runtime.install import (
        RecipeInstallationError,
        install_reviewed_runtime,
    )

    monkeypatch.setattr(
        "kodi_lab.runtime.install._database_call",
        lambda *args: {"ack": json.dumps(preview_acknowledgement())},
    )
    monkeypatch.setattr(
        "docker.from_env",
        lambda **kwargs: pytest.fail("mismatched consent must precede Docker"),
    )
    with pytest.raises(RecipeInstallationError, match="candidate consent differs"):
        install_reviewed_runtime(
            "piers", settings=Settings(), operation_id=uuid4(), display_candidate=True
        )


@pytest.mark.parametrize(
    "change",
    [
        None,
        "uid",
        "bool_uid",
        "loader",
        "library_path",
        "binary",
        "flags",
        "extra",
        "substring",
    ],
)
def test_loader_process_match_refuses_substitution(change: str | None) -> None:
    from kodi_lab.runtime.display_validation import _PIERS_PROCESS_SOURCE

    namespace: dict[str, object] = {}
    exec(_PIERS_PROCESS_SOURCE, namespace)
    args = [
        "/opt/fdo/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2",
        "--library-path",
        "/app/lib:/opt/mesa/lib:/opt/fdo/lib/x86_64-linux-gnu:/opt/fdo/lib/x86_64-linux-gnu/pulseaudio:/opt/fdo/lib",
        "/app/lib/kodi/kodi.bin",
        "--standalone",
        "--windowing=x11",
        "--audio-backend=alsa",
    ]
    executable = args[0]
    uid: object = 1000
    if change == "uid":
        uid = 0
    if change == "bool_uid":
        uid = True
    if change == "loader":
        executable = "/usr/bin/unreviewed-loader"
    if change == "library_path":
        args[2] += ":/tmp/substituted"
    if change == "binary":
        args[3] = "/tmp/kodi.bin"
    if change == "flags":
        args.remove("--windowing=x11")
    if change == "extra":
        args.append("--extra")
    if change == "substring":
        args = ["/bin/echo", *args]
    from typing import Any, cast

    match = cast(Any, namespace["matches_piers_process"])
    assert match(("\0".join(args) + "\0").encode(), executable, uid) is (change is None)


def test_piers_cannot_borrow_ordinary_process_proof() -> None:
    from kodi_lab.runtime.display_validation import display_validation_matches

    capability = reviewed_display_capability(load_display_candidate_recipe("piers"))
    assert capability is not None
    assert not display_validation_matches(proof(), capability)
    assert display_validation_matches(
        {**proof(), "process_probe": "piers-loader"}, capability
    )
