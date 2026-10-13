"""Mocked acquisition flow and real filesystem boundary tests, not crypto proof."""

import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from kodi_lab.runtime import ostree
from kodi_lab.runtime.ostree import (
    CommandResult,
    OstreeAcquirer,
    OstreeAcquisitionBusy,
    OstreeAcquisitionError,
    _application_release,
    _directory,
    _inventory,
    _read_file,
    _safe_link,
    _stage_quota,
    _validate_signature_status,
    _write_file,
    signed_component_metadata,
)
from kodi_lab.runtime.recipes import (
    FLATHUB_PRIMARY_KEY,
    FLATHUB_SIGNING_SUBKEY,
    KODI_RC1_SOURCE_REVISION,
    load_inactive_ostree_recipe,
)
from tests.runtime.ostree_support import signed_fixture


def valid_signature_status() -> bytes:
    return (
        f"[GNUPG:] VALIDSIG {FLATHUB_SIGNING_SUBKEY} 2026-09-28 "
        f"1790611713 0 4 0 1 10 00 {FLATHUB_PRIMARY_KEY}\n"
    ).encode()


class FakeRunner:
    def __init__(self, *, fail: str | None = None, interrupt: bool = False) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.fail = fail
        self.interrupt = interrupt

    def run(
        self, argv: tuple[str, ...], *, stage: Path, timeout: float
    ) -> CommandResult:
        assert 0 < timeout <= 900
        self.commands.append(argv)
        if self.interrupt and "pull" in argv:
            raise KeyboardInterrupt
        if self.fail is not None and self.fail in argv:
            return CommandResult(1, b"private upstream failure")
        if argv[0] == "/usr/bin/gpg":
            return CommandResult(
                0,
                (
                    f"pub:::::::::\nfpr:::::::::{FLATHUB_PRIMARY_KEY}:\n"
                    f"sub:::::::::\nfpr:::::::::{FLATHUB_SIGNING_SUBKEY}:\n"
                ).encode(),
            )
        if argv[0] == "/usr/bin/gpgv":
            return CommandResult(0, valid_signature_status())
        if "init" in argv:
            (stage / "repo/objects").mkdir(parents=True)
        if "pull" in argv:
            pin = argv[-1].split("@")[1]
            component = next(
                c
                for c in load_inactive_ostree_recipe().recipe.provider.components
                if c.commit == pin
            )
            commit, detached = signed_fixture(component.role)
            objects = stage / "repo/objects" / pin[:2]
            objects.mkdir(exist_ok=True)
            (objects / (pin[2:] + ".commit")).write_bytes(commit)
            (objects / (pin[2:] + ".commitmeta")).write_bytes(detached)
        if "checkout" in argv:
            Path(argv[-1]).mkdir()
        return CommandResult(0, b"")


@pytest.fixture
def worker_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    tmp_path.chmod(0o700)
    monkeypatch.setattr(
        shutil, "disk_usage", lambda _path: SimpleNamespace(free=16 * 1024**3)
    )
    return tmp_path


@pytest.fixture
def mocked_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    # These substitutions are explicitly flow-test doubles. Crypto has its own
    # real upstream tests; complete payload verification has an opt-in live gate.
    monkeypatch.setattr(ostree, "_application_release", lambda _path: None)
    monkeypatch.setattr(
        ostree, "_inventory", lambda _path, _component: ("a" * 64, 3, 100)
    )


def test_pinned_acquisition_evidence_and_owned_cleanup(
    worker_root: Path, mocked_payload: None
) -> None:
    runner = FakeRunner()
    with OstreeAcquirer(worker_root, runner=runner).acquire(uuid4()) as result:
        proof = json.loads(result.evidence.canonical_bytes())
        assert proof["installation_status"] == "inactive"
        assert proof["scope"] == "three-reviewed-ostree-components"
        assert proof["artifact_closure"] == "verified"
        assert proof["ostree_integrity"] == "passed"
        assert len(proof["components"]) == 3
        assert len(result.evidence.canonical_bytes()) < 16384
        assert result.evidence.canonical_bytes() == result.evidence.canonical_bytes()
        assert result.component_path("app").is_dir()
        assert result.stage.parent == worker_root
        stage = result.stage
        with pytest.raises(OstreeAcquisitionError, match="unknown_component"):
            result.component_path("../../escape")  # type: ignore[arg-type]
    assert not stage.exists()
    assert list(worker_root.iterdir()) == [worker_root / "ostree-acquisition.lock"]
    pulls = [cmd[-1] for cmd in runner.commands if "pull" in cmd]
    assert pulls == [
        f"{c.ref}@{c.commit}"
        for c in load_inactive_ostree_recipe().recipe.provider.components
    ]
    assert any("fsck" in cmd for cmd in runner.commands)
    assert all(
        "--disable-verify-bindings" not in cmd and "--no-gpg-verify" not in cmd
        for cmd in runner.commands
    )
    assert sum("--set=gpg-verify=true" in cmd for cmd in runner.commands) == 2


@pytest.mark.parametrize(
    ("command", "code"),
    [
        ("pull", "pinned_artifact_unavailable"),
        ("fsck", "ostree_integrity_failed"),
        ("checkout", "verification_failed"),
        ("--status-fd=1", "signature_invalid"),
    ],
)
def test_failed_verification_never_yields_or_replaces_pins(
    command: str, code: str, worker_root: Path, mocked_payload: None
) -> None:
    runner = FakeRunner(fail=command)
    with (
        pytest.raises(OstreeAcquisitionError, match=code),
        OstreeAcquirer(worker_root, runner=runner).acquire(uuid4()),
    ):
        pytest.fail("unverified artifacts yielded")
    assert list(worker_root.iterdir()) == [worker_root / "ostree-acquisition.lock"]
    assert all("--no-gpg-verify" not in cmd for cmd in runner.commands)


def test_interrupted_acquisition_cleans_only_its_owned_stage(worker_root: Path) -> None:
    preserved = worker_root / "unrelated"
    preserved.mkdir()
    (preserved / "keep").write_text("preserve")
    with (
        pytest.raises(KeyboardInterrupt),
        OstreeAcquirer(worker_root, runner=FakeRunner(interrupt=True)).acquire(uuid4()),
    ):
        pytest.fail("interrupted artifacts yielded")
    assert (preserved / "keep").read_text() == "preserve"
    assert not [p for p in worker_root.glob("ostree-????????*") if p.is_dir()]


def test_partial_stage_is_not_reused_or_removed(worker_root: Path) -> None:
    operation = uuid4()
    stage = worker_root / f"ostree-{operation.hex}"
    stage.mkdir()
    (stage / "partial").write_text("unverified")
    runner = FakeRunner()
    with (
        pytest.raises(OstreeAcquisitionError, match="partial_staging_not_reusable"),
        OstreeAcquirer(worker_root, runner=runner).acquire(operation),
    ):
        pytest.fail("partial artifacts yielded")
    assert not runner.commands
    assert (stage / "partial").read_text() == "unverified"


def test_partial_cache_has_no_authority_and_reacquisition_keeps_pins(
    worker_root: Path, mocked_payload: None
) -> None:
    cache = worker_root / "cache"
    cache.mkdir()
    (cache / "verification.json").write_text('{"ready":true}')
    runs = [FakeRunner(), FakeRunner()]
    proofs = []
    operation = uuid4()
    for runner in runs:
        with OstreeAcquirer(worker_root, runner=runner).acquire(operation) as acquired:
            proofs.append(acquired.evidence.canonical_bytes())
    assert proofs[0] == proofs[1]
    assert [c[-1] for c in runs[0].commands if "pull" in c] == [
        c[-1] for c in runs[1].commands if "pull" in c
    ]
    assert (cache / "verification.json").exists()


def test_concurrent_acquisition_rejects_second_request(
    worker_root: Path, mocked_payload: None
) -> None:
    first = FakeRunner()
    second = FakeRunner()
    with OstreeAcquirer(worker_root, runner=first).acquire(uuid4()) as acquired:
        with (
            pytest.raises(OstreeAcquisitionBusy, match="acquisition_busy"),
            OstreeAcquirer(worker_root, runner=second).acquire(uuid4()),
        ):
            pytest.fail("concurrent acquisition allowed")
        assert acquired.stage.exists()
        assert not second.commands


@pytest.mark.parametrize("boundary", ["root", "ancestor", "lock", "stage"])
def test_staging_rejects_symlink_boundaries(boundary: str, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    root = tmp_path / "worker"
    root.mkdir(mode=0o700)
    operation = uuid4()
    if boundary == "root":
        root.rmdir()
        root.symlink_to(outside, target_is_directory=True)
    elif boundary == "ancestor":
        (tmp_path / "alias").symlink_to(tmp_path, target_is_directory=True)
        root = tmp_path / "alias/worker"
    elif boundary == "lock":
        (root / "ostree-acquisition.lock").symlink_to(outside / "lock")
    else:
        (root / f"ostree-{operation.hex}").symlink_to(outside, target_is_directory=True)
    with (
        pytest.raises(OstreeAcquisitionError),
        OstreeAcquirer(root, runner=FakeRunner()).acquire(operation),
    ):
        pytest.fail("unsafe stage allowed")
    assert not list(outside.iterdir())


def test_staging_requires_private_worker_ownership(tmp_path: Path) -> None:
    tmp_path.chmod(0o755)
    with (
        pytest.raises(OstreeAcquisitionError, match="unsafe_worker_staging_root"),
        OstreeAcquirer(tmp_path, runner=FakeRunner()).acquire(uuid4()),
    ):
        pytest.fail("public stage allowed")


def test_staging_rejects_path_escape(tmp_path: Path) -> None:
    with pytest.raises(OstreeAcquisitionError, match="unsafe_staging_path"):
        _directory(tmp_path / "../escape")


def test_low_disk_space_fails_before_download(
    worker_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "disk_usage", lambda _path: SimpleNamespace(free=0))
    runner = FakeRunner()
    with (
        pytest.raises(OstreeAcquisitionError, match="insufficient_staging_space"),
        OstreeAcquirer(worker_root, runner=runner).acquire(uuid4()),
    ):
        pytest.fail("download without disk capacity")
    assert not runner.commands


def test_private_file_helpers_do_not_follow_links(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.write_text("preserve")
    link = tmp_path / "link"
    link.symlink_to(outside)
    with pytest.raises(OSError):
        _read_file(link)
    with pytest.raises(OSError):
        _write_file(link, b"replace")
    assert outside.read_text() == "preserve"


def test_private_file_helper_rejects_hardlinks(tmp_path: Path) -> None:
    first = tmp_path / "first"
    first.write_bytes(b"data")
    os.link(first, tmp_path / "second")
    with pytest.raises(OstreeAcquisitionError, match="unsafe_staged_file"):
        _read_file(first)


@pytest.mark.parametrize(
    "target",
    [
        "../../escape",
        "/home/kodi/.kodi",
        "/usr/share/alsa/alsa.conf.d/../../../../home/kodi",
    ],
)
def test_payload_links_cannot_escape_reviewed_layout(target: str) -> None:
    with pytest.raises(OstreeAcquisitionError, match="unsafe_payload_symlink"):
        _safe_link("files/link", target, "platform")


def test_reviewed_absolute_link_is_data_not_host_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    component = load_inactive_ostree_recipe().recipe.provider.components[1]
    metadata = signed_component_metadata(signed_fixture("platform")[0], component)
    (tmp_path / "metadata").write_text(metadata)
    (tmp_path / "files").mkdir()
    (tmp_path / "files/host-python").symlink_to("/usr/bin/python3")
    monkeypatch.setattr(ostree, "_CRITICAL_FILES", {"platform": {}})
    digest, count, size = _inventory(tmp_path, component)
    assert len(digest) == 64
    assert count == 3
    assert size == len(metadata.encode())
    assert _inventory(tmp_path, component) == (digest, count, size)


def test_inventory_rejects_changed_critical_content(tmp_path: Path) -> None:
    component = load_inactive_ostree_recipe().recipe.provider.components[0]
    (tmp_path / "metadata").write_text(
        signed_component_metadata(signed_fixture("app")[0], component)
    )
    with pytest.raises(OstreeAcquisitionError, match="critical_content_mismatch"):
        _inventory(tmp_path, component)


def test_stage_quota_is_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "oversized").write_bytes(b"content")
    monkeypatch.setattr(ostree, "MAX_STAGE_BYTES", 1)
    with pytest.raises(OstreeAcquisitionError, match="staging_quota_exceeded"):
        _stage_quota(tmp_path)


@pytest.mark.parametrize(
    "problem", ["missing", "wrong_primary", "wrong_subkey", "bad", "failure"]
)
def test_signature_status_requires_exact_authorized_signer(problem: str) -> None:
    output = valid_signature_status()
    status = 0
    if problem == "missing":
        output = b"[GNUPG:] GOODSIG fake\n"
    elif problem == "wrong_primary":
        output = output.replace(FLATHUB_PRIMARY_KEY.encode(), b"A" * 40)
    elif problem == "wrong_subkey":
        output = output.replace(FLATHUB_SIGNING_SUBKEY.encode(), b"B" * 40)
    elif problem == "bad":
        output += b"[GNUPG:] BADSIG fake\n"
    else:
        status = 1
    with pytest.raises(OstreeAcquisitionError, match="signature_invalid"):
        _validate_signature_status(CommandResult(status, output))


@pytest.mark.parametrize("release", ["22.0~rc2-Piers", "22.0-Piers", "23.0~rc1"])
def test_rc2_final_or_future_embedded_release_is_rejected(
    release: str, tmp_path: Path
) -> None:
    write_release_fixture(tmp_path, release=release)
    with pytest.raises(OstreeAcquisitionError, match="release_metadata_mismatch"):
        _application_release(tmp_path)


def write_release_fixture(
    root: Path,
    *,
    release: str = "22.0~rc1-Piers",
    revision: str = KODI_RC1_SOURCE_REVISION,
) -> None:
    (root / "files/share/metainfo").mkdir(parents=True)
    manifest = {
        "runtime": "org.freedesktop.Platform",
        "runtime-version": "26.08",
        "modules": [
            {
                "name": "kodi",
                "sources": [
                    {
                        "type": "git",
                        "url": "https://github.com/xbmc/xbmc.git",
                        "commit": revision,
                    }
                ],
            }
        ],
    }
    (root / "files/manifest.json").write_text(json.dumps(manifest))
    (root / "files/share/metainfo/tv.kodi.Kodi.metainfo.xml").write_text(
        "<component><id>tv.kodi.Kodi</id><releases>"
        f'<release version="{release}" type="development"/>'
        "</releases></component>"
    )


def test_wrong_upstream_source_is_rejected(tmp_path: Path) -> None:
    write_release_fixture(tmp_path, revision="a" * 40)
    with pytest.raises(OstreeAcquisitionError, match="source_revision_mismatch"):
        _application_release(tmp_path)


def test_exact_embedded_release_is_accepted(tmp_path: Path) -> None:
    write_release_fixture(tmp_path)
    _application_release(tmp_path)
