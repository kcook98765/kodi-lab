from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from kodi_lab.domain import (
    RevisionApprovalState,
    RuntimeDefinition,
    RuntimeRevision,
    normalize_sha256_digest,
)

CONTENT_DIGEST = "a" * 64
MANIFEST_DIGEST = "b" * 64
BUILT_AT = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)


def make_revision(**overrides: object) -> RuntimeRevision:
    values: dict[str, object] = {
        "id": uuid4(),
        "runtime_definition_id": uuid4(),
        "revision_number": 1,
        "source_kind": "official-package",
        "source_reference": "ppa:team-xbmc/ppa@21.3",
        "content_digest": CONTENT_DIGEST,
        "manifest_reference": "catalog/kodi-21.3.yaml",
        "manifest_digest": MANIFEST_DIGEST,
        "built_at": BUILT_AT,
    }
    values.update(overrides)
    return RuntimeRevision(**values)  # type: ignore[arg-type]


def test_valid_runtime_definition() -> None:
    definition_id = uuid4()

    definition = RuntimeDefinition(
        id=definition_id,
        slug="kodi-21.3",
        kodi_version="21.3",
    )

    assert definition.id == definition_id
    assert definition.slug == "kodi-21.3"


@pytest.mark.parametrize("slug", ["", " ", "Kodi-21", "-kodi", "kodi 21"])
def test_runtime_definition_rejects_invalid_slug(slug: str) -> None:
    with pytest.raises(ValueError, match="slug"):
        RuntimeDefinition(id=uuid4(), slug=slug, kodi_version="21.3")


def test_runtime_definition_rejects_blank_kodi_version() -> None:
    with pytest.raises(ValueError, match="kodi_version"):
        RuntimeDefinition(id=uuid4(), slug="kodi-21", kodi_version=" ")


def test_revision_normalizes_sha256_digests() -> None:
    revision = make_revision(
        content_digest=CONTENT_DIGEST.upper(),
        manifest_digest=f"sha256:{MANIFEST_DIGEST.upper()}",
    )

    assert revision.content_digest == f"sha256:{CONTENT_DIGEST}"
    assert revision.manifest_digest == f"sha256:{MANIFEST_DIGEST}"
    assert normalize_sha256_digest(CONTENT_DIGEST) == f"sha256:{CONTENT_DIGEST}"


@pytest.mark.parametrize("digest", ["", "sha256:abc", "g" * 64, "sha512:" + "a" * 64])
def test_revision_rejects_invalid_digest(digest: str) -> None:
    with pytest.raises(ValueError, match="SHA-256"):
        make_revision(content_digest=digest)


def test_revision_requires_positive_number() -> None:
    with pytest.raises(ValueError, match="positive"):
        make_revision(revision_number=0)


@pytest.mark.parametrize(
    "field", ["source_kind", "source_reference", "manifest_reference"]
)
def test_revision_rejects_blank_provenance(field: str) -> None:
    with pytest.raises(ValueError, match=field):
        make_revision(**{field: " "})


def test_revision_requires_timezone_aware_build_timestamp() -> None:
    with pytest.raises(ValueError, match="built_at"):
        make_revision(built_at=datetime(2026, 9, 14, 12, 0))


def test_approval_requires_timestamp_and_normalizes_timezone() -> None:
    with pytest.raises(ValueError, match="approved_at"):
        make_revision(approval_state=RevisionApprovalState.APPROVED)

    approved_at = datetime(2026, 9, 14, 5, 0, tzinfo=UTC) - timedelta(hours=7)
    revision = make_revision().approve(approved_at=approved_at, note="reviewed")

    assert revision.approval_state is RevisionApprovalState.APPROVED
    assert revision.approved_at is not None
    assert revision.approved_at.tzinfo is UTC
    assert revision.approval_note == "reviewed"


def test_not_approved_revision_rejects_approval_metadata() -> None:
    with pytest.raises(ValueError, match="not-approved"):
        make_revision(approved_at=BUILT_AT)


def test_trust_confirmation_is_independent_from_approval() -> None:
    revision = make_revision(requires_trusted_build_confirmation=True)
    confirmed = revision.confirm_trusted_build(confirmed_at=BUILT_AT)

    assert confirmed.approval_state is RevisionApprovalState.NOT_APPROVED
    assert confirmed.trusted_build_confirmed_at == BUILT_AT


def test_trust_confirmation_requires_explicit_requirement() -> None:
    with pytest.raises(ValueError, match="requires_trusted"):
        make_revision().confirm_trusted_build(confirmed_at=BUILT_AT)


def test_revision_intrinsic_fields_are_frozen() -> None:
    revision = make_revision()

    with pytest.raises(FrozenInstanceError):
        revision.revision_number = 2  # type: ignore[misc]
