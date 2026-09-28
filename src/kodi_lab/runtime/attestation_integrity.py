"""Local integrity binding for reviewed runtime acquisition provenance.

The persistent state master key is the deployment trust anchor. A distinct
HMAC key is derived for this purpose; neither key is stored in SQLite.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
from pathlib import Path
from uuid import UUID

from kodi_lab.domain import RuntimeValidationEvidence
from kodi_lab.storage.secrets import SecretStoreError, decode_master_key

_PURPOSE = b"kodi-lab-runtime-recipe-attestation-v1\x00"
_BINDING_FIELD = "integrity_binding"
_BINDING_PATTERN = re.compile(r"hmac-sha256:[0-9a-f]{64}\Z")


class AttestationIntegrityError(RuntimeError):
    """The local integrity key or canonical provenance is unavailable."""


def canonical_json(value: object) -> str:
    """The one deterministic representation for signed local records."""

    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def installed_package_digest(packages: object) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(packages).encode()).hexdigest()


def _integrity_key(data_root: Path) -> bytes:
    """Read only the existing private state key and derive a separate purpose."""

    root_fd = key_fd = None
    try:
        root_fd = os.open(
            data_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        )
        root = os.fstat(root_fd)
        if not stat.S_ISDIR(root.st_mode) or stat.S_IMODE(root.st_mode) != 0o700:
            raise AttestationIntegrityError("private state root is unsafe")
        key_fd = os.open(
            "master-key",
            os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=root_fd,
        )
        key_file = os.fstat(key_fd)
        if (
            not stat.S_ISREG(key_file.st_mode)
            or stat.S_IMODE(key_file.st_mode) != 0o600
            or key_file.st_uid != root.st_uid
            or key_file.st_gid != root.st_gid
            or key_file.st_nlink != 1
        ):
            raise AttestationIntegrityError("private integrity key is unsafe")
        raw = os.read(key_fd, 129)
        if not raw.endswith(b"\n") or len(raw) > 128:
            raise AttestationIntegrityError("private integrity key is invalid")
        master = decode_master_key(raw.decode("ascii"))
        if master is None:
            raise AttestationIntegrityError("private integrity key is invalid")
        return hmac.digest(master, _PURPOSE, "sha256")
    except (OSError, UnicodeError, SecretStoreError) as error:
        raise AttestationIntegrityError(
            "private integrity key is unavailable"
        ) from error
    finally:
        if key_fd is not None:
            os.close(key_fd)
        if root_fd is not None:
            os.close(root_fd)


def _payload(
    attestation: dict[str, object],
    validation: object,
    *,
    operation_id: UUID,
    revision_id: UUID,
    image_id: str,
    evidence: RuntimeValidationEvidence,
) -> bytes:
    value = {
        "purpose": "kodi-lab-runtime-recipe-attestation-v1",
        "operation_id": str(operation_id),
        "runtime_revision_id": str(revision_id),
        "image_id": image_id,
        "attestation": attestation,
        "validation": validation,
        "evidence": {
            "id": str(evidence.id),
            "runtime_revision_id": str(evidence.runtime_revision_id),
            "contract_version": evidence.contract_version,
            "result": evidence.result.value,
            "started_at": evidence.started_at.isoformat(),
            "completed_at": evidence.completed_at.isoformat(),
            "recorded_at": evidence.recorded_at.isoformat(),
            "evidence_reference": evidence.evidence_reference,
            "summary": evidence.summary,
        },
    }
    return canonical_json(value).encode()


def seal_attestation(
    attestation_json: str,
    validation_json: str,
    *,
    data_root: Path,
    operation_id: UUID,
    revision_id: UUID,
    image_id: str,
    evidence: RuntimeValidationEvidence,
) -> str:
    """Add the HMAC before the one transaction that makes a row ready."""

    try:
        attestation = json.loads(attestation_json)
        validation = json.loads(validation_json)
        if (
            not isinstance(attestation, dict)
            or _BINDING_FIELD in attestation
            or canonical_json(attestation) != attestation_json
            or canonical_json(validation) != validation_json
        ):
            raise ValueError
        key = _integrity_key(data_root)
        message = _payload(
            attestation,
            validation,
            operation_id=operation_id,
            revision_id=revision_id,
            image_id=image_id,
            evidence=evidence,
        )
        attestation[_BINDING_FIELD] = (
            "hmac-sha256:" + hmac.digest(key, message, "sha256").hex()
        )
        return canonical_json(attestation)
    except (TypeError, ValueError) as error:
        raise AttestationIntegrityError("canonical attestation is invalid") from error


def verify_attestation(
    attestation_json: str,
    validation_json: str,
    *,
    data_root: Path,
    operation_id: UUID,
    revision_id: UUID,
    image_id: str,
    evidence: RuntimeValidationEvidence,
) -> bool:
    """Reject missing, malformed, or changed persisted provenance."""

    try:
        attestation = json.loads(attestation_json)
        validation = json.loads(validation_json)
        if (
            not isinstance(attestation, dict)
            or canonical_json(attestation) != attestation_json
            or canonical_json(validation) != validation_json
        ):
            return False
        binding = attestation.pop(_BINDING_FIELD, None)
        if not isinstance(binding, str) or _BINDING_PATTERN.fullmatch(binding) is None:
            return False
        key = _integrity_key(data_root)
        message = _payload(
            attestation,
            validation,
            operation_id=operation_id,
            revision_id=revision_id,
            image_id=image_id,
            evidence=evidence,
        )
        expected = "hmac-sha256:" + hmac.digest(key, message, "sha256").hex()
        return hmac.compare_digest(binding, expected)
    except (AttestationIntegrityError, TypeError, ValueError):
        return False
