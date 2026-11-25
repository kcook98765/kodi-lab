from __future__ import annotations

from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from zipfile import ZipFile

import pytest

from kodi_lab.managed.controller import Controller
from kodi_lab.managed.packages import read_zip, sha
from kodi_lab.managed.protocol import Refusal
from kodi_lab.managed.public_relay import HOST, approved_connect, approved_tls_hello
from kodi_lab.managed.public_repository import BASE, LANES, passive_repository, stored


def repository(*, prefix: str = BASE, extra: str = "") -> bytes:
    lane_rows = []
    for lane, (minimum, maximum) in LANES.items():
        catalog = f"{prefix}catalogs/{lane}/addons.xml"
        lane_rows.append(
            f'<dir minversion="{minimum}" maxversion="{maximum}">'
            f"<info>{catalog}</info>"
            f'<checksum verify="sha256">{catalog}.sha256</checksum>'
            f"<datadir>{prefix}packages/</datadir>"
            f"<artdir>{prefix}artwork/{lane}/</artdir><hashes>sha256</hashes></dir>"
        )
    manifest = (
        '<addon id="repository.ai.kodi" version="0.1.1" name="kcook-lab-repo">'
        '<requires><import addon="xbmc.addon" version="19.0.0"/></requires>'
        '<extension point="xbmc.addon.repository">'
        + "".join(lane_rows)
        + '</extension><extension point="xbmc.addon.metadata"/>'
        + extra
        + "</addon>"
    )
    data = BytesIO()
    with ZipFile(data, "w") as archive:
        archive.writestr("repository.ai.kodi/addon.xml", manifest)
    return data.getvalue()


def test_passive_repository_requires_separate_parser_and_pinned_endpoints() -> None:
    data = repository()
    with pytest.raises(Refusal, match="invalid_addon_id"):
        read_zip(data)
    assert passive_repository(data).id == "repository.ai.kodi"
    with pytest.raises(Refusal, match="public_repository_origin"):
        passive_repository(repository(prefix="https://example.invalid/"))
    with pytest.raises(Refusal, match="unsupported_extension"):
        passive_repository(repository(extra='<extension point="xbmc.service" />'))


def test_select_requires_approval_and_existing_managed_owner(tmp_path: Path) -> None:
    root = tmp_path / "lab"
    root.mkdir(mode=0o700)
    controller = Controller(root)
    actor = "ddbbc150-bf53-11f1-8b3d-0800200c9a66"
    with pytest.raises(Refusal, match="managed_acquire_required"):
        controller.perform(
            "managed_public_repository_select",
            {"repository": "repository.ai.kodi"},
            actor=actor,
        )
    controller.perform("managed_acquire", {}, actor=actor)
    with pytest.raises(Refusal, match="public_repository_not_approved"):
        controller.perform(
            "managed_public_repository_select",
            {"repository": "repository.ai.kodi"},
            actor=actor,
        )
    assert not controller.omega.public.session.exists()


def test_changed_reviewed_archive_fails_readback(tmp_path: Path) -> None:
    approved = tmp_path / "public-approved"
    approved.mkdir()
    data = repository()
    digest = sha(data)
    archive = approved / digest
    archive.write_bytes(data)
    assert stored(tmp_path, digest) == data
    archive.write_bytes(data + b"changed")
    with pytest.raises(Refusal, match="public_approval_mismatch"):
        stored(tmp_path, digest)


def test_failed_public_start_and_stop_revoke_egress(tmp_path: Path) -> None:
    controller = Controller(tmp_path)
    runtime = controller.omega
    runtime.public.session.touch()
    with (
        patch.object(runtime.public, "remove") as remove,
        patch.object(runtime, "start", side_effect=Refusal("startup_failed")),
        patch.object(runtime, "stop", side_effect=Refusal("stop_failed")),
    ):
        with pytest.raises(Refusal, match="startup_failed"):
            controller.start_runtime()
        remove.assert_called_once_with()
        remove.reset_mock()
        with pytest.raises(Refusal, match="stop_failed"):
            controller.stop_runtime()
        remove.assert_called_once_with()


def client_hello(host: str, *, extension: bytes = b"") -> bytes:
    def vector(data: bytes, size: int = 2) -> bytes:
        return len(data).to_bytes(size) + data

    name = vector(b"\0" + vector(host.encode()))
    extensions = vector(b"\0\0" + vector(name) + extension)
    data = (
        b"\x03\x03"
        + bytes(32)
        + vector(b"", 1)
        + vector(b"\x13\x01")
        + vector(b"\0", 1)
        + extensions
    )
    return b"\x01" + vector(data, 3)


def test_connect_and_tls_name_pinned() -> None:
    assert approved_connect(f"CONNECT {HOST}:443 HTTP/1.1\r\n\r\n".encode())
    assert not approved_connect(b"CONNECT example.invalid:443 HTTP/1.1\r\n\r\n")
    assert not approved_connect(f"GET https://{HOST}/ HTTP/1.1\r\n\r\n".encode())
    assert not approved_connect(f"CONNECT {HOST}:80 HTTP/1.1\r\n\r\n".encode())
    assert approved_tls_hello(client_hello(HOST))
    assert not approved_tls_hello(client_hello("example.invalid"))
    assert not approved_tls_hello(client_hello(HOST)[:-1])
    assert not approved_tls_hello(client_hello(HOST, extension=b"\xfe\x0d\0\0"))
