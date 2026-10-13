"""Real OSTree verification, plus an explicitly opt-in upstream acquisition gate.

KODI_LAB_TEST_OSTREE and KODI_LAB_TEST_OSTREE_LIBDIR select isolated *test*
tooling when OSTree is not installed. Production acquisition has no such knobs.
No Docker images, Kodi processes, database records, or runtime installs are made.
"""

import os
import shutil
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from kodi_lab.runtime.ostree import (
    OstreeAcquirer,
    detached_signatures,
    trusted_key_bytes,
)
from kodi_lab.runtime.recipes import load_inactive_ostree_recipe
from tests.runtime.ostree_support import signed_fixture


@pytest.fixture
def tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Callable[..., subprocess.CompletedProcess[bytes]]:
    binary = os.environ.get("KODI_LAB_TEST_OSTREE") or shutil.which("ostree")
    if binary is None:
        pytest.skip("real OSTree integration requires OSTree tooling")
    library = os.environ.get("KODI_LAB_TEST_OSTREE_LIBDIR")
    original = subprocess.Popen

    def isolated_popen(args: Sequence[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        command = list(args)
        if "/usr/bin/ostree" in command:
            command[command.index("/usr/bin/ostree")] = binary
            if library:
                kwargs["env"] = dict(kwargs["env"], LD_LIBRARY_PATH=library)
        return original(command, **kwargs)

    # Keep the production runner's limits, environment, timeout, and cleanup.
    monkeypatch.setattr(subprocess, "Popen", isolated_popen)
    environment = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": str(tmp_path)}
    if library:
        environment["LD_LIBRARY_PATH"] = library

    def invoke(repo: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [binary, f"--repo={repo}", *args],
            env=environment,
            capture_output=True,
            timeout=30,
            check=False,
        )

    return invoke


@pytest.mark.parametrize("role", ["app", "platform", "gl"])
def test_real_ostree_checks_retained_upstream_signature(
    role: str, tmp_path: Path, tool: Callable[..., subprocess.CompletedProcess[bytes]]
) -> None:
    repo = tmp_path / "repo"
    key = tmp_path / "flathub.gpg"
    key.write_bytes(trusted_key_bytes())
    assert tool(repo, "init", "--mode=archive").returncode == 0
    component = next(
        c
        for c in load_inactive_ostree_recipe().recipe.provider.components
        if c.role == role
    )
    assert (
        tool(
            repo,
            "remote",
            "add",
            f"--gpg-import={key}",
            "reviewed",
            component.repository_url,
        ).returncode
        == 0
    )
    objects = repo / "objects" / component.commit[:2]
    objects.mkdir(exist_ok=True)
    commit, detached = signed_fixture(role)
    (objects / (component.commit[2:] + ".commit")).write_bytes(commit)
    (objects / (component.commit[2:] + ".commitmeta")).write_bytes(detached)
    result = tool(repo, "show", "--gpg-verify-remote=reviewed", component.commit)
    assert result.returncode == 0, result.stderr.decode()
    assert b"Good signature" in result.stdout
    assert b"4184DD4D907A7CAE" in result.stdout
    signature = detached_signatures(detached)[0]
    offset = detached.index(signature) + len(signature) - 1
    changed = detached[:offset] + bytes([detached[offset] ^ 1]) + detached[offset + 1 :]
    (objects / (component.commit[2:] + ".commitmeta")).write_bytes(changed)
    altered = tool(repo, "show", "--gpg-verify-remote=reviewed", component.commit)
    assert altered.returncode != 0 or b"Good signature" not in altered.stdout


@pytest.mark.parametrize("corrupt", ["commit", "content"])
def test_real_ostree_fsck_rejects_object_corruption(
    corrupt: str,
    tmp_path: Path,
    tool: Callable[..., subprocess.CompletedProcess[bytes]],
) -> None:
    repo = tmp_path / "repo"
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "hello").write_text("deterministic integrity fixture\n")
    assert tool(repo, "init", "--mode=bare-user-only").returncode == 0
    result = tool(
        repo,
        "commit",
        "--branch=validation",
        "--subject=fixture",
        "--timestamp=2026-09-28T00:00:00Z",
        f"--tree=dir={tree}",
    )
    assert result.returncode == 0, result.stderr.decode()
    assert tool(repo, "fsck").returncode == 0
    suffix = "commit" if corrupt == "commit" else "file"
    objects = list(repo.glob(f"objects/*/*.{suffix}"))
    assert objects
    objects[0].chmod(0o600)
    with objects[0].open("r+b") as stream:
        value = stream.read(1)
        stream.seek(0)
        stream.write(bytes([value[0] ^ 1]))
    assert tool(repo, "fsck").returncode != 0


def test_real_ostree_missing_tree_cannot_claim_verified_closure(
    tmp_path: Path, tool: Callable[..., subprocess.CompletedProcess[bytes]]
) -> None:
    repo = tmp_path / "repo"
    assert tool(repo, "init", "--mode=archive").returncode == 0
    component = load_inactive_ostree_recipe().recipe.provider.components[0]
    objects = repo / "objects" / component.commit[:2]
    objects.mkdir(exist_ok=True)
    (objects / (component.commit[2:] + ".commit")).write_bytes(signed_fixture("app")[0])
    assert tool(repo, "fsck").returncode != 0


@pytest.mark.slow
@pytest.mark.skipif(
    os.environ.get("KODI_LAB_TEST_LIVE_OSTREE") != "1",
    reason="explicit upstream acquisition gate only",
)
def test_live_exact_rc1_acquisition(
    tool: Callable[..., subprocess.CompletedProcess[bytes]],
) -> None:
    root = os.environ.get("KODI_LAB_TEST_LIVE_STAGING")
    assert root is not None, (
        "live gate requires an explicitly isolated staging directory"
    )
    staging = Path(root)
    staging.mkdir(mode=0o700, exist_ok=False)
    try:
        with OstreeAcquirer(staging).acquire(uuid4()) as acquired:
            assert acquired.evidence.artifact_closure == "verified"
            assert acquired.evidence.installation_status == "inactive"
            assert all(
                acquired.component_path(c.role).is_dir()
                for c in acquired.evidence.components
            )
            output = staging.parent / "live-verification.json"
            assert not output.exists()
            output.write_bytes(acquired.evidence.canonical_bytes())
        assert list(staging.iterdir()) == [staging / "ostree-acquisition.lock"]
    finally:
        shutil.rmtree(staging)
