"""Durable installation of a wheel-owned binary-package runtime recipe."""

from __future__ import annotations

import fcntl
import json
import os
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy.orm import Session, sessionmaker

from kodi_lab import __version__
from kodi_lab.application.managed_instances import (
    ExactRevisionSelection,
    ManagedInstanceService,
)
from kodi_lab.application.runtime_eligibility import (
    RuntimeRevisionEligibilityPolicy,
    RuntimeSelectionPolicy,
)
from kodi_lab.config import Settings
from kodi_lab.domain import (
    ConnectionState,
    HealthStatus,
    InstanceType,
    RuntimeContainerArtifact,
    RuntimeDefinition,
    RuntimeExecutionProfile,
    RuntimeRecipeInstallationState,
    RuntimeRevision,
    RuntimeSupportTier,
    RuntimeValidationEvidence,
    RuntimeValidationResult,
    Target,
    TargetAvailability,
    TargetType,
)
from kodi_lab.remote_lifecycle import validate as remote_validate
from kodi_lab.runtime.assembly import AssemblyResult, assemble_reviewed_image
from kodi_lab.runtime.attestation_integrity import (
    canonical_json,
    installed_package_digest,
    seal_attestation,
)
from kodi_lab.runtime.managed_network import ensure_managed_network
from kodi_lab.runtime.recipes import (
    REVIEWED_SIGNED_DISTRO_SOURCE_KIND,
    ReviewedRecipe,
    load_reviewed_recipe,
)
from kodi_lab.runtime.recipes import recipe_target_id as recipe_target_id
from kodi_lab.runtime.temporary_networks import (
    TemporaryNetworkError,
    preflight_temporary_networks,
    reclaim_terminal_orphans,
)
from kodi_lab.runtime.validation import ValidationResult, validate_reviewed_image
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.repositories import (
    ManagedInstanceRepository,
    RuntimeContainerArtifactRepository,
    RuntimeDefinitionRepository,
    RuntimeExecutionProfileRepository,
    RuntimeRecipeInstallationRepository,
    RuntimeRevisionRepository,
    RuntimeValidationEvidenceRepository,
    TargetRepository,
)


class RecipeInstallationError(RuntimeError):
    """The reviewed installation cannot safely proceed or be committed."""


@dataclass(frozen=True, slots=True)
class InstallOutcome:
    operation_id: UUID
    runtime_revision_id: UUID
    image_id: str
    reused: bool


def _canonical_json(value: object, *, maximum: int) -> str:
    serialized = canonical_json(value)
    if len(serialized) > maximum:
        raise RecipeInstallationError("recipe evidence exceeded its bound")
    return serialized


def acquisition_attestation(reviewed: ReviewedRecipe, assembly: AssemblyResult) -> str:
    recipe = reviewed.recipe
    return _canonical_json(
        {
            "schema": "kodi-binary-recipe-acquisition-v1",
            "recipe_id": recipe.recipe_id,
            "recipe_revision": recipe.recipe_revision,
            "recipe_digest": reviewed.digest,
            "generation": recipe.generation,
            "kodi_version": recipe.kodi_version,
            "platform": recipe.platform,
            "base_image_reference": recipe.base_image_reference,
            "provider": recipe.provider.model_dump(),
            "critical_packages": [
                item.model_dump() for item in recipe.critical_packages
            ],
            "installed_packages": assembly.package_inventory,
            "installed_packages_digest": installed_package_digest(
                assembly.package_inventory
            ),
            "signed_metadata_digests": assembly.apt_metadata_digests,
            "image_id": assembly.image_id,
            "image_size": assembly.image_size,
            "assembly_elapsed_seconds": round(assembly.elapsed_seconds, 3),
            "build_log_digest": assembly.build_log_digest,
            "build_log_bytes": assembly.build_log_bytes,
            "apt_index_download": assembly.apt_index_download,
            "apt_package_download": assembly.apt_package_download,
            "kodi_lab_version": __version__,
            "validation_contract": recipe.validation_contract,
            "acquisition_result": "passed",
        },
        maximum=65536,
    )


def _validation_json(result: ValidationResult) -> str:
    return _canonical_json(result.report, maximum=8192)


def _create_operation(
    factory: sessionmaker[Session], reviewed: ReviewedRecipe, operation_id: UUID
) -> dict[str, str]:
    with factory.begin() as session:
        repo = RuntimeRecipeInstallationRepository(session)
        stale = repo.active(reviewed.recipe.generation)
        if stale is not None:
            repo.fail(UUID(stale.id), code="interrupted", now=datetime.now(UTC))
        repo.create(
            operation_id=operation_id,
            recipe_id=reviewed.recipe.recipe_id,
            recipe_revision=reviewed.recipe.recipe_revision,
            recipe_digest=reviewed.digest,
            generation=reviewed.recipe.generation,
            now=datetime.now(UTC),
        )
    return {"operation_id": str(operation_id)}


def _advance(
    factory: sessionmaker[Session],
    operation_id: UUID,
    *,
    expected: RuntimeRecipeInstallationState,
    image_id: str | None = None,
    attestation_json: str | None = None,
) -> dict[str, str]:
    with factory.begin() as session:
        RuntimeRecipeInstallationRepository(session).advance(
            operation_id,
            expected=expected,
            image_id=image_id,
            attestation_json=attestation_json,
            now=datetime.now(UTC),
        )
    return {"state": RuntimeRecipeInstallationRepository._NEXT[expected].value}


def _fail(
    factory: sessionmaker[Session], operation_id: UUID, *, code: str
) -> dict[str, str]:
    with factory.begin() as session:
        RuntimeRecipeInstallationRepository(session).fail(
            operation_id, code=code, now=datetime.now(UTC)
        )
    return {"state": "failed"}


def _installation_failure_code(error: BaseException) -> str:
    current: BaseException | None = error
    for _ in range(5):
        if isinstance(current, TemporaryNetworkError):
            return current.code
        current = current.__cause__ if current is not None else None
    return (
        "validation_failed"
        if type(error).__name__ == "RecipeValidationError"
        else "installation_failed"
    )


def _terminal_operation(factory: sessionmaker[Session], operation_id: UUID) -> bool:
    with factory() as session:
        row = RuntimeRecipeInstallationRepository(session).get(operation_id)
        return row is not None and row.state in (
            RuntimeRecipeInstallationState.READY,
            RuntimeRecipeInstallationState.FAILED,
        )


def _record_early_failure(
    factory: sessionmaker[Session], reviewed: ReviewedRecipe, operation_id: UUID
) -> dict[str, str]:
    """Keep a broker-accepted attempt visible if startup fails before pending."""

    with factory.begin() as session:
        repo = RuntimeRecipeInstallationRepository(session)
        existing = repo.get(operation_id)
        now = datetime.now(UTC)
        if existing is None:
            repo.create_failed_before_start(
                operation_id=operation_id,
                recipe_id=reviewed.recipe.recipe_id,
                recipe_revision=reviewed.recipe.recipe_revision,
                recipe_digest=reviewed.digest,
                generation=reviewed.recipe.generation,
                code="installation_failed",
                now=now,
            )
        elif existing.state not in (
            RuntimeRecipeInstallationState.READY,
            RuntimeRecipeInstallationState.FAILED,
        ):
            repo.fail(operation_id, code="installation_failed", now=now)
    return {"state": "failed"}


@contextmanager
def _record_worker_start_failure(
    settings: Settings, reviewed: ReviewedRecipe, operation_id: UUID | None
) -> Iterator[None]:
    try:
        yield
    except Exception:
        if operation_id is not None:
            with suppress(Exception):
                _database_call(
                    settings,
                    lambda factory: _record_early_failure(
                        factory, reviewed, operation_id
                    ),
                )
        raise


def _ready_installation(
    factory: sessionmaker[Session], reviewed: ReviewedRecipe
) -> dict[str, str]:
    with factory() as session:
        row = RuntimeRecipeInstallationRepository(session).latest_ready(
            reviewed.recipe.generation
        )
        if (
            row is None
            or row.recipe_id != reviewed.recipe.recipe_id
            or row.recipe_revision != reviewed.recipe.recipe_revision
            or row.recipe_digest != reviewed.digest
            or row.runtime_revision_id is None
            or row.image_id is None
        ):
            return {}
        revision = RuntimeRevisionRepository(session).get(UUID(row.runtime_revision_id))
        definition = RuntimeDefinitionRepository(session).get(
            reviewed.recipe.definition_id
        )
        if (
            revision is None
            or definition is None
            or not RuntimeRevisionEligibilityPolicy()
            .evaluate_for_release(
                session,
                required_release=definition.kodi_release,
                definition=definition,
                revision=revision,
                policy=RuntimeSelectionPolicy(
                    include_legacy=True,
                    validation_contract=reviewed.recipe.validation_contract,
                ),
            )
            .eligible
        ):
            return {}
        return {
            "operation_id": row.id,
            "runtime_revision_id": row.runtime_revision_id,
            "image_id": row.image_id,
        }


def _commit_ready(
    factory: sessionmaker[Session],
    reviewed: ReviewedRecipe,
    operation_id: UUID,
    assembly: AssemblyResult,
    validation: ValidationResult,
    *,
    data_root: Path,
) -> dict[str, str]:
    recipe = reviewed.recipe
    attestation = acquisition_attestation(reviewed, assembly)
    validation_data = _validation_json(validation)
    with factory.begin() as session:
        definitions = RuntimeDefinitionRepository(session)
        expected_definition = RuntimeDefinition(
            id=recipe.definition_id,
            slug=recipe.definition_slug,
            kodi_version=recipe.kodi_version,
            support_tier=RuntimeSupportTier.CURRENT,
        )
        existing_definition = definitions.get(recipe.definition_id)
        if existing_definition is None:
            if definitions.get_by_slug(recipe.definition_slug) is not None:
                raise RecipeInstallationError(
                    "reviewed recipe definition slug conflicts"
                )
            definitions.create(expected_definition)
        elif existing_definition != expected_definition:
            raise RecipeInstallationError("reviewed recipe definition conflicts")
        revisions = RuntimeRevisionRepository(session)
        previous = revisions.list_for_definition(recipe.definition_id)
        revision_id = uuid4()
        revision = RuntimeRevision(
            id=revision_id,
            runtime_definition_id=recipe.definition_id,
            revision_number=max((item.revision_number for item in previous), default=0)
            + 1,
            source_kind=REVIEWED_SIGNED_DISTRO_SOURCE_KIND,
            source_reference=(
                f"{recipe.provider.distribution} {recipe.provider.suite} signed archive"
            ),
            content_digest=assembly.image_id,
            manifest_reference=reviewed.reference,
            manifest_digest=reviewed.digest,
            built_at=validation.started_at,
            build_log_reference=(
                f"kodi-lab-db:runtime-recipe-installation/{operation_id}"
                f"#build-log-{assembly.build_log_digest}"
            ),
        )
        revisions.create(revision)
        RuntimeContainerArtifactRepository(session).create(
            RuntimeContainerArtifact(revision_id, assembly.image_id)
        )
        RuntimeExecutionProfileRepository(session).create(
            RuntimeExecutionProfile(
                runtime_revision_id=revision_id,
                **recipe.execution_profile.model_dump(),
            )
        )
        evidence = RuntimeValidationEvidence(
            id=uuid4(),
            runtime_revision_id=revision_id,
            contract_version=recipe.validation_contract,
            result=RuntimeValidationResult.PASSED,
            started_at=validation.started_at,
            completed_at=validation.completed_at,
            recorded_at=validation.completed_at,
            evidence_reference=(
                f"kodi-lab-db:runtime-recipe-installation/{operation_id}#validation"
            ),
            summary="Reviewed package image passed fixed two-cycle validation",
        )
        RuntimeValidationEvidenceRepository(session).create(evidence)
        bound_attestation = seal_attestation(
            attestation,
            validation_data,
            data_root=data_root,
            operation_id=operation_id,
            revision_id=revision_id,
            image_id=assembly.image_id,
            evidence=evidence,
        )
        RuntimeRecipeInstallationRepository(session).ready(
            operation_id,
            revision_id=revision_id,
            image_id=assembly.image_id,
            attestation_json=bound_attestation,
            validation_json=validation_data,
            now=datetime.now(UTC),
        )
        revisions.approve(revision_id, approved_at=datetime.now(UTC))
    return {"runtime_revision_id": str(revision_id)}


def bootstrap_recipe_instance(
    factory: sessionmaker[Session], reviewed: ReviewedRecipe, revision_id: UUID
) -> dict[str, str]:
    """Create a stopped managed pin once; never rebind an existing instance."""

    target_id = recipe_target_id(reviewed)
    expected = Target(
        id=target_id,
        name=f"{reviewed.recipe.display_generation} Lab",
        target_type=TargetType.MANAGED,
        kodi_version=reviewed.recipe.kodi_version,
        availability=TargetAvailability.UNAVAILABLE,
        health=HealthStatus.WARNING,
        connection_state=ConnectionState.DISCONNECTED,
    )
    with factory.begin() as session:
        targets = TargetRepository(session)
        current = targets.get(target_id)
        if current is None:
            if any(item.name == expected.name for item in targets.list()):
                raise RecipeInstallationError(
                    "managed generation target name conflicts"
                )
            targets.create(expected)
        elif (
            current.name != expected.name
            or current.target_type is not TargetType.MANAGED
            or current.kodi_version != expected.kodi_version
        ):
            raise RecipeInstallationError("managed generation target conflicts")
        instance = ManagedInstanceRepository(session).get(target_id)
        if instance is not None:
            pinned = RuntimeRevisionRepository(session).get(
                instance.runtime_revision_id
            )
            if (
                pinned is None
                or pinned.runtime_definition_id != reviewed.recipe.definition_id
            ):
                raise RecipeInstallationError("managed generation pin conflicts")
            return {"target_id": str(target_id), "created": "false"}
    ManagedInstanceService(factory).create(
        target_id=target_id,
        selection=ExactRevisionSelection(reviewed.recipe.definition_id, revision_id),
        instance_type=InstanceType.DISPOSABLE,
    )
    return {"target_id": str(target_id), "created": "true"}


def _database_call(
    settings: Settings, action: Callable[[sessionmaker[Session]], dict[str, str]]
) -> dict[str, str]:
    """Root Docker worker forks a state-owner child for each SQLite transaction."""

    def execute() -> dict[str, str]:
        remote_validate(
            settings.data_root,
            settings.database_url,
            require_head=True,
            require_marker=True,
        )
        engine = create_database_engine(settings.database_url)
        try:
            return action(create_session_factory(engine))
        finally:
            engine.dispose()

    if os.geteuid() != 0:
        return execute()
    owner = settings.data_root.lstat()
    if (
        not stat.S_ISDIR(owner.st_mode)
        or owner.st_uid == 0
        or owner.st_uid != owner.st_gid
        or stat.S_IMODE(owner.st_mode) != 0o700
    ):
        raise RecipeInstallationError("state root owner is unsafe for installation")
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        try:
            os.setgroups([])
            os.setgid(owner.st_gid)
            os.setuid(owner.st_uid)
            output = {"ok": True, "result": execute()}
        except Exception as error:
            output = {"ok": False, "error": type(error).__name__}
        payload = json.dumps(output, separators=(",", ":")).encode()
        os.write(write_fd, payload[:4096])
        os.close(write_fd)
        os._exit(0)
    os.close(write_fd)
    try:
        payload = os.read(read_fd, 4097)
    finally:
        os.close(read_fd)
    _, status = os.waitpid(pid, 0)
    if len(payload) > 4096 or not os.WIFEXITED(status) or os.WEXITSTATUS(status) != 0:
        raise RecipeInstallationError("state-owner transaction did not complete")
    try:
        result = json.loads(payload)
    except ValueError as error:
        raise RecipeInstallationError(
            "state-owner transaction response is invalid"
        ) from error
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise RecipeInstallationError(
            "state-owner transaction failed ("
            + str(result.get("error", "unknown"))
            + ")"
        )
    value = result.get("result")
    if not isinstance(value, dict) or any(
        not isinstance(key, str) or not isinstance(item, str)
        for key, item in value.items()
    ):
        raise RecipeInstallationError("state-owner transaction result is invalid")
    return value


@contextmanager
def _installation_lock(runtime_root: Path) -> Iterator[None]:
    root = runtime_root.lstat()
    if not stat.S_ISDIR(root.st_mode) or stat.S_IMODE(root.st_mode) not in (
        0o700,
        0o750,
    ):
        raise RecipeInstallationError("runtime lock directory is unsafe")
    path = runtime_root / "recipe-install.lock"
    descriptor = os.open(
        path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
    )
    try:
        item = os.fstat(descriptor)
        if (
            not stat.S_ISREG(item.st_mode)
            or item.st_uid != os.geteuid()
            or item.st_nlink != 1
            or stat.S_IMODE(item.st_mode) != 0o600
        ):
            raise RecipeInstallationError("runtime installation lock is unsafe")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RecipeInstallationError(
                "another runtime installation is active"
            ) from error
        yield
    finally:
        os.close(descriptor)


def _exact_existing_image(reviewed: ReviewedRecipe, image_id: str) -> bool:
    import docker  # type: ignore[import-untyped]

    client = docker.from_env(timeout=15)
    try:
        inspected = client.api.inspect_image(image_id)
        config = inspected.get("Config")
        labels = config.get("Labels") if isinstance(config, dict) else None
        return (
            inspected.get("Id") == image_id
            and inspected.get("Os") == "linux"
            and inspected.get("Architecture") == "amd64"
            and isinstance(labels, dict)
            and labels.get("io.kodi-lab.recipe-digest") == reviewed.digest
        )
    except Exception:
        return False
    finally:
        client.close()


def install_reviewed_runtime(
    generation: str,
    *,
    settings: Settings,
    report: Callable[[str], None] = print,
    operation_id: UUID | None = None,
) -> InstallOutcome:
    """Install or ensure one reviewed generation through exact local evidence."""

    reviewed = load_reviewed_recipe(generation)
    with (
        _record_worker_start_failure(settings, reviewed, operation_id),
        _installation_lock(settings.runtime_root),
    ):
        import docker

        network_client = docker.from_env(timeout=30)
        try:
            ensure_managed_network(network_client.api)
        finally:
            network_client.close()
        prior = _database_call(
            settings, lambda factory: _ready_installation(factory, reviewed)
        )
        if prior and _exact_existing_image(reviewed, prior["image_id"]):
            revision_id = UUID(prior["runtime_revision_id"])
            _database_call(
                settings,
                lambda factory: bootstrap_recipe_instance(
                    factory, reviewed, revision_id
                ),
            )
            return InstallOutcome(
                UUID(prior["operation_id"]), revision_id, prior["image_id"], True
            )
        operation_id = operation_id or uuid4()
        _database_call(
            settings,
            lambda factory: _create_operation(factory, reviewed, operation_id),
        )
        try:
            _database_call(
                settings,
                lambda factory: _advance(
                    factory,
                    operation_id,
                    expected=RuntimeRecipeInstallationState.PENDING,
                ),
            )
            preflight_client = docker.from_env(timeout=30)
            try:

                def terminal(prior_id: UUID) -> bool:
                    result = _database_call(
                        settings,
                        lambda factory: {
                            "terminal": (
                                "true"
                                if _terminal_operation(factory, prior_id)
                                else "false"
                            )
                        },
                    )
                    return result["terminal"] == "true"

                reclaim_terminal_orphans(
                    preflight_client,
                    terminal=terminal,
                )
                preflight_temporary_networks(preflight_client, settings)
            finally:
                preflight_client.close()
            report("acquiring")

            def on_base_ready() -> None:
                _database_call(
                    settings,
                    lambda factory: _advance(
                        factory,
                        operation_id,
                        expected=RuntimeRecipeInstallationState.ACQUIRING,
                    ),
                )
                report("assembling")

            assembly = assemble_reviewed_image(
                reviewed,
                operation_id=operation_id,
                on_base_ready=on_base_ready,
                settings=settings,
            )
            attestation = acquisition_attestation(reviewed, assembly)
            _database_call(
                settings,
                lambda factory: _advance(
                    factory,
                    operation_id,
                    expected=RuntimeRecipeInstallationState.ASSEMBLING,
                    image_id=assembly.image_id,
                    attestation_json=attestation,
                ),
            )
            report("validating")
            validation = validate_reviewed_image(
                reviewed,
                image_id=assembly.image_id,
                operation_id=operation_id,
                settings=settings,
            )
            committed = _database_call(
                settings,
                lambda factory: _commit_ready(
                    factory,
                    reviewed,
                    operation_id,
                    assembly,
                    validation,
                    data_root=settings.data_root,
                ),
            )
        except Exception as error:
            code = _installation_failure_code(error)
            with suppress(Exception):
                _database_call(
                    settings,
                    lambda factory: _fail(factory, operation_id, code=code),
                )
            raise RecipeInstallationError(
                f"reviewed {generation} installation failed ({code})"
            ) from error
        revision_id = UUID(committed["runtime_revision_id"])
        _database_call(
            settings,
            lambda factory: bootstrap_recipe_instance(factory, reviewed, revision_id),
        )
        report("ready")
        return InstallOutcome(operation_id, revision_id, assembly.image_id, False)
