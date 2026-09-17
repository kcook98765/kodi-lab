"""Repository-owned identities for additive curated suppression revisions."""

from dataclasses import dataclass, replace
from uuid import UUID

from kodi_lab.domain import (
    PythonBytecodePolicy,
    RevisionApprovalState,
    RuntimeContainerArtifact,
    RuntimeExecutionProfile,
    RuntimeRevision,
)


@dataclass(frozen=True, slots=True)
class CuratedSuppressionRevision:
    """Exact retained lineage and new identity for one curated runtime."""

    canonical_release: str
    runtime_definition_id: UUID
    predecessor_revision_id: UUID
    predecessor_revision_number: int
    suppression_revision_id: UUID
    suppression_revision_number: int
    image_id: str

    def __post_init__(self) -> None:
        if self.suppression_revision_number != self.predecessor_revision_number + 1:
            raise ValueError("suppression revision must be the next revision number")


CURATED_SUPPRESSION_REVISIONS = (
    CuratedSuppressionRevision(
        canonical_release="19.5",
        runtime_definition_id=UUID("51950000-0000-4000-8000-000000000001"),
        predecessor_revision_id=UUID("51950000-0000-4000-8000-000000000002"),
        predecessor_revision_number=1,
        suppression_revision_id=UUID("51950000-0000-4000-8000-000000000005"),
        suppression_revision_number=2,
        image_id=(
            "sha256:73c221f1fc1fc14cbfdfd82e62fd4b87752ef71e0521227cb6790451fa51d2bd"
        ),
    ),
    CuratedSuppressionRevision(
        canonical_release="20.5",
        runtime_definition_id=UUID("52000000-0000-4000-8000-000000000001"),
        predecessor_revision_id=UUID("52000000-0000-4000-8000-000000000002"),
        predecessor_revision_number=1,
        suppression_revision_id=UUID("52000000-0000-4000-8000-000000000005"),
        suppression_revision_number=2,
        image_id=(
            "sha256:be35d7cb8899dfe474c6a1eca62da99c4ada1280f4325933d7b6193e83537638"
        ),
    ),
    CuratedSuppressionRevision(
        canonical_release="21.3",
        runtime_definition_id=UUID("3a000000-0000-4000-8000-000000000001"),
        predecessor_revision_id=UUID("3a000000-0000-4000-8000-000000000004"),
        predecessor_revision_number=2,
        suppression_revision_id=UUID("3a000000-0000-4000-8000-000000000006"),
        suppression_revision_number=3,
        image_id=(
            "sha256:70e0166fd79ea731c3f81056c2f0516f8fe5c9c8ec729b0c0f1c3e6bac66f254"
        ),
    ),
    CuratedSuppressionRevision(
        canonical_release="22.0-beta.2",
        runtime_definition_id=UUID("52200000-0000-4000-8000-000000000001"),
        predecessor_revision_id=UUID("52200000-0000-4000-8000-000000000002"),
        predecessor_revision_number=1,
        suppression_revision_id=UUID("52200000-0000-4000-8000-000000000005"),
        suppression_revision_number=2,
        image_id=(
            "sha256:4fb611666a2637904d6edd0cf90aa8cfc12c8d89edaa7a7fa60c61ef26655918"
        ),
    ),
)


def derive_curated_suppression_revision(
    lineage: CuratedSuppressionRevision,
    *,
    predecessor: RuntimeRevision,
    artifact: RuntimeContainerArtifact,
    profile: RuntimeExecutionProfile,
) -> tuple[RuntimeRevision, RuntimeContainerArtifact, RuntimeExecutionProfile]:
    """Derive the additive unapproved catalog records from exact trusted history."""

    if (
        predecessor.id != lineage.predecessor_revision_id
        or predecessor.runtime_definition_id != lineage.runtime_definition_id
        or predecessor.revision_number != lineage.predecessor_revision_number
        or artifact.runtime_revision_id != predecessor.id
        or artifact.image_id != lineage.image_id
        or profile.runtime_revision_id != predecessor.id
        or profile.python_bytecode_policy
        is not PythonBytecodePolicy.INTERPRETER_DEFAULT
        or profile.kodi_release.canonical != lineage.canonical_release
    ):
        raise ValueError("curated predecessor identity does not match trusted lineage")
    revision = replace(
        predecessor,
        id=lineage.suppression_revision_id,
        revision_number=lineage.suppression_revision_number,
        approval_state=RevisionApprovalState.NOT_APPROVED,
        approved_at=None,
        approval_note=None,
        trusted_build_confirmed_at=None,
    )
    return (
        revision,
        RuntimeContainerArtifact(
            runtime_revision_id=revision.id,
            image_id=artifact.image_id,
        ),
        replace(
            profile,
            runtime_revision_id=revision.id,
            python_bytecode_policy=PythonBytecodePolicy.SUPPRESS,
        ),
    )
