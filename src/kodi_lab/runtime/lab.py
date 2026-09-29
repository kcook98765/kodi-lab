"""Installed Linux lab workflow over canonical managed instances and the broker."""

from __future__ import annotations

import argparse
import os
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from uuid import UUID, uuid5

from docker.errors import DockerException, NotFound  # type: ignore[import-untyped]
from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.application.errors import ApplicationServiceError
from kodi_lab.application.fixtures import FixtureAdministrationService
from kodi_lab.application.managed_instances import (
    ExactRevisionSelection,
    ManagedInstanceService,
)
from kodi_lab.application.readiness_tasks import ReadinessTaskRegistry
from kodi_lab.application.runtime_catalog import RuntimeCatalogAdministrationService
from kodi_lab.application.runtime_lifecycle import ManagedRuntimeLifecycleService
from kodi_lab.application.runtime_status import (
    ManagedRuntimeStatusService,
    ProvisionState,
)
from kodi_lab.broker.client import BrokerClient
from kodi_lab.broker.docker_inventory import ContainerRuntimeState
from kodi_lab.broker.errors import ClientError
from kodi_lab.config import Settings, safe_settings_error
from kodi_lab.domain import (
    ConnectionState,
    HealthStatus,
    InstanceType,
    ManagedInstance,
    Target,
    TargetAvailability,
    TargetType,
)
from kodi_lab.fixtures.package import TRUSTED_FIXTURE_SLUGS, load_packaged_fixture
from kodi_lab.host_bootstrap import HostBootstrapError
from kodi_lab.kodi.readiness import KodiReadinessCoordinator, UsabilityState
from kodi_lab.remote_lifecycle import lifecycle_lock
from kodi_lab.remote_lifecycle import validate as remote_validate
from kodi_lab.runtime.distribution import (
    CuratedRuntime,
    CuratedRuntimeError,
    bootstrap_catalog,
    load_curated_runtimes,
    verify_local_images,
)
from kodi_lab.runtime.install import recipe_target_id
from kodi_lab.runtime.managed_network import ensure_managed_network
from kodi_lab.runtime.recipes import load_reviewed_recipe, supported_generations
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.repositories import (
    ManagedInstanceRepository,
    RuntimeExecutionProfileRepository,
    RuntimeRecipeInstallationRepository,
    RuntimeRevisionRepository,
    TargetRepository,
)

_NAMESPACE = UUID("dc264ebb-d7c8-4ea7-8d54-77d5e7624e09")
SELECTORS = ("19", "20", "21", "22")
KNOWN_SELECTORS = SELECTORS + supported_generations()
_NAMES = ("Kodi 19.5 Lab", "Kodi 20.5 Lab", "Kodi 21.3 Lab", "Kodi 22 Beta 2 Lab")
_ACKNOWLEDGED_PREVIEW_RUNTIME = "kodi-22.0-beta2"


class LabWorkflowError(ValueError):
    """A canonical lab prerequisite or definition differs."""


def canonical_id(selector: str) -> UUID:
    if selector in supported_generations():
        return recipe_target_id(load_reviewed_recipe(selector))
    if selector not in SELECTORS:
        raise LabWorkflowError(f"unknown lab runtime selector: {selector}")
    return uuid5(_NAMESPACE, f"managed-kodi-{selector}")


def select_runtimes(selectors: Sequence[str]) -> tuple[str, ...]:
    if not selectors:
        return SELECTORS
    unknown = set(selectors) - set(KNOWN_SELECTORS)
    if unknown:
        raise LabWorkflowError(f"unknown lab runtime selector: {sorted(unknown)[0]}")
    return tuple(selector for selector in KNOWN_SELECTORS if selector in selectors)


def _expected_target(selector: str, runtime: CuratedRuntime) -> Target:
    return Target(
        id=canonical_id(selector),
        name=_NAMES[SELECTORS.index(selector)],
        target_type=TargetType.MANAGED,
        kodi_version=runtime.manifest.kodi_version,
        availability=TargetAvailability.UNAVAILABLE,
        health=HealthStatus.WARNING,
        connection_state=ConnectionState.DISCONNECTED,
    )


def _require_canonical(
    factory: sessionmaker[Session], runtimes: Sequence[CuratedRuntime]
) -> None:
    """Reject every conflict before filling any missing canonical row."""

    with factory() as session:
        targets = TargetRepository(session)
        instances = ManagedInstanceRepository(session)
        all_targets = targets.list()
        for selector, runtime in zip(SELECTORS, runtimes, strict=True):
            expected = _expected_target(selector, runtime)
            current = targets.get(expected.id)
            if current is not None and current != expected:
                raise LabWorkflowError(f"{selector}: canonical lab target conflicts")
            if current is None and any(
                target.name == expected.name for target in all_targets
            ):
                raise LabWorkflowError(f"{selector}: canonical lab name conflicts")
            instance = instances.get(expected.id)
            if instance is not None and instance != ManagedInstance(
                target_id=expected.id,
                instance_type=InstanceType.DISPOSABLE,
                runtime_revision_id=runtime.revision.id,
                runtime_state=instance.runtime_state,
                start_on_host_boot=False,
                notes=None,
                fixture_definition_id=instance.fixture_definition_id,
            ):
                raise LabWorkflowError(f"{selector}: canonical lab instance conflicts")
            if instance is not None and current is None:
                raise LabWorkflowError(f"{selector}: canonical lab target is absent")


def bootstrap_instances(
    factory: sessionmaker[Session],
    *,
    acknowledge_preview: bool,
    report: Callable[[str], None] | None = None,
) -> list[str]:
    """Create absent managed pins through the existing selection service."""

    runtimes = load_curated_runtimes()
    if not acknowledge_preview:
        raise LabWorkflowError(
            "Kodi 22 Preview requires --ack-preview-kodi22 "
            "and the Trusted Custom Build opt-in"
        )
    _require_canonical(factory, runtimes)
    preview = next(
        (
            runtime
            for runtime in runtimes
            if runtime.name == _ACKNOWLEDGED_PREVIEW_RUNTIME
        ),
        None,
    )
    if preview is None or not preview.revision.requires_trusted_build_confirmation:
        raise LabWorkflowError("acknowledged Kodi 22 Preview revision is unavailable")
    RuntimeCatalogAdministrationService(factory).confirm_trusted_custom_build(
        preview.revision.id, confirmed_at=datetime.now(UTC)
    )
    service = ManagedInstanceService(
        factory,
        trusted_custom_builds_enabled=True,
        include_legacy=True,
        include_prerelease=True,
    )
    results: list[str] = []
    for selector, runtime in zip(SELECTORS, runtimes, strict=True):
        expected = _expected_target(selector, runtime)
        with factory.begin() as session:
            targets = TargetRepository(session)
            if targets.get(expected.id) is None:
                targets.create(expected)
        with factory() as session:
            existing = ManagedInstanceRepository(session).get(expected.id)
        if existing is None:
            try:
                service.create(
                    target_id=expected.id,
                    selection=ExactRevisionSelection(
                        runtime.definition.id, runtime.revision.id
                    ),
                    instance_type=InstanceType.DISPOSABLE,
                )
            except ApplicationServiceError as error:
                kind = type(error).__name__
                raise LabWorkflowError(
                    f"{selector}: managed instance creation failed ({kind})"
                ) from error
            result = f"{selector}: created {expected.id}"
        else:
            result = f"{selector}: verified {expected.id}"
        results.append(result)
        if report is not None:
            report(result)
    return results


def _bootstrap(settings: Settings, *, acknowledge_preview: bool) -> None:
    if not acknowledge_preview:
        raise LabWorkflowError("Kodi 22 Preview requires --ack-preview-kodi22")
    if not settings.trusted_custom_builds_enabled:
        raise LabWorkflowError(
            "Trusted Custom Build opt-in is required for this bootstrap invocation"
        )
    if (
        not (settings.data_root / ".state.json").is_file()
        or not (settings.data_root / "database" / "kodi-lab.db").is_file()
    ):
        raise LabWorkflowError("initialized application state is required")
    runtimes = load_curated_runtimes()
    import docker  # type: ignore[import-untyped]

    client = docker.from_env()
    verified_images: dict[str, dict[str, object]] = {}
    try:
        client.ping()
        for runtime in runtimes:
            distribution = runtime.manifest.distribution
            assert distribution is not None
            for candidate in (
                runtime.artifact.image_id,
                distribution.loaded_image_id,
            ):
                try:
                    verified_images[candidate] = dict(
                        client.api.inspect_image(candidate)
                    )
                except NotFound:
                    continue
        verify_local_images(runtimes, verified_images.__getitem__)
        created = ensure_managed_network(client.api)
    finally:
        client.close()
    print("managed network: " + ("created" if created else "verified"), flush=True)
    if os.geteuid() == 0:
        owner = settings.data_root.stat()
        if owner.st_uid == 0 or owner.st_uid != owner.st_gid:
            raise LabWorkflowError("state root needs the Web numeric owner")
        os.setgroups([])
        os.setgid(owner.st_gid)
        os.setuid(owner.st_uid)
    with lifecycle_lock(settings.data_root):
        remote_validate(
            settings.data_root,
            settings.database_url,
            require_head=True,
            require_marker=True,
        )
        engine = create_database_engine(settings.database_url)
        try:
            factory = create_session_factory(engine)
            count = bootstrap_catalog(factory, verified_images.__getitem__)
            print(f"curated catalog: verified ({count} rows created)", flush=True)
            bootstrap_instances(
                factory,
                acknowledge_preview=acknowledge_preview,
                report=lambda result: print(result, flush=True),
            )
        finally:
            engine.dispose()


def _verify_records(factory: sessionmaker[Session]) -> None:
    _require_canonical(factory, load_curated_runtimes())
    with factory() as session:
        instances = ManagedInstanceRepository(session)
        if any(instances.get(canonical_id(selector)) is None for selector in SELECTORS):
            raise LabWorkflowError("lab bootstrap is incomplete; rerun bootstrap")


def _verify_recipe_record(factory: sessionmaker[Session], generation: str) -> None:
    reviewed = load_reviewed_recipe(generation)
    target_id = recipe_target_id(reviewed)
    with factory() as session:
        target = TargetRepository(session).get(target_id)
        instance = ManagedInstanceRepository(session).get(target_id)
        if target is None or instance is None:
            raise LabWorkflowError(f"{generation}: reviewed runtime is not installed")
        revision = RuntimeRevisionRepository(session).get(instance.runtime_revision_id)
        if (
            target.kodi_version != reviewed.recipe.kodi_version
            or revision is None
            or revision.runtime_definition_id != reviewed.recipe.definition_id
        ):
            raise LabWorkflowError(f"{generation}: managed recipe pin conflicts")


def assign_packaged_fixture(
    factory: sessionmaker[Session], selector: str, slug: str
) -> str:
    """Assign a compatible wheel-owned fixture without direct database work."""

    if selector in supported_generations():
        _verify_recipe_record(factory, selector)
        release = load_reviewed_recipe(selector).recipe.kodi_version
    else:
        _verify_records(factory)
        release = load_curated_runtimes()[
            SELECTORS.index(selector)
        ].manifest.kodi_version
    fixture = load_packaged_fixture(slug).definition
    if fixture.compatible_kodi_version != release:
        raise LabWorkflowError(f"{slug} is incompatible with Kodi {release}")
    service = FixtureAdministrationService(factory)
    try:
        registered = service.register_packaged_fixture(slug)
        service.assign(
            managed_instance_id=canonical_id(selector),
            fixture_definition_id=registered.id,
        )
    except ApplicationServiceError as error:
        raise LabWorkflowError(
            f"fixture assignment failed ({type(error).__name__})"
        ) from error
    return f"{selector}: fixture {slug} assigned"


def run_selected(chosen: Sequence[str], action: Callable[[str], str]) -> int:
    """Report every independent runtime result and return failure on any error."""

    failed = False
    for selector in chosen:
        try:
            result = action(selector)
        except Exception as error:
            failed = True
            reason = (
                str(error)[:160]
                if isinstance(error, LabWorkflowError)
                else type(error).__name__
            )
            print(f"{selector}: failed ({reason})", flush=True)
        else:
            print(f"{selector}: {result}", flush=True)
    return 1 if failed else 0


def _run_control(settings: Settings, command: str, selectors: Sequence[str]) -> int:
    remote_validate(
        settings.data_root,
        settings.database_url,
        require_head=True,
        require_marker=True,
    )
    engine = create_database_engine(settings.database_url)
    readiness = ReadinessTaskRegistry()
    try:
        factory = create_session_factory(engine)
        release_bundle = os.environ.get("KODI_LAB_RELEASE_BUNDLE") == "1"
        if selectors:
            chosen = select_runtimes(selectors)
        else:
            with factory() as session:
                installed_generations = tuple(
                    generation
                    for generation in supported_generations()
                    if ManagedInstanceRepository(session).get(canonical_id(generation))
                    is not None
                )
            chosen = (
                supported_generations()
                if release_bundle
                else installed_generations or SELECTORS
            )
        if release_bundle and any(selector in SELECTORS for selector in chosen):
            raise LabWorkflowError("private archive selectors are unavailable")
        if any(selector in SELECTORS for selector in chosen):
            _verify_records(factory)
        with factory() as session:
            missing_generations = {
                selector
                for selector in chosen
                if selector in supported_generations()
                and ManagedInstanceRepository(session).get(canonical_id(selector))
                is None
            }
        for selector in chosen:
            if (
                selector in supported_generations()
                and selector not in missing_generations
            ):
                _verify_recipe_record(factory, selector)
        broker = BrokerClient()
        try:
            broker.ping()
        except ClientError:

            def unavailable(_selector: str) -> str:
                raise LabWorkflowError("broker unavailable")

            return run_selected(chosen, unavailable)
        status_service = ManagedRuntimeStatusService(factory, broker, readiness)
        lifecycle = ManagedRuntimeLifecycleService(
            factory, broker, readiness, status_service
        )

        def perform(selector: str) -> str:
            if selector in missing_generations:
                if command == "status":
                    return "supported unacquired"
                raise LabWorkflowError("reviewed runtime is not installed")
            instance_id = canonical_id(selector)
            status = status_service.get(instance_id)
            if command == "start":
                if status.provision_state is ProvisionState.UNPROVISIONED:
                    status = lifecycle.provision(instance_id)
                if status.container_state is ContainerRuntimeState.STOPPED:
                    lifecycle.start(instance_id)
                elif status.container_state is not ContainerRuntimeState.RUNNING:
                    raise LabWorkflowError("runtime state is ambiguous")
                with factory() as session:
                    instance = ManagedInstanceRepository(session).get(instance_id)
                    assert instance is not None
                    profile = RuntimeExecutionProfileRepository(session).get(
                        instance.runtime_revision_id
                    )
                    assert profile is not None
                result = KodiReadinessCoordinator(broker, profile).wait_until_usable(
                    str(instance_id)
                )
                if result.usability_state is not UsabilityState.USABLE:
                    raise LabWorkflowError(
                        f"readiness failed: {result.failure_reason or 'unknown'}"
                    )
                identity = (
                    result.identity.kodi_release if result.identity else "unknown"
                )
                return f"ready {identity}"
            if command == "stop":
                if status.container_state is ContainerRuntimeState.RUNNING:
                    lifecycle.stop(instance_id)
                    return "stopped"
                if status.provision_state is ProvisionState.UNPROVISIONED:
                    return "unprovisioned"
                if status.container_state is ContainerRuntimeState.STOPPED:
                    return "already stopped"
                raise LabWorkflowError("runtime state is ambiguous")
            readiness_state = status.readiness_state.value
            if status.container_state is ContainerRuntimeState.RUNNING:
                with factory() as session:
                    instance = ManagedInstanceRepository(session).get(instance_id)
                    assert instance is not None
                    profile = RuntimeExecutionProfileRepository(session).get(
                        instance.runtime_revision_id
                    )
                    assert profile is not None
                observed = KodiReadinessCoordinator(broker, profile).wait_until_usable(
                    str(instance_id)
                )
                if observed.usability_state is not UsabilityState.USABLE:
                    raise LabWorkflowError(
                        f"readiness failed: {observed.failure_reason or 'unknown'}"
                    )
                readiness_state = "usable"
            return (
                f"{status.provision_state.value} {status.container_state.value} "
                f"revision={status.runtime_revision_number} "
                f"readiness={readiness_state}"
            )

        return run_selected(chosen, perform)
    finally:
        readiness.close()
        engine.dispose()


def _run_fixture_assignment(settings: Settings, selector: str, slug: str) -> None:
    remote_validate(
        settings.data_root,
        settings.database_url,
        require_head=True,
        require_marker=True,
    )
    engine = create_database_engine(settings.database_url)
    try:
        print(assign_packaged_fixture(create_session_factory(engine), selector, slug))
    finally:
        engine.dispose()


def repin_reviewed_runtime(
    factory: sessionmaker[Session], generation: str, revision_id: UUID
) -> UUID:
    """Explicitly select the latest ready recipe revision for an existing pin."""

    reviewed = load_reviewed_recipe(generation)
    target_id = recipe_target_id(reviewed)
    with factory() as session:
        installed = RuntimeRecipeInstallationRepository(session).latest_ready(
            generation
        )
        current = ManagedInstanceRepository(session).get(target_id)
        if (
            installed is None
            or installed.recipe_id != reviewed.recipe.recipe_id
            or installed.recipe_revision != reviewed.recipe.recipe_revision
            or installed.recipe_digest != reviewed.digest
            or installed.runtime_revision_id != str(revision_id)
            or current is None
        ):
            raise LabWorkflowError("exact ready recipe revision is unavailable")
        if current.runtime_revision_id == revision_id:
            raise LabWorkflowError("revision is already pinned")
    try:
        ManagedInstanceService(factory).reassign_runtime(
            target_id=target_id,
            selection=ExactRevisionSelection(
                reviewed.recipe.definition_id, revision_id
            ),
        )
    except ApplicationServiceError as error:
        raise LabWorkflowError(
            f"reviewed revision selection failed ({type(error).__name__})"
        ) from error
    return revision_id


def _run_repin(settings: Settings, generation: str, revision_id: UUID) -> None:
    with lifecycle_lock(settings.data_root):
        remote_validate(
            settings.data_root,
            settings.database_url,
            require_head=True,
            require_marker=True,
        )
        engine = create_database_engine(settings.database_url)
        readiness = ReadinessTaskRegistry()
        try:
            factory = create_session_factory(engine)
            _verify_recipe_record(factory, generation)
            broker = BrokerClient()
            try:
                broker.ping()
                status = ManagedRuntimeStatusService(factory, broker, readiness).get(
                    canonical_id(generation)
                )
            except ClientError as error:
                raise LabWorkflowError("broker unavailable") from error
            if (
                status.provision_state is not ProvisionState.UNPROVISIONED
                or status.container_state is not ContainerRuntimeState.STOPPED
            ):
                raise LabWorkflowError("stop and deprovision before repinning")
            repin_reviewed_runtime(factory, generation, revision_id)
        finally:
            readiness.close()
            engine.dispose()
    print(f"{generation}: explicitly pinned revision={revision_id}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kodi-lab-lab")
    commands = parser.add_subparsers(dest="command", required=True)
    bootstrap = commands.add_parser("bootstrap")
    bootstrap.add_argument("--ack-preview-kodi22", action="store_true")
    fixture = commands.add_parser("fixture")
    fixture_commands = fixture.add_subparsers(dest="fixture_command", required=True)
    assign = fixture_commands.add_parser("assign")
    assign.add_argument("selector", choices=KNOWN_SELECTORS)
    assign.add_argument("slug", choices=sorted(TRUSTED_FIXTURE_SLUGS))
    repin = commands.add_parser(
        "repin", help="select an exact reacquired revision after recovery"
    )
    repin.add_argument("generation", choices=supported_generations())
    repin.add_argument("revision_id", type=UUID)
    for name in ("start", "stop", "status"):
        command = commands.add_parser(name)
        command.add_argument("selectors", nargs="*")
    arguments = parser.parse_args(argv)
    try:
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        if arguments.command == "bootstrap":
            _bootstrap(settings, acknowledge_preview=arguments.ack_preview_kodi22)
            return 0
        if arguments.command == "fixture":
            _run_fixture_assignment(settings, arguments.selector, arguments.slug)
            return 0
        if arguments.command == "repin":
            _run_repin(settings, arguments.generation, arguments.revision_id)
            return 0
        return _run_control(settings, arguments.command, arguments.selectors)
    except ValidationError as error:
        parser.error(safe_settings_error(error))
    except DockerException:
        parser.error(
            "Docker Engine is unavailable; check the broker docker.sock mount "
            "and daemon access"
        )
    except (
        CuratedRuntimeError,
        HostBootstrapError,
        LabWorkflowError,
        OSError,
        RuntimeError,
        ValueError,
    ) as error:
        parser.error(str(error))


if __name__ == "__main__":
    raise SystemExit(main())
