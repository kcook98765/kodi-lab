"""RC2 exact identity, retained RC1 evidence and cross-release refusal."""

from __future__ import annotations

import base64
import hashlib
import json
import runpy
import subprocess
from importlib import resources
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from kodi_lab.application.managed_bridge_trust import require_approved_identity
from kodi_lab.application.runtime_installations import preview_acknowledgement
from kodi_lab.broker.managed_bridge_trust import approved_bridge_build
from kodi_lab.runtime.ostree import (
    CommandResult,
    OstreeAcquisitionError,
    _validate_signature_status,
    detached_signatures,
    signed_component_metadata,
    trusted_key_bytes,
)
from kodi_lab.runtime.ostree_assembly import load_assembly_assets
from kodi_lab.runtime.ostree_validation import native_identity
from kodi_lab.runtime.preview import PreviewAcknowledgement
from kodi_lab.runtime.recipes import (
    PIERS_DISPLAY_INSTALLATION_DIGEST,
    PIERS_RC2_INSTALLATION_DIGEST,
    RuntimeRecipe,
    load_inactive_ostree_recipe,
    load_installation_recipe,
    load_recorded_reviewed_recipe,
)
from kodi_lab.runtime.ubuntu_support import load_support_closure
from kodi_lab.runtime.validation import RecipeValidationError


def test_rc2_has_distinct_pins_and_preserves_rc1_support_and_source() -> None:
    current = load_installation_recipe("piers")
    old = load_recorded_reviewed_recipe(
        "piers", "piers-22.0-rc1-r3", 3, PIERS_DISPLAY_INSTALLATION_DIGEST
    )
    assert current.digest == PIERS_RC2_INSTALLATION_DIGEST != old.digest
    assert current.recipe.kodi_version == "22.0-rc.2"
    assert old.recipe.kodi_version == "22.0-rc.1"
    assert current.recipe.provider.components[1:] == old.recipe.provider.components[1:]
    assert current.recipe.provider.components[0] != old.recipe.provider.components[0]
    assert load_support_closure(revision=4) == load_support_closure(revision=3)
    before, after = load_assembly_assets(revision=3), load_assembly_assets(revision=4)
    assert {n for n in before if before[n] != after[n]} == {
        "Dockerfile",
        "healthcheck.py",
    }
    assert current.recipe.definition_id != old.recipe.definition_id


@pytest.mark.parametrize("field", ["kodi_version", "provider", "execution_profile"])
def test_old_payload_cannot_be_admitted_as_rc2(field: str) -> None:
    current = load_installation_recipe("piers").recipe.model_dump()
    old = load_recorded_reviewed_recipe(
        "piers", "piers-22.0-rc1-r3", 3, PIERS_DISPLAY_INSTALLATION_DIGEST
    ).recipe.model_dump()
    current[field] = old[field]
    with pytest.raises(ValidationError):
        RuntimeRecipe.model_validate(current)


def test_old_preview_consent_cannot_be_relabelled_rc2() -> None:
    current = preview_acknowledgement()
    assert current["release"] == "22.0-rc.2"
    current["release"] = "22.0-rc.1"
    with pytest.raises(ValidationError, match="release differs"):
        PreviewAcknowledgement.model_validate(current)


def test_signed_published_rc2_commit_and_exact_binding(tmp_path: Path) -> None:
    fixture = json.loads(
        Path(__file__).with_name("fixtures").joinpath("rc2-signed-app.json").read_text()
    )
    commit = base64.b64decode(fixture["commit"], validate=True)
    detached = base64.b64decode(fixture["detached"], validate=True)
    component = load_inactive_ostree_recipe("22.0-rc.2").recipe.provider.components[0]
    assert hashlib.sha256(commit).hexdigest() == component.commit
    metadata = signed_component_metadata(commit, component)
    assert hashlib.sha256(metadata.encode()).hexdigest() == component.metadata_sha256
    key = tmp_path / "flathub.gpg"
    key.write_bytes(trusted_key_bytes())
    raw = tmp_path / "app.commit"
    raw.write_bytes(commit)
    for i, sig in enumerate(detached_signatures(detached)):
        signature = tmp_path / f"signature-{i}"
        signature.write_bytes(sig)
        result = subprocess.run(
            [
                "/usr/bin/gpgv",
                "--homedir",
                str(tmp_path),
                "--keyring",
                str(key),
                "--status-fd=1",
                str(signature),
                str(raw),
            ],
            capture_output=True,
            timeout=10,
            check=False,
        )
        _validate_signature_status(
            CommandResult(result.returncode, result.stdout + result.stderr)
        )
    with pytest.raises(OstreeAcquisitionError, match="commit_digest_mismatch"):
        signed_component_metadata(
            commit, load_inactive_ostree_recipe().recipe.provider.components[0]
        )


@pytest.mark.parametrize(
    "ordinal,revision,accepted",
    [
        ("2", "d686683a", True),
        ("1", "28ea2eac", False),
        ("2", "28ea2eac", False),
        ("3", "d686683a", False),
    ],
)
def test_native_rc2_requires_exact_source_and_ordinal(
    ordinal: str, revision: str, accepted: bool
) -> None:
    identity = {
        "name": "Kodi",
        "version": {
            "major": 22,
            "minor": 0,
            "tag": "releasecandidate",
            "tagversion": ordinal,
            "revision": revision,
        },
    }
    if accepted:
        assert native_identity(identity, "22.0-rc.2")["tagversion"] == "2"
    else:
        with pytest.raises(RecipeValidationError):
            native_identity(identity, "22.0-rc.2")


@pytest.mark.parametrize("ordinal", ["1", "2", True])
def test_rc2_healthcheck_rejects_old_native_identity(
    monkeypatch: pytest.MonkeyPatch, ordinal: object
) -> None:
    asset = resources.files("kodi_lab.runtime").joinpath(
        "assembly_assets/piers-22.0-rc2-r4/healthcheck.py"
    )
    health = runpy.run_path(str(asset))
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: None)
    payload = json.dumps(
        {
            "result": {
                "name": "Kodi",
                "version": {
                    "major": 22,
                    "minor": 0,
                    "tag": "releasecandidate",
                    "tagversion": ordinal,
                },
            }
        }
    ).encode()

    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def read(self, size: int) -> bytes:
            return payload

    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **kw: Response())
    if ordinal == "2":
        health["main"]()
    else:
        with pytest.raises(RuntimeError, match="RC2 identity mismatch"):
            health["main"]()


def test_rc2_reuses_reviewed_bridge_but_requires_exact_live_release() -> None:
    approved = approved_bridge_build("22.0-rc.2")
    assert approved == approved_bridge_build("22.0-rc.1")
    identity = SimpleNamespace(
        **{
            k: approved[k]
            for k in (
                "addon_id",
                "addon_version",
                "protocol_version",
                "build_fingerprint",
            )
        },
        kodi_version="22.0",
        kodi_release="22.0-rc.2",
        capabilities=["ui_observation"],
    )
    require_approved_identity(identity, "22.0-rc.2")  # type: ignore[arg-type]
    identity.kodi_release = "22.0-rc.1"
    with pytest.raises(RuntimeError, match="managed_bridge_identity_mismatch"):
        require_approved_identity(identity, "22.0-rc.2")  # type: ignore[arg-type]


@pytest.mark.parametrize("old_app", [False, True])
def test_context_binds_the_acquired_release_instead_of_the_old_default(
    monkeypatch: pytest.MonkeyPatch, old_app: bool
) -> None:
    from uuid import uuid4

    from kodi_lab.runtime import ostree_context
    from kodi_lab.runtime.ostree_context import ContextError, verified_context
    from tests.runtime.test_ostree_installation import assembly

    operation = uuid4()
    evidence = assembly(operation).acquisition
    if old_app:
        old = load_inactive_ostree_recipe().recipe.provider.components[0]
        app = evidence.components[0].model_copy(update={"commit": old.commit})
        evidence = evidence.model_copy(
            update={"components": (app, *evidence.components[1:])}
        )
    # Isolate the release/closure boundary; descriptor, payload-race and sealing
    # checks are exercised with actual files in test_ostree_context.
    acquired = SimpleNamespace(evidence=evidence, assert_handoff=lambda _: None)

    def capture(
        _archive: object, _acquired: object, role: str, _deadline: float
    ) -> tuple[str, int, int]:
        component = next(c for c in evidence.components if c.role == role)
        return (
            component.inventory_sha256,
            component.entry_count,
            component.content_bytes,
        )

    monkeypatch.setattr(ostree_context, "_capture_tree", capture)
    if old_app:
        with (
            pytest.raises(ContextError, match="component_identity_mismatch"),
            verified_context(acquired, operation, entries={}),  # type: ignore[arg-type]
        ):
            pytest.fail("RC1 app entered RC2 assembly")
    else:
        with verified_context(acquired, operation, entries={}) as context:  # type: ignore[arg-type]
            assert (
                context.recipe_digest == load_inactive_ostree_recipe("22.0-rc.2").digest
            )
