"""Only public staging artifacts receive modes independent of the host umask."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.packaging.test_linux_v1_bundle import bundle as public_bundle_fixture
from tests.packaging.test_linux_v1_bundle import bundle_tool, verify

bundle = public_bundle_fixture


@pytest.mark.parametrize("mask", ["022", "077"])
def test_public_staging_modes_ignore_umask(tmp_path: Path, mask: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import json, os, sys
from pathlib import Path
from tests.packaging.test_linux_v1_bundle import bundle, bundle_tool, verify
mask = int(sys.argv[2], 8)
os.umask(mask)
root = Path(sys.argv[1])
root.mkdir(mode=0o700)
staged = root / 'bundle'
staged.mkdir()
bundle.__wrapped__(staged)
before = verify(staged)
bundle_tool['public_bundle_modes'](staged)
assert verify(staged) == before
assert os.umask(mask) == mask
(root / 'private').write_bytes(b'private configuration')
print(json.dumps({str(p.relative_to(root)): p.stat().st_mode & 0o777
                  for p in [root, *root.rglob('*')]}))
""",
            str(tmp_path / "installation"),
            mask,
        ],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        check=True,
    )
    modes = json.loads(result.stdout)
    assert modes["."] == 0o700
    assert modes["private"] == (0o600 if mask == "077" else 0o644)
    assert modes["bundle"] == 0o755
    for name in ("bridge", "repository", "runtime-recipes"):
        assert modes[f"bundle/{name}"] == 0o755
    for name in bundle_tool["RELEASE_FILES"] | {"BUNDLE.json", "SHA256SUMS"}:
        assert modes[f"bundle/{name}"] == (
            0o755 if name == "linux-v1-bundle" else 0o644
        )


@pytest.mark.parametrize(
    "unsafe", ["private", "symlink", "directory-link", "hardlink", "extra-directory"]
)
def test_modes_refuse_non_public_or_linked_inventory(
    bundle: Path, tmp_path: Path, unsafe: str
) -> None:
    private = tmp_path.parent / f"{tmp_path.name}-private"
    private.write_bytes(b"private")
    private.chmod(0o600)
    artifact = bundle / "INSTALL.md"
    artifact.chmod(0o600)
    if unsafe == "private":
        (bundle / ".env").write_bytes(b"private configuration")
    elif unsafe == "symlink":
        artifact.unlink()
        artifact.symlink_to(private)
    elif unsafe == "directory-link":
        (bundle / "other").symlink_to(private.parent, target_is_directory=True)
    elif unsafe == "hardlink":
        artifact.unlink()
        os.link(private, artifact)
    else:
        (bundle / "other").mkdir()
    try:
        with pytest.raises(ValueError):
            bundle_tool["public_bundle_modes"](bundle)
        assert private.stat().st_mode & 0o777 == 0o600
        assert artifact.stat().st_mode & 0o777 == 0o600
    finally:
        private.unlink()


def test_public_modes_do_not_replace_content_verification(bundle: Path) -> None:
    bundle_tool["public_bundle_modes"](bundle)
    (bundle / "INSTALL.md").write_bytes(b"tampered")
    with pytest.raises(ValueError, match="artifact differs"):
        verify(bundle)
