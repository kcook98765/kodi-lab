from __future__ import annotations

import stat
import zipfile
from hashlib import sha256
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response

from kodi_lab.managed.broker import BrokerClient
from kodi_lab.managed.controller import Controller
from kodi_lab.managed.protocol import OperationReply
from tests.web.test_managed_upload import bearer


def _valid_zip() -> bytes:
    output = BytesIO()
    addon_id = "plugin.video.webzip"
    descriptor = (
        f'<addon id="{addon_id}" name="Web ZIP" version="1.0.0">'
        '<extension point="xbmc.python.pluginsource" library="default.py">'
        "<provides>video</provides></extension>"
        '<extension point="xbmc.addon.metadata" /></addon>'
    )
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{addon_id}/addon.xml", descriptor)
        archive.writestr(f"{addon_id}/default.py", "print('passive')\n")
    return output.getvalue()


def _unsafe_zip(case: str) -> bytes:
    output = BytesIO()
    addon_id = "plugin.video.unsafe"
    descriptor = (
        f'<addon id="{addon_id}" name="Unsafe" version="1.0.0">'
        '<extension point="xbmc.python.pluginsource" library="default.py">'
        "<provides>video</provides></extension></addon>"
    )
    if case == "repository":
        descriptor = (
            f'<addon id="{addon_id}" name="Unsafe" version="1.0.0">'
            '<extension point="xbmc.addon.repository" /></addon>'
        )
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(f"{addon_id}/addon.xml", descriptor)
        archive.writestr(f"{addon_id}/default.py", "pass\n")
        if case == "traversal":
            archive.writestr("../escape", "x")
        elif case == "vcs":
            archive.writestr(f"{addon_id}/.GiT/config", "x")
        elif case == "duplicate":
            with pytest.warns(UserWarning):
                archive.writestr(f"{addon_id}/default.py", "other\n")
        elif case == "symlink":
            link = zipfile.ZipInfo(f"{addon_id}/link")
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(link, "default.py")
    return output.getvalue()


@pytest.fixture
def managed_upload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Controller:
    root = tmp_path / "managed"
    root.mkdir(mode=0o700)
    controller = Controller(root)

    def upload(
        _broker: BrokerClient, actor: str, archive: bytes, digest: str
    ) -> OperationReply:
        return controller.upload_package(archive, digest, actor=actor)

    monkeypatch.setattr(BrokerClient, "upload", upload)
    return controller


def _send(client: TestClient, token: str, data: bytes, digest: str) -> Response:
    return client.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/zip",
            "X-Kodi-Lab-Package-SHA256": digest,
        },
        content=data,
    )


def test_managed_upload_returns_only_exact_artifact_identity(
    client: TestClient, managed_upload: Controller
) -> None:
    actor, token = bearer(client, managed=True)
    payload = _valid_zip()
    digest = sha256(payload).hexdigest()
    mismatch = _send(client, token, payload, "1" * 64)
    assert mismatch.status_code == 422
    assert mismatch.json()["code"] == "package_digest_mismatch"
    assert (
        list(managed_upload.actor_directory(actor).joinpath("approved").iterdir()) == []
    )
    response = _send(client, token, payload, digest)
    assert response.status_code == 200
    assert response.json()["result"] == {
        "addon": "plugin.video.webzip",
        "version": "1.0.0",
        "digest": digest,
    }
    approved = managed_upload.actor_directory(actor) / "approved"
    assert list(approved.iterdir()) == [approved / f"{digest}.zip"]
    assert (approved / f"{digest}.zip").read_bytes() == payload
    assert not list(managed_upload.runtime.feed.iterdir())


@pytest.mark.parametrize(
    ("case", "code"),
    [
        ("traversal", "unsafe_package_path"),
        ("symlink", "unsafe_zip_entry"),
        ("vcs", "unsafe_package_path"),
        ("duplicate", "unsafe_zip_entry"),
        ("repository", "unsupported_extension"),
    ],
)
def test_unsafe_zip_never_enters_managed_approval_or_feed(
    client: TestClient, managed_upload: Controller, case: str, code: str
) -> None:
    actor, token = bearer(client, managed=True)
    payload = _unsafe_zip(case)
    response = _send(client, token, payload, sha256(payload).hexdigest())
    assert response.status_code == 422
    assert response.json()["code"] == code
    assert not list((managed_upload.actor_directory(actor) / "approved").iterdir())
    assert not list(managed_upload.runtime.feed.iterdir())
