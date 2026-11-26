from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path
from zipfile import ZipFile

import pytest

from kodi_lab.managed import controller as managed
from kodi_lab.managed import packages as managed_packages
from kodi_lab.managed.controller import Controller
from kodi_lab.managed.feed_retirement import FeedRetirement
from kodi_lab.managed.packages import Package

ADDON = "script.retirementfixture"


class Interrupted(BaseException):
    pass


def archive(version: str, *, payload: str = "original") -> bytes:
    stream = io.BytesIO()
    with ZipFile(stream, "w") as bundle:
        bundle.writestr(
            f"{ADDON}/addon.xml",
            f'<addon id="{ADDON}" name="Fixture" version="{version}">'
            '<extension point="xbmc.python.script" library="default.py"/></addon>',
        )
        bundle.writestr(f"{ADDON}/default.py", f"print({payload!r})")
    return stream.getvalue()


@pytest.fixture
def site(tmp_path: Path) -> Controller:
    root = tmp_path / "site"
    root.mkdir(mode=0o700)
    return Controller(root)


def book(site: Controller) -> FeedRetirement:
    return FeedRetirement(site.omega.root, site.omega.root, site.omega.target)


def path_for(site: Controller, package: Package) -> Path:
    return (
        site.omega.private_feed
        / "zips"
        / package.id
        / f"{package.id}-{package.version}.zip"
    )


def publish(site: Controller, version: str, *, payload: str = "original") -> Package:
    pkg = site.packages.approve(archive(version, payload=payload))
    site.packages.publish(pkg, retirement=book(site))
    return pkg


def eligible(site: Controller) -> bool:
    tracker = book(site)
    state = tracker.read()
    if not state.pending:
        return False
    return tracker.prove(state, state.pending[0]) is not None


def idle_engine(monkeypatch: pytest.MonkeyPatch, *, observed: str = "") -> None:
    def run(
        args: list[str], *, timeout: float, **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        assert args == [
            "docker",
            "--host",
            "unix:///var/run/docker.sock",
            "ps",
            "-aq",
            "--filter",
            "name=^/kodi-lab-managed-v2$",
        ]
        assert timeout == 0.5
        return subprocess.CompletedProcess(args, 0, stdout=observed)

    monkeypatch.setattr(managed.subprocess, "run", run)


def test_keeps_canonical_bytes_current_copy_and_unknown_legacy(
    site: Controller, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = publish(site, "1.0.0")
    legacy = path_for(site, first).parent / f"{ADDON}-0.1.0.zip"
    legacy.write_bytes(b"unknown history")
    second = publish(site, "2.0.0")
    assert path_for(site, first).read_bytes() == first.data
    assert eligible(site)
    idle_engine(monkeypatch)
    site.resume_feed_cleanup()
    assert not path_for(site, first).exists()
    assert not path_for(site, first).with_suffix(".zip.sha256").exists()
    assert path_for(site, second).read_bytes() == second.data
    assert site.packages.get(first.digest).data == first.data
    assert legacy.read_bytes() == b"unknown history"


def test_same_version_uses_current_path_and_retains_both_approved_archives(
    site: Controller, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = publish(site, "1.0.0")
    second = publish(site, "1.0.0", payload="changed")
    idle_engine(monkeypatch)
    site.resume_feed_cleanup()
    assert not book(site).read().pending
    assert path_for(site, first).read_bytes() == second.data
    assert site.packages.get(first.digest).data == first.data


@pytest.mark.parametrize(
    "protection", ["catalog", "canonical_changed", "copy_changed", "public"]
)
def test_declines_retirement_on_protection_or_identity_drift(
    site: Controller, monkeypatch: pytest.MonkeyPatch, protection: str
) -> None:
    first = publish(site, "1.0.0")
    publish(site, "2.0.0")
    original = site.omega.root / "approved" / (first.digest + ".zip")
    if protection == "catalog":
        (site.omega.root / "catalog.json").write_text(json.dumps([first.digest]))
    elif protection == "canonical_changed":
        original.write_bytes(b"changed canonical")
    elif protection == "copy_changed":
        path_for(site, first).write_bytes(b"changed copy")
    else:
        site.omega.public.session.write_text("pending test")
    idle_engine(monkeypatch)
    site.resume_feed_cleanup()
    assert path_for(site, first).exists()
    assert book(site).read().pending


@pytest.mark.parametrize("protect", ["active", "native_pending"])
def test_native_work_blocks_feed_removal(
    site: Controller, monkeypatch: pytest.MonkeyPatch, protect: str
) -> None:
    first = publish(site, "1.0.0")
    publish(site, "2.0.0")
    if protect == "active":
        idle_engine(monkeypatch, observed="container")
    else:
        idle_engine(monkeypatch)
        site.write_operation(
            {"name": "addon_install", "state": "pending", "native_pending": True}
        )
    site.resume_feed_cleanup()
    assert path_for(site, first).exists()


@pytest.mark.parametrize("committed", [False, True])
def test_interrupted_publication_reconciles_only_verified_index(
    site: Controller, monkeypatch: pytest.MonkeyPatch, committed: bool
) -> None:
    first = publish(site, "1.0.0")
    second = site.packages.approve(archive("2.0.0"))

    def interrupt(_book: FeedRetirement) -> None:
        raise Interrupted

    atomic = managed_packages.atomic

    def fail_before_checksum(path: Path, data: bytes) -> None:
        if path.name == "addons.xml.sha256":
            raise Interrupted
        atomic(path, data)

    with monkeypatch.context() as faults:
        if committed:
            faults.setattr(FeedRetirement, "confirm_publication", interrupt)
        else:
            faults.setattr(managed_packages, "atomic", fail_before_checksum)
        with pytest.raises(Interrupted):
            site.packages.publish(second, retirement=book(site))
    assert path_for(site, first).exists()
    assert book(site).read().planned
    idle_engine(monkeypatch)
    recreated = Controller(site.omega.root)
    recreated.resume_feed_cleanup()
    assert path_for(site, first).exists()
    recreated.resume_feed_cleanup()
    assert path_for(site, first).exists() is not committed
    assert path_for(site, second).exists()
    assert site.packages.get(first.digest).data == first.data


def test_durable_deletion_phase_retries_partial_retirement(
    site: Controller, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = publish(site, "1.0.0")
    publish(site, "2.0.0")
    target = path_for(site, first)
    sidecar = target.with_suffix(".zip.sha256")
    idle_engine(monkeypatch)
    unlink = Path.unlink

    def fail_checksum(path: Path, *args: object, **kwargs: object) -> None:
        if path == sidecar:
            raise OSError("injected checksum unlink failure")
        unlink(path, missing_ok=bool(kwargs.get("missing_ok", False)))

    with monkeypatch.context() as faults:
        faults.setattr(Path, "unlink", fail_checksum)
        site.resume_feed_cleanup()
    assert not target.exists()
    assert sidecar.exists()
    assert book(site).read().pending[0].phase == "removing"
    site.resume_feed_cleanup()
    assert not sidecar.exists()
    assert not book(site).read().pending


def test_changed_file_after_proof_is_preserved(site: Controller) -> None:
    first = publish(site, "1.0.0")
    publish(site, "2.0.0")
    tracker = book(site)
    state = tracker.read()
    pending = state.pending[0]
    proof = tracker.prove(state, pending)
    assert proof is not None
    path_for(site, first).write_bytes(b"replacement")
    assert not tracker.retire(state, pending, proof)
    assert path_for(site, first).read_bytes() == b"replacement"
