"""The RC1 candidate is reviewed text, without changing the stable publisher."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

from kodi_lab.broker.managed_bridge_trust import approved_bridge_build
from kodi_lab.kodi.rc1_bridge import (
    build_rc1_bridge_archive,
    reviewed_rc1_bridge_sources,
)
from scripts.build_narrow_bridge import ADDON_VERSION, build_archive

ROOT = Path(__file__).parents[2] / "bridge/service.kodi_lab_bridge"


def test_candidate_matches_live_qualified_archive_and_preserves_stable(
    tmp_path: Path,
) -> None:
    fragment = (
        ROOT.parents[1] / "src/kodi_lab/kodi/rc1_bridge_identity.txt"
    ).read_bytes()
    assert fragment.endswith(b"\n") and not fragment.endswith(b"\n\n")
    candidate = tmp_path / "rc1.zip"
    digest = build_rc1_bridge_archive(ROOT, candidate)
    assert digest == "031373f6311a3950f9e5374304433d2e8ae0cc8d255034752891535c3ee71147"
    assert build_rc1_bridge_archive(ROOT, tmp_path / "again.zip") == digest
    assert ADDON_VERSION == "1.1.27"
    assert build_archive(ROOT, tmp_path / "stable.zip") != digest
    for name, value in reviewed_rc1_bridge_sources(ROOT).items():
        expected = approved_bridge_build("22.0-rc.1")["files"].get(name)
        if expected is not None:
            assert hashlib.sha256(value).hexdigest() == expected


@pytest.mark.parametrize("failure", ["modified_source", "symlink", "oversized"])
def test_reviewed_overlay_rejects_untrusted_source(
    tmp_path: Path,
    failure: str,
) -> None:
    root = tmp_path / "source"
    shutil.copytree(ROOT, root)
    if failure == "symlink":
        (root / "narrow_bridge.py").unlink()
        (root / "narrow_bridge.py").symlink_to(ROOT / "narrow_bridge.py")
    elif failure == "modified_source":
        (root / "narrow_bridge.py").write_text("print('unreviewed')")
    else:
        (root / "README.md").write_bytes(b"x" * 1048577)
    with pytest.raises(ValueError):
        reviewed_rc1_bridge_sources(root)


def test_reviewed_overlay_rejects_changed_fragment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("kodi_lab.kodi.rc1_bridge._FRAGMENT_SHA256", "0" * 64)
    with pytest.raises(ValueError, match="overlay differs"):
        reviewed_rc1_bridge_sources(ROOT)
