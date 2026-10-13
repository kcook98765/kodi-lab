"""Real GPG verification of retained upstream signed OSTree metadata."""

import hashlib
from pathlib import Path

import pytest

from kodi_lab.runtime.ostree import (
    BoundedCommandRunner,
    OstreeAcquisitionError,
    _metadata_config,
    _validate_key_listing,
    _validate_signature_status,
    detached_signatures,
    signed_component_metadata,
    trusted_key_bytes,
)
from kodi_lab.runtime.recipes import FLATHUB_KEY_SHA256, load_inactive_ostree_recipe
from tests.runtime.ostree_support import signed_fixture


@pytest.fixture
def crypto_stage(tmp_path: Path) -> Path:
    for name in ("home", "gnupg", "tmp"):
        (tmp_path / name).mkdir(mode=0o700)
    (tmp_path / "flathub.gpg").write_bytes(trusted_key_bytes())
    return tmp_path


def test_real_key_material_and_primary_subkey_binding(crypto_stage: Path) -> None:
    assert hashlib.sha256(trusted_key_bytes()).hexdigest() == FLATHUB_KEY_SHA256
    result = BoundedCommandRunner().run(
        (
            "/usr/bin/gpg",
            "--batch",
            "--no-options",
            "--no-autostart",
            "--with-colons",
            "--show-keys",
            str(crypto_stage / "flathub.gpg"),
        ),
        stage=crypto_stage,
        timeout=10,
    )
    assert result.returncode == 0
    _validate_key_listing(result.output)


@pytest.mark.parametrize("role", ["app", "platform", "gl"])
def test_real_upstream_signatures_and_signed_metadata(
    role: str, crypto_stage: Path
) -> None:
    commit, detached = signed_fixture(role)
    component = next(
        c
        for c in load_inactive_ostree_recipe().recipe.provider.components
        if c.role == role
    )
    embedded = signed_component_metadata(commit, component)
    _metadata_config(embedded, component)
    (crypto_stage / "commit").write_bytes(commit)
    for signature in detached_signatures(detached):
        (crypto_stage / "signature").write_bytes(signature)
        result = BoundedCommandRunner().run(
            (
                "/usr/bin/gpgv",
                "--homedir",
                str(crypto_stage / "gnupg"),
                "--keyring",
                str(crypto_stage / "flathub.gpg"),
                "--status-fd=1",
                str(crypto_stage / "signature"),
                str(crypto_stage / "commit"),
            ),
            stage=crypto_stage,
            timeout=10,
        )
        _validate_signature_status(result)


@pytest.mark.parametrize("tamper", ["signature", "commit"])
def test_real_gpg_rejects_tampered_upstream_data(
    tamper: str, crypto_stage: Path
) -> None:
    commit, detached = signed_fixture("app")
    signature = detached_signatures(detached)[0]
    if tamper == "signature":
        signature = signature[:-1] + bytes([signature[-1] ^ 1])
    else:
        commit = commit[:100] + bytes([commit[100] ^ 1]) + commit[101:]
    (crypto_stage / "commit").write_bytes(commit)
    (crypto_stage / "signature").write_bytes(signature)
    result = BoundedCommandRunner().run(
        (
            "/usr/bin/gpgv",
            "--homedir",
            str(crypto_stage / "gnupg"),
            "--keyring",
            str(crypto_stage / "flathub.gpg"),
            "--status-fd=1",
            str(crypto_stage / "signature"),
            str(crypto_stage / "commit"),
        ),
        stage=crypto_stage,
        timeout=10,
    )
    assert result.returncode != 0
    with pytest.raises(OstreeAcquisitionError, match="signature_invalid"):
        _validate_signature_status(result)


def test_corrupt_commit_cannot_pass_immutable_identity() -> None:
    commit, _ = signed_fixture("app")
    component = load_inactive_ostree_recipe().recipe.provider.components[0]
    with pytest.raises(OstreeAcquisitionError, match="commit_digest_mismatch"):
        signed_component_metadata(commit + b"corrupted", component)


@pytest.mark.parametrize("data", [b"", b"\x00", b"invalid metadata"])
def test_missing_or_malformed_signature_metadata_is_rejected(data: bytes) -> None:
    with pytest.raises(OstreeAcquisitionError):
        detached_signatures(data)


@pytest.mark.parametrize(
    "changes", [{"ref": "app/tv.kodi.Kodi/aarch64/beta"}, {"collection": "attacker"}]
)
def test_signed_commit_binding_checks_are_independent(changes: dict[str, str]) -> None:
    commit, _ = signed_fixture("app")
    # Deliberately bypass the model validator to exercise the lower verification layer.
    component = (
        load_inactive_ostree_recipe()
        .recipe.provider.components[0]
        .model_copy(update=changes)
    )
    with pytest.raises(OstreeAcquisitionError, match="binding_mismatch"):
        signed_component_metadata(commit, component)
