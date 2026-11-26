from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from scripts.qualification_run import (
    MANIFEST,
    require_current_artifacts,
    require_owned_namespaces,
    write,
)


def test_cleanup_preserves_artifacts_added_after_manifest(tmp_path: Path) -> None:
    root = tmp_path / "run"
    inbox = root / "artifact-inbox"
    inbox.mkdir(parents=True)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    original = b"observed run result"
    (inbox / "result.txt").write_bytes(original)
    write(
        evidence / MANIFEST,
        {"artifacts/result.txt": hashlib.sha256(original).hexdigest()},
    )
    require_current_artifacts(root, evidence, os.getuid(), already_removing=False)
    extra = inbox / "late.txt"
    extra.write_text("late evidence from an unfinished helper")
    with pytest.raises(ValueError, match="inbox changed"):
        require_current_artifacts(root, evidence, os.getuid(), already_removing=False)
    assert extra.read_text() == "late evidence from an unfinished helper"


def test_absent_artifact_inbox_requires_verified_removal_phase(tmp_path: Path) -> None:
    require_current_artifacts(tmp_path, tmp_path, os.getuid(), already_removing=True)
    with pytest.raises(FileNotFoundError):
        require_current_artifacts(
            tmp_path, tmp_path, os.getuid(), already_removing=False
        )


def test_exact_default_builder_history_namespace_is_run_owned() -> None:
    namespace = "klq-0123456789abcdef0123456789abcdef"
    require_owned_namespaces(
        namespace, {namespace, namespace + "-plugins", namespace + "_history"}
    )
    for foreign in ("buildkit_history", namespace + "_history-copy"):
        with pytest.raises(ValueError, match="Unexpected containerd namespaces"):
            require_owned_namespaces(namespace, {namespace, foreign})
