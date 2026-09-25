"""Installed, exact-artifact bootstrap for the reviewed Linux amd64 runtimes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any
from uuid import uuid5

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from kodi_lab.config import Settings
from kodi_lab.domain import (
    KODI_RUNTIME_VALIDATION_CONTRACT,
    RevisionApprovalState,
    RuntimeContainerArtifact,
    RuntimeDefinition,
    RuntimeExecutionProfile,
    RuntimeRevision,
    RuntimeValidationEvidence,
    RuntimeValidationResult,
)
from kodi_lab.remote_lifecycle import lifecycle_lock
from kodi_lab.remote_lifecycle import validate as remote_validate
from kodi_lab.runtime.curated_catalog import CURATED_SUPPRESSION_REVISIONS
from kodi_lab.runtime.managed_network import ensure_managed_network
from kodi_lab.runtime.manifest import RuntimeBuildManifest
from kodi_lab.storage.engine import create_database_engine, create_session_factory
from kodi_lab.storage.repositories import (
    RuntimeContainerArtifactRepository,
    RuntimeDefinitionRepository,
    RuntimeExecutionProfileRepository,
    RuntimeRevisionRepository,
    RuntimeValidationEvidenceRepository,
)

RUNTIME_NAMES = (
    "kodi-19.5",
    "kodi-20.5",
    "kodi-21.3",
    "kodi-22.0-beta2",
)
_TIERS = ("legacy", "current", "current", "preview")


class CuratedRuntimeError(ValueError):
    """A reviewed identity, artifact, or catalog invariant does not hold."""


@dataclass(frozen=True, slots=True)
class CuratedRuntime:
    name: str
    manifest: RuntimeBuildManifest
    manifest_digest: str
    report: Mapping[str, Any]
    definition: RuntimeDefinition
    revision: RuntimeRevision
    artifact: RuntimeContainerArtifact
    profile: RuntimeExecutionProfile
    evidence: RuntimeValidationEvidence


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise CuratedRuntimeError("validation timestamp is invalid")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def load_curated_runtimes() -> tuple[CuratedRuntime, ...]:
    """Read only wheel-owned manifests and their exact reviewed evidence."""

    package = resources.files("kodi_lab.runtime")
    result: list[CuratedRuntime] = []
    for name, tier, lineage in zip(
        RUNTIME_NAMES, _TIERS, CURATED_SUPPRESSION_REVISIONS, strict=True
    ):
        manifest_bytes = package.joinpath("catalog", f"{name}.json").read_bytes()
        manifest = RuntimeBuildManifest.model_validate_json(manifest_bytes)
        catalog = manifest.catalog
        distribution = manifest.distribution
        if catalog is None or distribution is None or manifest.schema_version != 2:
            raise CuratedRuntimeError("curated manifest is not distributable")
        if (
            manifest.runtime_kind != "kodi"
            or manifest.kodi_version != lineage.canonical_release
            or manifest.build.platform != "linux/amd64"
            or manifest.result.image_id != lineage.image_id
            or catalog.definition_id != lineage.runtime_definition_id
            or catalog.revision_id != lineage.suppression_revision_id
            or catalog.revision_number != lineage.suppression_revision_number
            or catalog.support_tier.value != tier
            or catalog.slug != name
            or catalog.requires_trusted_build_confirmation
            != (name == "kodi-22.0-beta2")
        ):
            raise CuratedRuntimeError("curated manifest differs from reviewed lineage")
        report_bytes = package.joinpath("evidence", f"{name}.json").read_bytes()
        if _sha256(report_bytes) != catalog.validation_report_digest:
            raise CuratedRuntimeError("curated validation evidence digest differs")
        report = json.loads(report_bytes)
        if not isinstance(report, dict):
            raise CuratedRuntimeError("curated validation evidence is invalid")
        cycles = report.get("cycles")
        if (
            report.get("schema") != KODI_RUNTIME_VALIDATION_CONTRACT
            or report.get("result") != "passed"
            or report.get("release") != manifest.kodi_version
            or report.get("image_id") != manifest.result.image_id
            or report.get("runtime_revision_id") != str(catalog.revision_id)
            or report.get("revision_number") != catalog.revision_number
            or report.get("bytecode_policy") != "suppress"
            or report.get("protected_isolation") is not True
            or not isinstance(cycles, list)
            or len(cycles) != 2
            or any(
                not isinstance(cycle, dict)
                or cycle.get("readiness") is not True
                or cycle.get("clean_stop") is not True
                for cycle in cycles
            )
        ):
            raise CuratedRuntimeError("curated validation evidence is inconsistent")
        started_at = _timestamp(cycles[0]["started_at"])
        completed_at = _timestamp(cycles[-1]["completed_at"])
        definition = RuntimeDefinition(
            id=catalog.definition_id,
            slug=catalog.slug,
            kodi_version=manifest.kodi_version,
            support_tier=catalog.support_tier,
        )
        profile = RuntimeExecutionProfile(
            runtime_revision_id=catalog.revision_id,
            **catalog.profile.model_dump(),
        )
        if profile.kodi_release != definition.kodi_release:
            raise CuratedRuntimeError("curated execution profile release differs")
        manifest_digest = _sha256(manifest_bytes)
        revision = RuntimeRevision(
            id=catalog.revision_id,
            runtime_definition_id=catalog.definition_id,
            revision_number=catalog.revision_number,
            source_kind=(
                "reviewed-source-build"
                if manifest.source_build is not None
                else "reviewed-package"
            ),
            source_reference=(
                manifest.source_build.source_archive_url
                if manifest.source_build is not None
                else manifest.package_sources[0]
            ),
            content_digest=manifest.result.image_id,
            manifest_reference=f"kodi-lab-wheel:runtime/catalog/{name}.json",
            manifest_digest=manifest_digest,
            built_at=manifest.result.built_at,
            build_log_reference=manifest.result.build_log_digest,
            requires_trusted_build_confirmation=(
                catalog.requires_trusted_build_confirmation
            ),
        )
        artifact = RuntimeContainerArtifact(
            runtime_revision_id=catalog.revision_id,
            image_id=manifest.result.image_id,
        )
        evidence = RuntimeValidationEvidence(
            id=uuid5(catalog.revision_id, "packaged-kodi-runtime-v1"),
            runtime_revision_id=catalog.revision_id,
            contract_version=KODI_RUNTIME_VALIDATION_CONTRACT,
            result=RuntimeValidationResult.PASSED,
            started_at=started_at,
            completed_at=completed_at,
            recorded_at=completed_at,
            evidence_reference=(
                f"kodi-lab-wheel:runtime/evidence/{name}.json"
                f"#{catalog.validation_report_digest}"
            ),
            summary="Reviewed two-cycle curated runtime validation",
        )
        result.append(
            CuratedRuntime(
                name,
                manifest,
                manifest_digest,
                report,
                definition,
                revision,
                artifact,
                profile,
                evidence,
            )
        )
    return tuple(result)


type ImageInspector = Callable[[str], Mapping[str, object]]


def verify_local_images(
    runtimes: Sequence[CuratedRuntime], inspect_image: ImageInspector
) -> dict[str, str]:
    """Resolve only reviewed descriptor or archive-derived config IDs."""

    resolved: dict[str, str] = {}
    for runtime in runtimes:
        distribution = runtime.manifest.distribution
        assert distribution is not None
        for candidate in (runtime.artifact.image_id, distribution.loaded_image_id):
            try:
                image = inspect_image(candidate)
            except Exception:
                continue
            if (
                image.get("Id") != candidate
                or image.get("Os") != "linux"
                or image.get("Architecture") != "amd64"
            ):
                raise CuratedRuntimeError(
                    f"approved image identity or platform differs for {runtime.name}"
                )
            resolved[runtime.name] = candidate
            break
        else:
            raise CuratedRuntimeError(f"approved image is absent for {runtime.name}")
    return resolved


def verify_archive(runtime: CuratedRuntime, path: Path) -> None:
    """Check exact reviewed export bytes before passing an archive to Docker."""

    expected = runtime.manifest.distribution
    assert expected is not None
    if path.is_symlink() or not path.is_file():
        raise CuratedRuntimeError("runtime archive must be a regular file")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size != expected.archive_size:
        raise CuratedRuntimeError("runtime archive size differs")
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    if "sha256:" + digest.hexdigest() != expected.archive_digest:
        raise CuratedRuntimeError("runtime archive digest differs")
    after = path.stat()
    if (info.st_ino, info.st_dev, info.st_size, info.st_mtime_ns) != (
        after.st_ino,
        after.st_dev,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise CuratedRuntimeError("runtime archive changed during verification")


def load_archive(runtime: CuratedRuntime, path: Path) -> None:
    """Load only a private verified copy, so pathname replacement cannot race load."""

    expected = runtime.manifest.distribution
    assert expected is not None
    with tempfile.TemporaryDirectory(prefix="kodi-lab-runtime-") as directory:
        staged = Path(directory) / "reviewed.tar"
        with path.open("rb") as source, staged.open("xb") as target:
            shutil.copyfileobj(source, target, length=1024 * 1024)
        staged.chmod(0o400)
        verify_archive(runtime, staged)
        completed = subprocess.run(
            ["docker", "image", "load", "--input", str(staged)],
            capture_output=True,
            check=False,
            text=True,
        )
        if completed.returncode != 0:
            raise CuratedRuntimeError("Docker could not load the reviewed archive")


def _require_catalog_record(
    existing: object | None, expected: object, label: str
) -> bool:
    if existing is None:
        return False
    if existing != expected:
        raise CuratedRuntimeError(f"curated catalog {label} conflicts")
    return True


def bootstrap_catalog(
    session_factory: sessionmaker[Session],
    inspect_image: ImageInspector,
) -> int:
    """Atomically seed only exact reviewed rows; repeat is a verified no-op."""

    runtimes = load_curated_runtimes()
    resolved = verify_local_images(runtimes, inspect_image)
    created = 0
    try:
        with session_factory.begin() as session:
            definitions = RuntimeDefinitionRepository(session)
            revisions = RuntimeRevisionRepository(session)
            artifacts = RuntimeContainerArtifactRepository(session)
            profiles = RuntimeExecutionProfileRepository(session)
            evidence_repo = RuntimeValidationEvidenceRepository(session)
            for runtime in runtimes:
                definition = runtime.definition
                revision = runtime.revision
                existing_definition = definitions.get(definition.id)
                if existing_definition is None:
                    if definitions.get_by_slug(definition.slug) is not None:
                        raise CuratedRuntimeError("curated catalog slug conflicts")
                    definitions.create(definition)
                    created += 1
                else:
                    _require_catalog_record(
                        existing_definition, definition, "definition"
                    )
                existing_revision = revisions.get(revision.id)
                if existing_revision is None:
                    if any(
                        item.revision_number == revision.revision_number
                        for item in revisions.list_for_definition(definition.id)
                    ):
                        raise CuratedRuntimeError(
                            "curated catalog revision number conflicts"
                        )
                    revisions.create(revision)
                    revisions.approve(revision.id, approved_at=datetime.now(UTC))
                    created += 1
                else:
                    if (
                        existing_revision.approval_state
                        is not RevisionApprovalState.APPROVED
                        or existing_revision.approval_note is not None
                        or replace(
                            existing_revision,
                            approved_at=None,
                            approval_state=RevisionApprovalState.NOT_APPROVED,
                            trusted_build_confirmed_at=None,
                        )
                        != revision
                    ):
                        raise CuratedRuntimeError("curated catalog revision conflicts")
                expected_artifact = RuntimeContainerArtifact(
                    runtime_revision_id=revision.id,
                    image_id=resolved[runtime.name],
                )
                existing_artifact = artifacts.get(revision.id)
                if existing_artifact is not None and (
                    existing_artifact.image_id != expected_artifact.image_id
                ):
                    allowed = runtime.manifest.distribution
                    assert allowed is not None
                    if existing_artifact.image_id not in (
                        runtime.artifact.image_id,
                        allowed.loaded_image_id,
                    ):
                        raise CuratedRuntimeError("curated catalog artifact conflicts")
                    try:
                        inspected = inspect_image(existing_artifact.image_id)
                    except Exception as error:
                        raise CuratedRuntimeError(
                            "curated catalog artifact is unavailable"
                        ) from error
                    if (
                        inspected.get("Id") != existing_artifact.image_id
                        or inspected.get("Os") != "linux"
                        or inspected.get("Architecture") != "amd64"
                    ):
                        raise CuratedRuntimeError("curated catalog artifact conflicts")
                    expected_artifact = existing_artifact
                if not _require_catalog_record(
                    existing_artifact, expected_artifact, "artifact"
                ):
                    artifacts.create(expected_artifact)
                    created += 1
                if not _require_catalog_record(
                    profiles.get(revision.id), runtime.profile, "profile"
                ):
                    profiles.create(runtime.profile)
                    created += 1
                existing_evidence = evidence_repo.list_for_revision(revision.id)
                if existing_evidence:
                    if existing_evidence != [runtime.evidence]:
                        raise CuratedRuntimeError(
                            "curated catalog validation conflicts"
                        )
                else:
                    evidence_repo.create(runtime.evidence)
                    created += 1
    except IntegrityError as error:
        raise CuratedRuntimeError("curated catalog identity conflicts") from error
    return created


def _docker_inspect(image_id: str) -> Mapping[str, object]:
    import docker  # type: ignore[import-untyped]

    client = docker.from_env()
    try:
        result = client.api.inspect_image(image_id)
        if not isinstance(result, dict):
            raise CuratedRuntimeError("Docker image inspection was invalid")
        return result
    finally:
        client.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kodi-lab-runtime")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="show wheel-owned reviewed identities")
    commands.add_parser("verify", help="verify all exact local Docker images")
    load_command = commands.add_parser("load", help="verify and load one archive")
    load_command.add_argument("name", choices=RUNTIME_NAMES)
    load_command.add_argument("archive", type=Path)
    commands.add_parser("bootstrap", help="verify all images and seed catalog")
    commands.add_parser(
        "network-ensure", help="verify or create the fixed managed network"
    )
    arguments = parser.parse_args(argv)
    try:
        runtimes = load_curated_runtimes()
        if arguments.command == "list":
            print(
                "name release tier revision reviewed_image_id "
                "loaded_image_id archive_sha256"
            )
            for runtime in runtimes:
                distribution = runtime.manifest.distribution
                assert distribution is not None
                print(
                    runtime.name,
                    runtime.manifest.kodi_version,
                    runtime.definition.support_tier.value,
                    runtime.revision.revision_number,
                    runtime.artifact.image_id,
                    distribution.loaded_image_id,
                    distribution.archive_digest,
                )
            return 0
        if arguments.command == "network-ensure":
            import docker

            client = docker.from_env()
            try:
                created = ensure_managed_network(client.api)
            finally:
                client.close()
            print(
                "Managed network created." if created else "Managed network verified."
            )
            return 0
        if arguments.command == "load":
            runtime = next(item for item in runtimes if item.name == arguments.name)
            load_archive(runtime, arguments.archive)
            verify_local_images((runtime,), _docker_inspect)
            print(f"Verified and loaded {runtime.name} by exact Docker image ID.")
            return 0
        if arguments.command == "verify":
            verify_local_images(runtimes, _docker_inspect)
            print("All four reviewed runtime images match their IDs and platform.")
            return 0
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        verified_images: dict[str, Mapping[str, object]] = {}
        for runtime in runtimes:
            distribution = runtime.manifest.distribution
            assert distribution is not None
            for candidate in (
                runtime.artifact.image_id,
                distribution.loaded_image_id,
            ):
                try:
                    verified_images[candidate] = _docker_inspect(candidate)
                except Exception:
                    continue
        verify_local_images(runtimes, verified_images.__getitem__)
        if os.geteuid() == 0:
            owner = settings.data_root.stat()
            if owner.st_uid == 0 or owner.st_uid != owner.st_gid:
                raise CuratedRuntimeError("state root needs the Web numeric owner")
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
                count = bootstrap_catalog(
                    create_session_factory(engine), verified_images.__getitem__
                )
            finally:
                engine.dispose()
        print(f"Reviewed runtime catalog verified; {count} rows created.")
        return 0
    except (CuratedRuntimeError, OSError, RuntimeError, ValueError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    raise SystemExit(main())
